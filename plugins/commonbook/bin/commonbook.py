#!/usr/bin/env python3
"""Commonbook — bind a repo's Claude Code memory to its git identity.

Claude Code's auto memory is stored per project at
``~/.claude/projects/<sanitized-cwd>/memory/``, keyed on the *filesystem path*
of the canonical git root. There is no remap mechanism: move or rename the
directory and a fresh, empty store appears while the old notes stay on disk,
unreachable, forever. Memory files are excluded from the transcript retention
sweep, so they are not cleaned up either — they simply accumulate as orphans.

``autoMemoryDirectory`` overrides that location and is read from user, project,
local or policy settings scope. Written into a repo's
``.claude/settings.local.json`` it travels with the directory, so the store is
addressed by *configuration* rather than by a path that changes.

Commonbook keys that directory on a hash of the git remote, which gives three
properties the default lacks:

* it survives ``mv`` and rename,
* it is re-derivable after a fresh clone,
* every worktree and every second checkout of the same repo share one store.

Three verbs, no configuration file, no network, standard library only.

    commonbook adopt     find orphaned stores and bring them into the book
    commonbook bind      point this repo's memory at its book
    commonbook doctor    report what is bound, unbound, orphaned or at risk

Python 3.9+.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

__version__ = "0.1.0"

HOME = Path.home()
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
DEFAULT_BOOKS = CLAUDE_DIR / "commonbook"

# Written into an orphan after its notes are copied out, so `doctor` can tell
# "unreachable, act now" from "already rescued, safe to delete". Without it the
# adopt leaves originals in place (deliberately) and doctor nags forever.
ADOPTED_MARKER = ".commonbook-adopted.json"

GIT_TIMEOUT = 10


# ─────────────────────────────────────────────────────────────── output ──

class Out:
    """Terminal output. Colour only when stdout is a TTY and NO_COLOR is unset."""

    def __init__(self) -> None:
        self.tty = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.tty else text

    def bold(self, t): return self._c("1", t)
    def dim(self, t): return self._c("2", t)
    def green(self, t): return self._c("32", t)
    def yellow(self, t): return self._c("33", t)
    def red(self, t): return self._c("31", t)
    def cyan(self, t): return self._c("36", t)

    def say(self, msg: str = "") -> None:
        print(msg)

    def warn(self, msg: str) -> None:
        print(f"{self.yellow('!')} {msg}")

    def err(self, msg: str) -> None:
        print(f"{self.red('x')} {msg}", file=sys.stderr)


out = Out()


# ────────────────────────────────────────────────────────────────── git ──

def git(*args: str, cwd=None) -> "tuple[str, int]":
    """Run git, returning (stdout, returncode). Never raises."""
    try:
        p = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
        return p.stdout.strip(), p.returncode
    except (OSError, subprocess.SubprocessError):
        return "", 1


def repo_root(start: Path) -> "Path | None":
    """The canonical repo root — the MAIN worktree, not a linked one.

    Auto memory is keyed on the canonical root so linked worktrees share one
    store. --git-common-dir points at the main checkout's .git even from inside
    a linked worktree, which is what makes that sharing work.
    """
    common, rc = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=start)
    if rc != 0 or not common:
        return None
    p = Path(common)
    return p.parent if p.name == ".git" else p


def normalize_remote(url: str) -> str:
    """Canonical form of a remote URL, so every transport spells it the same.

    git@github.com:Owner/Repo.git, https://github.com/owner/repo and
    ssh://git@github.com/owner/repo/ must all produce one identity, or a second
    clone added over a different transport silently gets a second book.
    """
    u = url.strip()
    u = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", u)   # scheme
    u = re.sub(r"^[^/@]+@", "", u)                       # user@
    u = u.replace(":", "/", 1) if "@" not in u and ":" in u.split("/")[0] else u
    u = re.sub(r"\.git$", "", u)
    u = u.rstrip("/").lower()
    return u


def identity(root: Path) -> "tuple[str, str, str]":
    """(book_id, kind, human) for a directory.

    Falls back deliberately, and the caller is expected to SAY which fallback
    was used — a path-keyed book has the same failure the tool exists to fix.
    """
    url, rc = git("remote", "get-url", "origin", cwd=root)
    if rc == 0 and url:
        norm = normalize_remote(url)
        return hashlib.sha256(norm.encode()).hexdigest()[:16], "remote", norm

    first, rc = git("rev-list", "--max-parents=0", "HEAD", cwd=root)
    if rc == 0 and first:
        sha = first.split("\n")[0].strip()
        return hashlib.sha256(sha.encode()).hexdigest()[:16], "root-commit", sha[:12]

    real = str(root.resolve())
    return hashlib.sha256(real.encode()).hexdigest()[:16], "path", real


# ───────────────────────────────────────────────────────── claude state ──

def sanitize(path: str) -> str:
    """Claude Code's own project-directory encoding: every non-alphanumeric
    becomes a hyphen. This is LOSSY and must never be inverted — see cwd_of()."""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(path))


def cwd_of(project_dir: Path) -> "str | None":
    """The authoritative cwd for a project store.

    Read from the first line of any transcript rather than decoded from the
    directory name. The encoding maps '-' to BOTH '/' and a literal hyphen, so
    inverting it is ambiguous and reports most live projects as orphans.
    """
    try:
        entries = sorted(project_dir.glob("*.jsonl"))
    except OSError:
        return None
    for jf in entries:
        try:
            with jf.open(encoding="utf-8", errors="replace") as fh:
                for _ in range(5):          # cwd is on the first record
                    line = fh.readline()
                    if not line:
                        break
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cwd = rec.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
        except OSError:
            continue
    return None


def notes_in(d: Path) -> "list[Path]":
    try:
        return [p for p in d.rglob("*.md") if p.is_file()]
    except OSError:
        return []


def scan_stores() -> "list[dict]":
    """Every project store that holds at least one note."""
    found = []
    if not PROJECTS_DIR.is_dir():
        return found
    try:
        entries = sorted(PROJECTS_DIR.iterdir())
    except OSError:
        return found
    for proj in entries:
        if not proj.is_dir():
            continue
        mem = proj / "memory"
        if not mem.is_dir():
            continue
        notes = notes_in(mem)
        if not notes:
            continue
        cwd = cwd_of(proj)
        found.append({
            "dir": proj,
            "memory": mem,
            "cwd": cwd,
            "alive": bool(cwd) and Path(cwd).is_dir(),
            "adopted": (mem / ADOPTED_MARKER).is_file(),
            "notes": notes,
            "bytes": sum(_size(p) for p in notes),
        })
    return found


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def settings_path(root: Path) -> Path:
    return root / ".claude" / "settings.local.json"


def read_settings(root: Path) -> "tuple[dict, bool]":
    """(settings, parseable). An unparseable file is never overwritten.

    A file can be valid JSON and still not be settings: `[1,2,3]`, `null`,
    `"hello"` and `42` all parse. Returning any of them as the settings dict
    makes the first `.get()` raise AttributeError several frames away from the
    cause, so the type is checked here rather than at every call site.
    """
    f = settings_path(root)
    if not f.is_file():
        return {}, True
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, False
    return (data, True) if isinstance(data, dict) else ({}, False)


def bound_dir(settings: dict) -> "str | None":
    """The configured book, only when it is usable as a path.

    A non-string value here reaches Path() and raises TypeError. Treating it as
    absent, and letting the caller report the file as unusable, keeps a
    hand-edited settings file from taking a read-only command down.
    """
    v = settings.get("autoMemoryDirectory")
    return v if isinstance(v, str) and v else None


def is_tracked(root: Path, rel: str) -> bool:
    _, rc = git("ls-files", "--error-unmatch", rel, cwd=root)
    return rc == 0


def set_skip_worktree(root: Path, rel: str) -> bool:
    """Ask git to ignore local edits to a tracked file, and VERIFY it took.

    update-index exits 0 even when the path is not in the index, so trusting
    the exit code would silently leave the binding committable.
    """
    git("update-index", "--skip-worktree", rel, cwd=root)
    o, rc = git("ls-files", "-v", rel, cwd=root)
    return rc == 0 and o.startswith("S ")


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ──────────────────────────────────────────────────────────────── verbs ──

def resolve_target(root: Path, book_id: str, vault: "str | None") -> Path:
    if vault:
        return Path(vault).expanduser().resolve() / book_id / "memory"
    return DEFAULT_BOOKS / book_id / "memory"


def cmd_bind(args) -> int:
    start = Path(args.path).expanduser().resolve()
    if not start.is_dir():
        out.err(f"not a directory: {start}")
        return 2

    root = repo_root(start) or start
    book_id, kind, human = identity(root)
    target = resolve_target(root, book_id, args.vault)

    settings, ok = read_settings(root)
    if not ok:
        out.err(f"{settings_path(root)} is not valid JSON — refusing to overwrite it")
        return 2

    # A tracked settings file would carry an absolute local path into everyone
    # else's clone. skip-worktree keeps the committed version and ignores the
    # local edit; if that cannot be set, refuse rather than leak.
    rel = ".claude/settings.local.json"
    if is_tracked(root, rel) and not set_skip_worktree(root, rel):
        out.err("settings.local.json is tracked by git and skip-worktree could not be set.")
        out.say("  Binding here would commit a local absolute path. Refusing.")
        return 2

    current = bound_dir(settings)
    if current == str(target):
        out.say(f"{out.green('already bound')}  {root.name} -> {target}")
        return 0

    settings["autoMemoryDirectory"] = str(target)
    if not args.dry_run:
        write_json_atomic(settings_path(root), settings)

    verb = "rebound" if current else "bound"
    out.say(f"{out.green(verb)}  {out.bold(root.name)} -> {target}")
    out.say(f"  identity: {kind}  {out.dim(human)}")
    if kind == "path":
        out.warn("no git remote and no commits — this book is keyed on the path,")
        out.say("  so it will orphan again if the directory moves. Add a remote to fix.")
    if args.dry_run:
        out.say(out.dim("  (dry run — nothing written)"))
    return 0


def match_orphan(cwd: str, live_roots: "dict[str, Path]") -> "Path | None":
    """Best live repo for an orphaned store: exact path, then basename."""
    name = Path(cwd).name.lower()
    for root in live_roots.values():
        if str(root).lower() == cwd.lower():
            return root
    hits = [r for r in live_roots.values() if r.name.lower() == name]
    return hits[0] if len(hits) == 1 else None


def discover_live(search: Path, depth: int) -> "dict[str, Path]":
    """Live git repos under a search root, to match orphans against."""
    roots: "dict[str, Path]" = {}
    skip = {"node_modules", ".next", "dist", "build", ".venv", ".turbo",
            "__pycache__", ".git", "vendor", "target"}
    base_depth = len(search.parts)
    for cur, dirs, _ in os.walk(search):
        p = Path(cur)
        if len(p.parts) - base_depth > depth:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        if (p / ".git").exists():
            roots[str(p)] = p
            dirs[:] = []
    return roots


def cmd_adopt(args) -> int:
    stores = scan_stores()
    orphans = [s for s in stores if not s["alive"] and not s["adopted"]]
    if not orphans:
        live = len(stores)
        out.say(f"{out.green('no orphaned memory')} — {live} store(s) all resolve to a live directory")
        return 0

    search = Path(args.search).expanduser().resolve()
    live_roots = discover_live(search, args.depth)

    plan = []
    for s in orphans:
        root = match_orphan(s["cwd"] or "", live_roots) if s["cwd"] else None
        plan.append((s, root))

    matched = [(s, r) for s, r in plan if r]
    unmatched = [(s, r) for s, r in plan if not r]

    total = sum(s["bytes"] for s, _ in matched)
    projects = {r.name for _, r in matched}

    out.say(out.bold(f"found {len(orphans)} orphaned memory store(s)"))
    out.say(out.dim(f"  searched {search} to depth {args.depth} · {len(live_roots)} live repo(s)"))
    out.say()
    for s, root in matched:
        out.say(f"  {out.cyan(root.name):<28} {len(s['notes']):>3} notes  {s['bytes']:>7,} B"
                f"  {out.dim(s['cwd'] or '?')}")
    for s, _ in unmatched:
        out.say(f"  {out.yellow('unmatched'):<28} {len(s['notes']):>3} notes  {s['bytes']:>7,} B"
                f"  {out.dim(s['cwd'] or 'no cwd recorded')}")
    out.say()

    if not matched:
        out.warn("nothing could be matched to a live repository")
        out.say("  Try --search <dir> or --depth N, or move the repo back and re-run.")
        return 1

    if args.dry_run:
        out.say(out.dim(f"dry run — would adopt {len(matched)} store(s), "
                        f"{total:,} bytes, {len(projects)} project(s)"))
        return 0

    if not args.yes and sys.stdin.isatty():
        try:
            reply = input(f"adopt {len(matched)} store(s) into each project's book? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            out.say()
            return 1
        if reply.strip().lower() not in ("y", "yes"):
            out.say("nothing adopted")
            return 1

    adopted = 0
    for s, root in matched:
        book_id, _, _ = identity(root)
        target = resolve_target(root, book_id, args.vault)
        target.mkdir(parents=True, exist_ok=True)
        tag = re.sub(r"[^a-z0-9]+", "-", Path(s["cwd"]).name.lower()).strip("-")[:32] or "orphan"
        for note in s["notes"]:
            rel = note.relative_to(s["memory"])
            # Orphans land BESIDE the live notes, never merged into them.
            # Merging is the one operation that loses information silently.
            dest = target / rel
            if dest.exists():
                dest = target / f"{rel.stem}.from-{tag}{rel.suffix}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(note, dest)
        write_json_atomic(s["memory"] / ADOPTED_MARKER, {
            "adoptedInto": str(target),
            "notes": len(s["notes"]),
            "bytes": s["bytes"],
            "originalCwd": s["cwd"],
            "tool": f"commonbook {__version__}",
        })
        adopted += 1
        # Bind so it cannot orphan again.
        bind_args = argparse.Namespace(path=str(root), vault=args.vault, dry_run=False)
        cmd_bind(bind_args)

    out.say()
    out.say(out.green(f"adopted {adopted} store(s) · {total:,} bytes · {len(projects)} project(s)"))
    out.say(out.dim("  originals left in place — remove them once you have verified the copies"))
    return 0


def cmd_doctor(args) -> int:
    start = Path(args.path).expanduser().resolve()
    root = repo_root(start)
    problems = 0

    out.say(out.bold("commonbook doctor"))
    out.say()

    # ── this repo
    if root:
        book_id, kind, human = identity(root)
        settings, ok = read_settings(root)
        bound = bound_dir(settings)
        target = resolve_target(root, book_id, args.vault)

        out.say(f"  repo      {root}")
        out.say(f"  identity  {kind}  {out.dim(human)}")
        if not ok:
            out.say(f"  binding   {out.red('settings.local.json is not valid JSON')}")
            problems += 1
        elif not bound:
            out.say(f"  binding   {out.yellow('unbound')} — memory is keyed on this path and will")
            out.say(f"            orphan if the directory moves. Fix: commonbook bind")
            problems += 1
        elif bound != str(target):
            out.say(f"  binding   {out.yellow('bound elsewhere')} -> {bound}")
            out.say(f"            expected {target}")
        else:
            n = len(notes_in(Path(bound))) if Path(bound).is_dir() else 0
            out.say(f"  binding   {out.green('bound')} -> {bound} {out.dim(f'({n} notes)')}")

        if kind == "path":
            out.say(f"            {out.yellow('no stable identity')} — add a git remote")
            problems += 1

        if is_tracked(root, ".claude/settings.local.json"):
            o, _ = git("ls-files", "-v", ".claude/settings.local.json", cwd=root)
            if o.startswith("S "):
                out.say(f"            {out.dim('settings.local.json is tracked but skip-worktree is set')}")
            else:
                out.say(f"            {out.red('settings.local.json is tracked and committable')}")
                problems += 1

        # CLAUDE.md does not include AGENTS.md automatically.
        cmd_md, agents_md = root / "CLAUDE.md", root / "AGENTS.md"
        if agents_md.is_file() and cmd_md.is_file():
            try:
                if "@AGENTS.md" not in cmd_md.read_text(encoding="utf-8", errors="replace"):
                    out.say(f"  agents    {out.yellow('AGENTS.md exists but CLAUDE.md does not import it')}")
                    out.say(f"            Claude Code reads CLAUDE.md; add a line: @AGENTS.md")
            except OSError:
                pass
    else:
        out.say(f"  repo      {out.yellow('not a git repository')} — {start}")
        out.say(f"            a book here would be keyed on the path and orphan on any move")
        problems += 1

    # ── machine-wide
    stores = scan_stores()
    orphans = [s for s in stores if not s["alive"] and not s["adopted"]]
    rescued = [s for s in stores if not s["alive"] and s["adopted"]]
    out.say()
    out.say(f"  stores    {len(stores)} with notes, {len(orphans)} orphaned")
    if orphans:
        b = sum(s["bytes"] for s in orphans)
        n = sum(len(s["notes"]) for s in orphans)
        out.say(f"            {out.yellow(f'{n} notes, {b:,} bytes unreachable')} — commonbook adopt")
        problems += 1
    if rescued:
        b = sum(s["bytes"] for s in rescued)
        out.say(f"            {out.dim(f'{len(rescued)} already adopted, {b:,} bytes — commonbook prune')}")

    out.say()
    if problems:
        out.say(out.yellow(f"{problems} thing(s) to fix"))
    else:
        out.say(out.green("all good"))
    return 1 if problems else 0


def cmd_prune(args) -> int:
    """Delete orphan stores whose notes were already copied into a book.

    Only ever touches a store carrying the adoption marker, and re-verifies that
    every note it holds still exists in the recorded target before deleting
    anything. A store that fails that check is kept and reported — the whole
    point of adopt is that these notes exist nowhere else.
    """
    rescued = [s for s in scan_stores() if not s["alive"] and s["adopted"]]
    if not rescued:
        out.say("nothing to prune — no adopted orphans")
        return 0

    safe, unsafe = [], []
    for s in rescued:
        try:
            marker = json.loads((s["memory"] / ADOPTED_MARKER).read_text(encoding="utf-8"))
            target = Path(marker.get("adoptedInto", ""))
        except (OSError, json.JSONDecodeError):
            unsafe.append((s, "marker unreadable"))
            continue
        if not target.is_dir():
            unsafe.append((s, "target book is gone"))
            continue
        missing = [n.name for n in s["notes"] if not list(target.rglob(n.name))]
        if missing:
            unsafe.append((s, f"{len(missing)} note(s) not found in the book"))
        else:
            safe.append((s, target))

    for s, target in safe:
        out.say(f"  {out.green('verified'):<22} {len(s['notes']):>3} notes present in {target}")
    for s, why in unsafe:
        out.say(f"  {out.yellow('kept'):<22} {s['dir'].name[:44]} — {why}")

    if not safe:
        out.say()
        out.warn("nothing verified as safe to delete")
        return 1

    total = sum(s["bytes"] for s, _ in safe)
    if args.dry_run:
        out.say()
        out.say(out.dim(f"dry run — would delete {len(safe)} store(s), {total:,} bytes"))
        return 0
    if not args.yes and sys.stdin.isatty():
        try:
            reply = input(f"delete {len(safe)} adopted store(s)? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            out.say()
            return 1
        if reply.strip().lower() not in ("y", "yes"):
            out.say("nothing deleted")
            return 1

    for s, _ in safe:
        shutil.rmtree(s["memory"], ignore_errors=True)
    out.say()
    out.say(out.green(f"pruned {len(safe)} store(s) · {total:,} bytes"))
    return 0


def note_type(text: str) -> "str | None":
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    t = re.search(r"^type:\s*(\S+)", m.group(1), re.M)
    return t.group(1).strip().lower() if t else None


def contract_violations(path: Path) -> "list[str]":
    """Why this note fails the contract for its own type.

    Two types carry a requirement, and each requirement exists because the note
    is useless without it:

    * A DECISION without the rejected alternative does not stop the question
      being relitigated — the chosen path is already visible in the code; what
      is invisible is that the other option was considered and ruled out.
    * A GOTCHA without verbatim error text cannot be found. Retrieval is a
      literal string search, so a paraphrased or tidied error is unfindable by
      the person hitting it next.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ["unreadable"]

    kind = note_type(text)
    if kind not in ("decision", "gotcha"):
        return []

    body = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)
    problems = []

    if kind == "decision":
        m = re.search(r"^#{1,4}\s*(rejected|alternatives considered|options rejected)\b(.*?)"
                      r"(?=^#{1,4}\s|\Z)", body, re.S | re.M | re.I)
        if not m:
            problems.append("no 'Rejected' section — a decision without the "
                            "alternative that lost is not a decision")
        else:
            # A heading with nothing under it, or a table that is only its own
            # header, is the same failure wearing the right hat. Drop the header
            # and separator rows before measuring — "| Option | Why not |" is
            # the template, not an answer.
            lines = []
            seen_sep = False
            for ln in m.group(2).splitlines():
                if re.match(r"^\s*\|[\s|:-]+\|\s*$", ln):
                    seen_sep = True
                    lines = []          # everything before the separator is the header
                    continue
                lines.append(ln)
            body_text = "\n".join(lines) if seen_sep else m.group(2)
            content = re.sub(r"[|\-\s:]", "", body_text)
            if len(content) < 12:
                problems.append("'Rejected' section is empty — name the "
                                "alternative and why it lost")

    if kind == "gotcha":
        fenced = re.findall(r"```.*?\n(.*?)```", body, re.S)
        if not any(len(b.strip()) >= 8 for b in fenced):
            problems.append("no verbatim error text in a fenced block — "
                            "retrieval is a literal string search")

    return problems


def iter_notes(book: Path):
    if not book.is_dir():
        return
    for f in sorted(book.rglob("*.md")):
        if f.is_file() and not f.name.startswith("."):
            yield f


def cmd_lint(args) -> int:
    """Check every note in the book against the contract for its type."""
    start = Path(args.path).expanduser().resolve()
    root = repo_root(start) or start
    settings, ok = read_settings(root)
    b = bound_dir(settings) if ok else None
    book = Path(b) if b else None
    if args.book:
        book = Path(args.book).expanduser()
    if not book or not book.is_dir():
        out.err("no book found — run `commonbook bind` first, or pass --book")
        return 2

    counts, bad = {}, []
    for f in iter_notes(book):
        try:
            kind = note_type(f.read_text(encoding="utf-8", errors="replace")) or "untyped"
        except OSError:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        for why in contract_violations(f):
            bad.append((f, why))

    out.say(out.bold(f"contracts in {book}"))
    out.say()
    for kind, n in sorted(counts.items()):
        out.say(f"  {kind:<12} {n}")
    out.say()

    if not bad:
        out.say(out.green("every typed note meets its contract"))
        return 0

    for f, why in bad:
        try:
            rel = f.relative_to(book)
        except ValueError:
            rel = f
        out.say(f"  {out.yellow('x')} {str(rel)[:48]:<48} {why}")
    out.say()
    out.say(out.yellow(f"{len(bad)} violation(s)"))
    return 1


def cmd_caps(args) -> int:
    """Machine-readable capability probe, for skills to branch on."""
    start = Path(args.path).expanduser().resolve()
    root = repo_root(start)
    book = None
    if root:
        settings, ok = read_settings(root)
        if ok:
            book = bound_dir(settings)
    print(json.dumps({
        "version": __version__,
        "repo": str(root) if root else None,
        "book": book,
        "bound": bool(book),
        "orphans": len([s for s in scan_stores() if not s["alive"]]),
    }))
    return 0


# ─────────────────────────────────────────────────────────────────── cli ──

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="commonbook",
        description="Bind a repo's Claude Code memory to its git identity.",
    )
    ap.add_argument("--version", action="version", version=f"commonbook {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("path", nargs="?", default=".", help="repo directory (default: cwd)")
        p.add_argument("--vault", metavar="DIR",
                       help="store books under DIR instead of ~/.claude/commonbook")

    p = sub.add_parser("bind", help="point this repo's memory at its book")
    common(p)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_bind)

    p = sub.add_parser("adopt", help="find orphaned memory and bring it into the book")
    p.add_argument("--vault", metavar="DIR")
    p.add_argument("--search", default=str(HOME), metavar="DIR",
                   help="where to look for live repos (default: home)")
    p.add_argument("--depth", type=int, default=5, metavar="N",
                   help="how deep to search (default: 5)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-y", "--yes", action="store_true", help="do not prompt")
    p.set_defaults(func=cmd_adopt)

    p = sub.add_parser("doctor", help="report what is bound, unbound or orphaned")
    common(p)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("lint", help="check notes against the contract for their type")
    common(p)
    p.add_argument("--book", metavar="DIR", help="check this directory instead")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("prune", help="delete orphan stores already copied into a book")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-y", "--yes", action="store_true", help="do not prompt")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("caps", help="machine-readable state, for skills")
    common(p)
    p.set_defaults(func=cmd_caps)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
