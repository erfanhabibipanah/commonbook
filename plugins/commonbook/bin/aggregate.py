#!/usr/bin/env python3
"""Build one ranked, budgeted index across every book.

Claude Code's auto memory is per repository. There is no supported way to ask
"what do I know about Postgres across all my projects", and the obvious fix --
concatenating every book's index -- does not fit: on a real machine the indexes
total 85 KB against a session-load budget of roughly 25 KB. Three times over.

So this is a ranking problem, not a merge problem. Entries compete for a byte
budget, the ones that lose are DROPPED AND COUNTED, and the count is printed.
Silent truncation is the failure this whole tool exists to avoid: content past
the cap is dropped with no error, so an index that looks complete is not.

    aggregate.py                 write the index
    aggregate.py --dry-run       show what fits and what is dropped
    aggregate.py --budget 20000  smaller budget

Ranking, highest first:
  1. pinned      an entry whose note carries `pin: true`
  2. recent      touched in the last 30 days
  3. referenced  the same topic named in more than one book
  4. everything else, newest first
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

def plural(n: int, one: str, many: "str | None" = None) -> str:
    """"1 note" / "2 notes". Written out rather than "note(s)" because a public
    tool is judged on its first screen, and (s) reads as unfinished."""
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


HOME = Path.home()
CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude"))
DEFAULT_BOOKS = CLAUDE_DIR / "commonbook"

# The budget this index is built to fit.
#
# Claude Code truncates an index when it loads it, and everything past the limit
# is dropped with no error -- so the number matters and being wrong about it in
# the generous direction means reporting an index as complete when it is not.
#
# What is actually verified, by reading the shipped binary (2.1.233):
#   * the measurement is on RAW text. The reducer sums Buffer.byteLength per
#     line over the unmodified string, and the surrounding code destructures
#     `rawSizeBytes`. Frontmatter and comments are NOT stripped first.
#   * the truncation is line-first then byte, so a line budget alone is not
#     enough and a byte budget alone is not either.
# What is NOT verified: the exact limits. The constants near the memory
# telemetry path (30 lines / 65536 bytes) belong to `logMemoryPinWrite`, which
# truncates content for an analytics payload -- not to the session load. The
# real limits are behind `surfaceCap` / `spliceCap`, which could not be resolved
# from the binary with confidence.
#
# So these are a documented ASSUMPTION, overridable on the command line, rather
# than a claim. If your version differs, pass --budget and --line-budget; the
# drop count is reported either way, which is the part that does not depend on
# getting the number right.
BYTE_BUDGET = 25_000
LINE_BUDGET = 200
RECENT_DAYS = 30


def frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def title_of(path: Path, text: str, fm: dict) -> str:
    if fm.get("name"):
        return fm["name"].strip("'\"")
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.replace("-", " ").replace("_", " ")


def summarise(text: str, fm: dict) -> str:
    if fm.get("description"):
        return fm["description"].strip("'\"")
    body = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    for para in body.split("\n\n"):
        para = " ".join(para.split())
        if para and not para.startswith(("#", "-", "*", "|", ">")):
            return para
    return ""


def scan_books(root: Path) -> "list[dict]":
    """Every note in every book, with the signals ranking needs."""
    notes = []
    if not root.is_dir():
        return notes
    for book in sorted(p for p in root.iterdir() if p.is_dir()):
        mem = book / "memory" if (book / "memory").is_dir() else book
        for f in sorted(mem.rglob("*.md")):
            if f.name == "MEMORY.md" or f.name.startswith("."):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                mtime = f.stat().st_mtime
            except OSError:
                continue
            fm = frontmatter(text)
            notes.append({
                "book": book.name,
                "path": f,
                "title": title_of(f, text, fm),
                "summary": summarise(text, fm),
                "pinned": str(fm.get("pin", "")).lower() in ("true", "yes", "1"),
                "mtime": mtime,
                "tags": [t.strip().lower() for t in
                         re.sub(r"[\[\]]", "", fm.get("tags", "")).split(",") if t.strip()],
            })
    return notes


def rank(notes: "list[dict]") -> "list[dict]":
    now = time.time()
    # A topic named in more than one book is, by definition, the cross-project
    # knowledge this index exists to surface — so it outranks a local note.
    seen: "dict[str, set]" = {}
    for n in notes:
        for t in n["tags"] + [n["title"].lower()]:
            seen.setdefault(t, set()).add(n["book"])
    for n in notes:
        shared = max((len(seen.get(t, ())) for t in n["tags"] + [n["title"].lower()]),
                     default=1)
        recent = (now - n["mtime"]) < RECENT_DAYS * 86400
        n["_rank"] = (
            0 if n["pinned"] else 1,
            0 if shared > 1 else 1,
            0 if recent else 1,
            -n["mtime"],
        )
        n["_shared"] = shared
    return sorted(notes, key=lambda n: n["_rank"])


def render(notes: "list[dict]", budget: int, line_budget: int = LINE_BUDGET) -> "tuple[str, int]":
    header = [
        "# Index across all books",
        "",
        "One line per note. Detail lives in the file named on each line; those",
        "are not loaded until read.",
        "",
    ]
    lines, used, dropped = list(header), sum(len(l) + 1 for l in header), 0
    current_book = None

    # Reserve room for the overflow notice up front. Appending it afterwards
    # pushes the index past the very cap this function exists to respect — and
    # the line cap in particular is off-by-one sensitive, since content past it
    # is dropped silently at load time.
    RESERVE_LINES, RESERVE_BYTES = 2, 160
    budget -= RESERVE_BYTES
    line_cap = line_budget - RESERVE_LINES

    for n in notes:
        block = []
        if n["book"] != current_book:
            block.append("")
            block.append(f"## {n['book']}")
        rel = n["path"].name
        summary = n["summary"]
        if len(summary) > 96:
            summary = summary[:96].rsplit(" ", 1)[0] + "…"
        mark = " *" if n["pinned"] else ("" if n["_shared"] < 2 else " +")
        block.append(f"- [{n['title']}]({rel}){mark}"
                     + (f" — {summary}" if summary else ""))

        cost = sum(len(l) + 1 for l in block)
        if used + cost > budget or len(lines) + len(block) > line_cap:
            dropped += 1
            continue
        lines += block
        used += cost
        current_book = n["book"]

    if dropped:
        # Report the overflow. An index that silently omits entries reads as
        # complete, which is worse than one that admits it is partial.
        note = ["", f"_{dropped} further note(s) did not fit this index. "
                    f"Search the books directly, or raise the budget._"]
        lines += note
    return "\n".join(lines) + "\n", dropped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=os.environ.get("COMMONBOOK_PROG", "aggregate.py"), description=__doc__.split("\n")[0])
    ap.add_argument("--books", default=str(DEFAULT_BOOKS), help="directory holding books")
    ap.add_argument("--out", help="where to write (default: <books>/INDEX.md)")
    ap.add_argument("--budget", type=int, default=BYTE_BUDGET,
                    metavar="BYTES", help=f"byte budget (default {BYTE_BUDGET:,})")
    ap.add_argument("--line-budget", type=int, default=LINE_BUDGET,
                    metavar="N", help=f"line budget (default {LINE_BUDGET})")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    root = Path(a.books).expanduser()
    notes = scan_books(root)
    if not notes:
        print(f"no notes found under {root}")
        return 0

    ranked = rank(notes)
    text, dropped = render(ranked, a.budget, a.line_budget)
    kept = len(ranked) - dropped

    print(f"  books    {len({n['book'] for n in notes})}")
    print(f"  notes    {len(notes)} found, {kept} indexed, {dropped} dropped")
    print(f"  size     {len(text.encode()):,} B / {a.budget:,} B budget "
          f"({len(text.splitlines())} / {a.line_budget} lines)")
    shared = sum(1 for n in ranked if n["_shared"] > 1)
    print(f"  shared   {shared} note(s) whose topic appears in more than one book")

    if a.dry_run:
        print("\n  dry run — nothing written")
        return 0

    out = Path(a.out).expanduser() if a.out else root / "INDEX.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, out)
    print(f"\n  wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
