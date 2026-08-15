#!/usr/bin/env python3
"""One JSON document describing the memory state of a machine.

Every other command here answers one question about one repo and prints prose.
That is right at a terminal and useless to anything else, because the state
that matters is spread across three places that are never seen together: a
settings file inside each repo, a store per filesystem path under the agent's
config directory, and a book per git identity. On the laptop this was written
on that is 55 repositories, 25 books and 366 notes — of which an aggregate
index carries 151 before the 200-line / 25,000-byte session-load cap starts
dropping the rest.

So this collects the three and emits data. It renders nothing: no HTML, no
chart, no colour. Coupling the collection to a renderer is the mistake to
avoid, because the first honest picture of a real machine is mostly a picture
of mess — duplicate directory names, notes with no frontmatter, books whose
repo was deleted months ago — and a renderer that must be edited before a new
kind of mess can appear is a renderer that will quietly drop it.

Which is the rule the whole document is built on. Anything skipped, dropped,
ambiguous or unreadable lands in ``warnings`` with a count, and the totals say
how many. A view that omits things while looking complete is worse than one
that admits it is partial.

Nothing is written except the file named by --out. Every fact in here is read.

    view.py              print the document to stdout
    view.py --out F      write it to F

The document names local paths and git remote URLs, because that is what the
state IS. It describes the machine it was generated on. Do not publish one
without reading it.

Python 3.9+.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

def plural(n: int, one: str, many: "str | None" = None) -> str:
    """"1 note" / "2 notes". Written out rather than "note(s)" because a public
    tool is judged on its first screen, and (s) reads as unfinished."""
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


SCHEMA = "commonbook.view/1"


def _sibling(name: str):
    """Load a module that lives beside this file.

    ``bin/`` is not a package and is not on sys.path when the file is run
    directly, so a plain ``import commonbook`` fails from here. It would also
    trip the stdlib-only import check in CI, which reads the source and has no
    way to tell a local file from a dependency. importlib answers both, and
    keeps identity, store scanning and frontmatter parsing in the one place
    each of them already lives.
    """
    path = Path(__file__).resolve().parent / (name + ".py")
    spec = importlib.util.spec_from_file_location("_commonbook_" + name, path)
    if spec is None or spec.loader is None:          # pragma: no cover
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cb = _sibling("commonbook")
agg = _sibling("aggregate")

SETTINGS_REL = ".claude/settings.local.json"

# The five states a repo can be in, as the renderer will draw them.
BOUND = "bound"
UNBOUND = "unbound"
ORPHANED = "orphaned"
NO_IDENTITY = "no-stable-identity"
REFUSED = "refused"

# Warning kinds, named rather than spelled out at the call site so a renderer
# can group on them and a test can assert one fired without matching prose.
W_SEARCH_ROOT_MISSING = "search-root-missing"
W_NO_REPOS = "no-repos-found"
W_DUPLICATE_SLUG = "duplicate-slug"
W_SETTINGS_UNPARSEABLE = "settings-unparseable"
W_SETTINGS_COMMITTABLE = "settings-committable"
W_NO_STABLE_IDENTITY = "no-stable-identity"
W_BOUND_ELSEWHERE = "bound-elsewhere"
W_BOUND_BOOK_MISSING = "bound-book-missing"
W_BOOK_OUTSIDE_ROOT = "book-outside-books-root"
W_BOOK_WITHOUT_REPO = "book-without-repo"
W_BOOK_EMPTY = "book-empty"
W_NOTE_NO_FRONTMATTER = "note-without-frontmatter"
W_NOTES_SKIPPED = "notes-skipped"
W_BOOK_INDEX_OVER_CAP = "book-index-over-cap"
W_INDEX_OVER_CAP = "index-over-cap"
W_ORPHAN_UNMATCHED = "orphan-unmatched"
W_STORE_NO_CWD = "store-without-cwd"
W_TOPIC_DRIFT = "topic-definition-drift"
# A repo can own several stores: Claude Code keys one per cwd, so a session
# started in packages/api gets its own. Keeping only one and dropping the rest
# is the silent omission this document exists to make impossible.
W_STORE_OUTSIDE_ROOT = "store-outside-search-root"
# One unreadable directory must not take the whole document down. A view that
# exits 1 with empty stdout tells the operator nothing about the 40 books that
# were fine.
W_BOOK_UNREADABLE = "book-unreadable"
W_BOOKS_ROOT_MISSING = "books-root-missing"


# ─────────────────────────────────────────────────────────────── warnings ──

class Warnings:
    """Collected into the document, never printed as they are found.

    A warning is a claim about the scan, so it has to travel with the scan.
    Anything written to the terminal while the document is being piped into a
    file is a finding nobody reads.
    """

    def __init__(self) -> None:
        self._seen: "dict[tuple, None]" = {}

    def add(self, kind: str, subject, detail: str) -> None:
        self._seen[(kind, str(subject), detail)] = None

    def kinds(self) -> "set[str]":
        return {k for k, _, _ in self._seen}

    def dump(self) -> "list[dict]":
        # Sorted so two runs over an unchanged machine produce the same bytes.
        return [{"kind": k, "subject": s, "detail": d}
                for k, s, d in sorted(self._seen)]

    def __len__(self) -> int:
        return len(self._seen)


# ─────────────────────────────────────────────────────────────── helpers ──

def iso(ts: "float | None") -> "str | None":
    """UTC to the second.

    Local time would shift every timestamp in the document either side of a
    clock change, making two otherwise identical scans diff as if the whole
    corpus had moved.
    """
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def under(child: Path, parent: Path) -> bool:
    """Is child inside parent?

    Both sides are canonicalised first. On macOS the temporary and per-user
    directories sit behind symlinks — /var is /private/var — so one side
    spelled by a settings file and the other by a scan compare unequal while
    naming the same directory, and every book looks like it is somewhere else.
    Case-insensitively too, because that filesystem is.
    """
    try:
        child, parent = child.resolve(), parent.resolve()
    except OSError:                                  # pragma: no cover
        pass
    c, p = str(child).casefold(), str(parent).casefold()
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def note_shape(path: Path) -> "tuple[bool | None, str | None]":
    """(has_frontmatter, declared type) for one note; (None, None) if unreadable.

    Read again rather than inferred from what ``scan_books`` returns: there, a
    note with no frontmatter and a note whose frontmatter carries none of the
    keys ranking reads collapse to the same empty dict, and only the first is
    worth reporting. "Has frontmatter" is taken as "the frontmatter says
    something" — an empty block carries no more than no block at all.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, None
    return bool(agg.frontmatter(text)), cb.note_type(text)


def book_notes_dir(book: Path) -> Path:
    """The directory ``scan_books`` reads a book's notes from.

    ``memory/`` when it exists, the book itself otherwise. Restated here so the
    candidate count below measures the same set — a book laid out as a vault
    keeps hub pages and other prose beside ``memory/``, and counting those as
    missing notes would fire a warning on every well-organised book.
    """
    try:
        mem = book / "memory"
        return mem if mem.is_dir() else book
    except OSError:
        return book

def index_file(book: Path) -> "Path | None":
    """A book's own session-load index, if it has one.

    Claude Code reads MEMORY.md at session start; the notes it names are not
    loaded until something opens them. ``scan_books`` skips it for exactly that
    reason, so it has to be found here.
    """
    try:
        candidate = book_notes_dir(book) / "MEMORY.md"
        return candidate if candidate.is_file() else None
    except OSError:
        return None

def measure_index(path: Path) -> "dict | None":
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    n_bytes, n_lines = len(text.encode("utf-8")), len(text.splitlines())
    return {
        "path": str(path),
        "bytes": n_bytes,
        "lines": n_lines,
        "overCap": n_bytes > agg.BYTE_BUDGET or n_lines > agg.LINE_BUDGET,
    }


# ────────────────────────────────────────────────────────────────── repos ──

def canonical_repos(search: Path, depth: int) -> "dict[str, dict]":
    """Checkouts found under the search root, collapsed onto their main worktree.

    ``discover_live`` stops at the first ``.git`` it meets and a linked worktree
    has one — a file rather than a directory, but it exists. Left alone that
    reports one repo per branch checked out, so a machine holding a dozen
    worktrees of one project looks like a dozen projects, all sharing a book
    and all apparently duplicate names. ``repo_root`` maps every checkout back
    to the canonical root, which is the same key the memory store uses.
    """
    groups: "dict[str, dict]" = {}
    for path in sorted(cb.discover_live(search, depth)[0].values(), key=str):
        root = cb.repo_root(path) or path
        entry = groups.setdefault(str(root), {"root": root, "checkouts": []})
        # Resolved, not spelled: git reports an absolute canonical path, the walk
        # reports whatever the search root was spelled as, and on a machine where
        # either sits behind a symlink the main checkout would list itself as one
        # of its own worktrees.
        if path.resolve() != root.resolve():
            entry["checkouts"].append(str(path))
    for entry in groups.values():
        entry["checkouts"].sort()
    return groups


def owning_repo(cwd: str, keys: "list[str]") -> "str | None":
    """The repo a live store belongs to: the longest path that contains its cwd.

    A session can start in a subdirectory, so an exact match is not enough —
    but the deepest containing repo is the one whose memory it is.
    """
    best = None
    for key in keys:
        if under(Path(cwd), Path(key)) and (best is None or len(key) > len(best)):
            best = key
    return best


def binding_state(parseable, tracked, skipped, kind, bound, orphan_count) -> "tuple[str, str]":
    """One state per repo, most urgent first.

    The order is deliberate. ``refused`` leads because nothing else about the
    repo can be fixed until it is cleared. ``orphaned`` comes next as the only
    state where notes already exist and cannot be read — everything below it
    predicts a future loss rather than reporting one. Then the worse of the two
    predictions: a path-keyed book orphans again on the very next move even
    after binding, whereas an unbound repo is fine until someone moves it.
    """
    if not parseable:
        return REFUSED, ("settings.local.json is not valid JSON — bind will not "
                         "overwrite it, so this repo cannot be bound as it stands")
    if tracked and not skipped:
        return REFUSED, ("settings.local.json is tracked by git and skip-worktree "
                         "is not set — a binding here would commit a local absolute path")
    if orphan_count:
        return ORPHANED, (f"{orphan_count} store(s) hold notes written under a path "
                          "this repo no longer occupies")
    if kind == "path":
        return NO_IDENTITY, ("no git remote and no commits — a book here is keyed on "
                             "the path and orphans again on the next move")
    if not bound:
        return UNBOUND, ("no autoMemoryDirectory — memory is keyed on this path and "
                         "orphans when the directory moves")
    return BOUND, "memory is addressed by configuration and survives a move"


def repo_entry(root: Path, checkouts, books_root: Path, vault_arg,
               orphans_here, live_stores, warn: Warnings) -> dict:
    book_id, kind, human = cb.identity(root)
    expected = cb.resolve_target(root, book_id, vault_arg)
    settings, parseable = cb.read_settings(root)
    bound = settings.get("autoMemoryDirectory") if parseable else None

    tracked = cb.is_tracked(root, SETTINGS_REL)
    skipped = False
    if tracked:
        # Read the flag; never set it. `bind` sets skip-worktree, this does not
        # — the whole document has to be reproducible without changing anything.
        flags, rc = cb.git("ls-files", "-v", SETTINGS_REL, cwd=root)
        skipped = rc == 0 and flags.startswith("S ")

    state, why = binding_state(parseable, tracked, skipped, kind, bound,
                               len(orphans_here))

    if not parseable:
        warn.add(W_SETTINGS_UNPARSEABLE, root, "settings.local.json does not parse as JSON")
    if tracked and not skipped:
        warn.add(W_SETTINGS_COMMITTABLE, root,
                 "settings.local.json is tracked and skip-worktree is not set — "
                 "a binding would be committed into everyone else's clone")
    if kind == "path":
        warn.add(W_NO_STABLE_IDENTITY, root,
                 "no remote and no commits — any book here is keyed on the path")

    book_notes, book_bytes = 0, 0
    if bound:
        book_path = Path(bound)
        if not book_path.is_dir():
            warn.add(W_BOUND_BOOK_MISSING, root,
                     f"bound to {bound}, which does not exist — nothing has been "
                     "written there yet, or the book has been deleted")
        else:
            # The same set the books section counts. MEMORY.md is the index
            # rather than a note, and is measured beside the book, not in it.
            notes = [p for p in cb.notes_in(book_path)
                     if p.name != "MEMORY.md" and not p.name.startswith(".")]
            book_notes, book_bytes = len(notes), sum(cb._size(p) for p in notes)
        # Canonicalised before comparing, for the same reason under() does it:
        # two spellings of one directory are not two directories.
        if book_path.resolve() != Path(expected).resolve():
            warn.add(W_BOUND_ELSEWHERE, root,
                     f"bound to {bound}, but this identity resolves to {expected}")
        if not under(book_path, books_root):
            warn.add(W_BOOK_OUTSIDE_ROOT, root,
                     f"bound to {bound}, outside the books root being scanned — "
                     "its notes are not counted here; re-run with --books pointing at it")

    return {
        "path": str(root),
        "name": root.name,
        "state": state,
        "why": why,
        "book": bound,
        "expectedBook": str(expected),
        "bookId": book_id,
        "identity": {"kind": kind, "value": human},
        "checkouts": checkouts,
        "notes": book_notes,
        "bytes": book_bytes,
        "settings": {
            "parseable": parseable,
            "trackedByGit": tracked,
            "skipWorktree": skipped,
        },
        # Where this repo's memory lands today if it is not bound: the store
        # keyed on the current path, which is the thing that orphans on a move.
        "pathStores": live_stores,
        "orphanStores": sorted(orphans_here),
    }


# ────────────────────────────────────────────────────────────────── books ──

def book_entries(books_root: Path, notes: "list[dict]", warn: Warnings) -> "list[dict]":
    """One entry per book directory, whether or not it holds notes.

    Enumerated from the directory rather than from the notes, because a book
    with nothing in it is itself a finding — a bind that never got written to,
    or a project whose notes were moved out and not cleaned up.
    """
    by_book: "dict[str, list[dict]]" = {}
    for n in notes:
        by_book.setdefault(n["book"], []).append(n)

    # A missing search root warns; a missing or unreadable books root must too,
    # or the default invocation reads as "no books, all clean" when what it
    # means is "looked in the wrong place". Silence here is the exact failure
    # this document exists to prevent.
    if not books_root.is_dir():
        warn.add(W_BOOKS_ROOT_MISSING, str(books_root),
                 "books root does not exist — pass --books to point at your books")
        dirs = []
    else:
        try:
            dirs = sorted(p for p in books_root.iterdir() if p.is_dir())
        except OSError as exc:
            warn.add(W_BOOKS_ROOT_MISSING, str(books_root),
                     f"books root is unreadable ({exc.__class__.__name__})")
            dirs = []

    entries = []
    for book in dirs:
        mine = by_book.get(book.name, [])
        sizes = [cb._size(n["path"]) for n in mine]
        mtimes = [n["mtime"] for n in mine]

        types: "dict[str, int]" = {}
        no_frontmatter, unreadable = [], []
        for n in mine:
            has_fm, kind = note_shape(n["path"])
            if has_fm is None:
                unreadable.append(n["path"].name)
                continue
            if not has_fm:
                no_frontmatter.append(n["path"].name)
            key = kind or "untyped"
            types[key] = types.get(key, 0) + 1

        if no_frontmatter:
            warn.add(W_NOTE_NO_FRONTMATTER, book.name,
                     f"{len(no_frontmatter)} note(s) carry no frontmatter, so they have "
                     "no type, no tags and nothing to rank on: "
                     + ", ".join(sorted(no_frontmatter)[:5])
                     + (" …" if len(no_frontmatter) > 5 else ""))
        if unreadable:
            warn.add(W_NOTES_SKIPPED, book.name,
                     f"{len(unreadable)} note(s) could not be read and are absent from "
                     "every count above: " + ", ".join(sorted(unreadable)[:5]))

        # The scan is the only thing that can notice its own gaps. scan_books
        # drops a note it cannot read and moves on, which is right for building
        # an index and wrong to leave unsaid — so count the candidates again and
        # report the difference rather than let it vanish.
        candidates = 0
        for f in book_notes_dir(book).rglob("*.md"):
            if f.is_file() and f.name != "MEMORY.md" and not f.name.startswith("."):
                candidates += 1
        if candidates > len(mine):
            warn.add(W_NOTES_SKIPPED, book.name,
                     f"{candidates - len(mine)} markdown file(s) under this book were "
                     "skipped while scanning and are in no count here")

        if not mine:
            warn.add(W_BOOK_EMPTY, book.name, "book directory holds no notes")

        idx = None
        found = index_file(book)
        if found:
            idx = measure_index(found)
            if idx and idx["overCap"]:
                warn.add(W_BOOK_INDEX_OVER_CAP, book.name,
                         f"MEMORY.md is {idx['bytes']:,} B / {idx['lines']} lines, over the "
                         f"{agg.BYTE_BUDGET:,} B / {agg.LINE_BUDGET} line session-load cap — "
                         "the overflow is dropped at load time without an error")

        entries.append({
            "id": book.name,
            "path": str(book),
            "notes": len(mine),
            "bytes": sum(sizes),
            "newest": iso(max(mtimes)) if mtimes else None,
            "oldest": iso(min(mtimes)) if mtimes else None,
            "types": {k: types[k] for k in sorted(types)},
            "withoutFrontmatter": len(no_frontmatter),
            "unreadable": len(unreadable),
            "index": idx,
            "repos": [],        # filled in by attach_books
        })
    return entries


def attach_books(books: "list[dict]", repos: "list[dict]", warn: Warnings) -> None:
    """Say which live repo each book belongs to, and how that was decided.

    Three ways, recorded rather than merged, because they carry different
    confidence. A repo bound to the book is a fact. A book named with the id
    this repo's identity resolves to is a fact one bind away. A book whose
    directory name matches a repo's directory name is a guess — the same
    basename heuristic ``adopt`` uses for orphans, and it is only taken when
    exactly one repo could answer to it, for the same reason.
    """
    by_name: "dict[str, list[dict]]" = {}
    for r in repos:
        by_name.setdefault(r["name"].casefold(), []).append(r)

    for b in books:
        matches: "dict[str, str]" = {}
        for r in repos:
            if r["book"] and under(Path(r["book"]), Path(b["path"])):
                matches[r["path"]] = "bound"
            elif r["bookId"] == b["id"]:
                matches.setdefault(r["path"], "expected-id")
        if not matches:
            named = by_name.get(b["id"].casefold(), [])
            if len(named) == 1:
                matches[named[0]["path"]] = "name"

        b["repos"] = [{"path": p, "how": matches[p]} for p in sorted(matches)]
        if not b["repos"]:
            warn.add(W_BOOK_WITHOUT_REPO, b["id"],
                     f"{b['notes']} note(s), {b['bytes']:,} B, and no live repository on "
                     "this machine binds to it or answers to its name")


# ───────────────────────────────────────────────────────────────── topics ──

def topic_map(notes: "list[dict]") -> "tuple[dict, dict, dict]":
    """(tag -> books, title -> books, topic -> note count).

    The same definition ``aggregate.rank`` scores on — a note's tags plus its
    lowercased title. It is rebuilt here because rank throws the map away once
    it has the count, and the map is the part a renderer needs: which books a
    topic spans, not how many. The two are cross-checked below, so if the
    upstream definition moves the document says so instead of quietly
    disagreeing with the index built from the same notes.
    """
    tags: "dict[str, set]" = {}
    titles: "dict[str, set]" = {}
    counts: "dict[str, int]" = {}
    for n in notes:
        for t in n["tags"]:
            tags.setdefault(t, set()).add(n["book"])
        titles.setdefault(n["title"].lower(), set()).add(n["book"])
        for t in set(n["tags"] + [n["title"].lower()]):
            counts[t] = counts.get(t, 0) + 1
    return tags, titles, counts


def cross_book_topics(notes: "list[dict]", warn: Warnings) -> "list[dict]":
    """Every tag or title that appears in more than one book.

    This is the only genuine cross-project structure in a corpus like this.
    Direct links between projects are almost always absent — nobody writing a
    note about one repo cites a note in another — so a graph of them is a graph
    of nothing. A shared topic is evidence that two projects met the same
    problem, and it falls out of what people already write.
    """
    tags, titles, counts = topic_map(notes)

    spans: "dict[str, set]" = {}
    for source in (tags, titles):
        for topic, books in source.items():
            spans.setdefault(topic, set()).update(books)

    for n in notes:
        # rank() computed the same number from the same notes. If it disagrees,
        # the two definitions have drifted apart and the index and this document
        # are describing different corpora.
        mine = max((len(spans.get(t, ())) for t in n["tags"] + [n["title"].lower()]),
                   default=1)
        if mine != n.get("_shared", mine):
            warn.add(W_TOPIC_DRIFT, n["book"],
                     "this module and aggregate.py no longer agree on what counts as a "
                     "shared topic; treat the topic list and the index as unrelated")
            break

    out = []
    for topic in sorted(spans):
        books = spans[topic]
        if len(books) < 2:
            continue
        in_tags, in_titles = topic in tags, topic in titles
        out.append({
            "topic": topic,
            "kind": "both" if in_tags and in_titles else ("tag" if in_tags else "title"),
            "books": sorted(books),
            "notes": counts.get(topic, 0),
        })
    out.sort(key=lambda t: (-len(t["books"]), -t["notes"], t["topic"]))
    return out


# ──────────────────────────────────────────────────────────────── document ──


def infer_books_root(roots) -> "tuple[Path, str]":
    """Where this machine actually keeps its books.

    Asking the user for --books is asking them to tell the tool something the
    tool can already see: most repos are bound, and their book paths share a
    parent. Guessing from that is right on any layout, including one this tool
    did not create. Falls back to the default only when nothing is bound yet.
    """
    parents: "dict[str, int]" = {}
    for root in roots:
        settings, ok = cb.read_settings(Path(root))
        if not ok:
            continue
        book = cb.bound_dir(settings)
        if not book:
            continue
        # <books>/<id>/memory -> <books>
        p = Path(book)
        anc = p.parent.parent if p.name == "memory" else p.parent
        parents[str(anc)] = parents.get(str(anc), 0) + 1
    if parents:
        best = max(parents.items(), key=lambda kv: kv[1])
        return Path(best[0]), f"inferred from {plural(best[1], 'bound repo')}"
    return cb.DEFAULT_BOOKS, "default"


def build(search: Path, depth: int, books_root: Path) -> dict:
    """Collect the whole state. Reads only."""
    warn = Warnings()

    if not search.is_dir():
        warn.add(W_SEARCH_ROOT_MISSING, search,
                 "the search root does not exist, so no repository was examined — "
                 "an empty result here means nothing was looked at, not that nothing is wrong")

    # bind writes resolve_target(root, id, None) when no vault was given, so
    # passing None back for the default keeps `expectedBook` byte-identical to
    # what a bind would actually put in the settings file. Everything else works
    # from the canonicalised path — see under().
    books_root = books_root.expanduser()
    vault_arg = None if books_root == cb.DEFAULT_BOOKS else str(books_root)
    books_root = books_root.resolve()

    groups = canonical_repos(search, depth)
    keys = sorted(groups)

    if search.is_dir() and not keys:
        warn.add(W_NO_REPOS, search,
                 f"no git repository found under this root at depth {depth} — widen "
                 "--search or raise --depth before reading anything into the totals")

    by_name: "dict[str, list[str]]" = {}
    for key in keys:
        by_name.setdefault(groups[key]["root"].name.casefold(), []).append(key)
    for name, sharing in sorted(by_name.items()):
        if len(sharing) > 1:
            warn.add(W_DUPLICATE_SLUG, name,
                     f"{len(sharing)} repositories share this directory name, so an "
                     "orphaned store naming it cannot be matched to one of them: "
                     + ", ".join(sharing))

    # ── stores: live ones belong to a repo, dead ones are the orphans
    stores = cb.scan_stores()
    live_by_repo: "dict[str, list]" = {}
    unattributed: "list[dict]" = []
    orphans, orphans_by_repo = [], {}
    roots = {k: groups[k]["root"] for k in keys}

    for s in stores:
        if s["alive"] and s["cwd"]:
            owner = owning_repo(s["cwd"], keys)
            entry = {
                "path": str(s["memory"]),
                "cwd": s["cwd"],
                "notes": len(s["notes"]),
                "bytes": s["bytes"],
            }
            if owner:
                # A list, not an assignment: a repo commonly owns several stores
                # and the previous shape kept whichever sorted last.
                live_by_repo.setdefault(owner, []).append(entry)
            else:
                # Its cwd exists but lies outside the search root, so no repo in
                # this document claims it. Record it rather than let it vanish.
                unattributed.append(entry)
                warn.add(W_STORE_OUTSIDE_ROOT, s["cwd"],
                         f"{len(s['notes'])} note(s), {s['bytes']} bytes in a live store "
                         f"outside the search root — widen --search to attribute it")
            continue

        match = cb.match_orphan(s["cwd"], roots) if s["cwd"] else None
        if match:
            orphans_by_repo.setdefault(str(match), []).append(str(s["memory"]))
        if not s["cwd"]:
            warn.add(W_STORE_NO_CWD, s["dir"].name,
                     f"{len(s['notes'])} note(s), {s['bytes']:,} B, and no transcript "
                     "recording a cwd — there is no way to tell whose memory this is")
        elif not s["adopted"] and not match:
            warn.add(W_ORPHAN_UNMATCHED, s["cwd"],
                     f"{len(s['notes'])} note(s), {s['bytes']:,} B unreachable, and no live "
                     "repository under the search root matches — widen --search, or the "
                     "project is gone and these notes are all that is left of it")
        orphans.append({
            "store": str(s["memory"]),
            "cwd": s["cwd"],
            "notes": len(s["notes"]),
            "bytes": s["bytes"],
            "match": str(match) if match else None,
            "adopted": s["adopted"],
        })
    orphans.sort(key=lambda o: (o["cwd"] or "", o["store"]))

    repos = [
        repo_entry(groups[k]["root"], groups[k]["checkouts"], books_root, vault_arg,
                   orphans_by_repo.get(k, []), live_by_repo.get(k, []), warn)
        for k in keys
    ]

    # ── books, and the index they would have to fit into together
    notes = agg.rank(agg.scan_books(books_root))
    books = book_entries(books_root, notes, warn)
    attach_books(books, repos, warn)
    topics = cross_book_topics(notes, warn)

    text, dropped = agg.render(notes, agg.BYTE_BUDGET) if notes else ("", 0)
    index = {
        "byteBudget": agg.BYTE_BUDGET,
        "lineBudget": agg.LINE_BUDGET,
        "bytes": len(text.encode("utf-8")),
        "lines": len(text.splitlines()),
        "candidates": len(notes),
        "indexed": len(notes) - dropped,
        "dropped": dropped,
    }
    if dropped:
        warn.add(W_INDEX_OVER_CAP, str(books_root),
                 f"{len(notes)} notes do not fit one index: {dropped} would be dropped at "
                 f"the {agg.LINE_BUDGET}-line / {agg.BYTE_BUDGET:,}-byte session-load cap")

    states = {s: 0 for s in (BOUND, UNBOUND, ORPHANED, NO_IDENTITY, REFUSED)}
    for r in repos:
        states[r["state"]] += 1

    return {
        "schema": SCHEMA,
        # The one field that changes between two scans of an unchanged machine.
        # Everything else is sorted so the rest of the document diffs cleanly.
        "generatedAt": iso(time.time()),
        "tool": f"commonbook {cb.__version__}",
        "scan": {
            "searchRoot": str(search),
            "depth": depth,
            "booksRoot": str(books_root),
            "projectsDir": str(cb.PROJECTS_DIR),
        },
        "totals": {
            "repos": len(repos),
            "checkouts": sum(len(r["checkouts"]) for r in repos),
            "states": states,
            "books": len(books),
            "notes": sum(b["notes"] for b in books),
            "bytes": sum(b["bytes"] for b in books),
            "emptyBooks": sum(1 for b in books if not b["notes"]),
            "stores": len(stores),
        "unattributedStores": len(unattributed),
            "orphanStores": sum(1 for o in orphans if not o["adopted"]),
            "rescuedStores": sum(1 for o in orphans if o["adopted"]),
            "orphanNotes": sum(o["notes"] for o in orphans if not o["adopted"]),
            "orphanBytes": sum(o["bytes"] for o in orphans if not o["adopted"]),
            "topics": len(topics),
            "warnings": len(warn),
        },
        "repos": repos,
        "books": books,
        "orphans": orphans,
        # Live stores that belong to no repo in this document. Listed so the
        # totals can be reconciled against the lists that produce them.
        "unattributed": sorted(unattributed, key=lambda e: e["cwd"]),
        "topics": topics,
        "index": index,
        "warnings": warn.dump(),
    }


def render(doc: dict) -> str:
    """The document as bytes, once, so stdout and --out cannot disagree."""
    return json.dumps(doc, indent=2) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=os.environ.get("COMMONBOOK_PROG", "view.py"), description=__doc__.split("\n")[0])
    ap.add_argument("--out", metavar="FILE", help="write the document here instead of stdout")
    ap.add_argument("--search", default=str(cb.HOME), metavar="DIR",
                    help="where to look for repositories (default: home)")
    ap.add_argument("--depth", type=int, default=5, metavar="N",
                    help="how deep to search (default: 5)")
    ap.add_argument("--books", "--vault", dest="books", default=None,
                    metavar="DIR", help="the directory holding books — the same one "
                                        "`bind --vault` writes to")
    a = ap.parse_args(argv)

    search = Path(a.search).expanduser().resolve()
    if a.books:
        books, how = Path(a.books).expanduser(), "given"
    else:
        # Infer rather than demand. The bindings already say where books live.
        roots, _ = cb.discover_live(search, a.depth)
        books, how = infer_books_root(roots)
    if how != "given":
        print(f"  books root: {cb.short(books)}  ({how})", file=sys.stderr)

    doc = build(search, a.depth, books)
    text = render(doc)

    if not a.out:
        sys.stdout.write(text)
        return 0

    try:
        cb.write_json_atomic(Path(a.out).expanduser(), doc)
    except OSError as exc:
        print(f"cannot write {a.out}: {exc}", file=sys.stderr)
        return 1

    t = doc["totals"]
    # Summary on stderr, so stdout is the document or nothing at all.
    print(f"  {t['repos']} repo(s) · {t['books']} book(s) · {t['notes']} note(s) · "
          f"{t['orphanStores']} orphan(s) · {t['warnings']} warning(s) -> {a.out}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
