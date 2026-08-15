#!/usr/bin/env python3
"""Adapter for an external code-graph tool. Probe, report, route — never vendor.

Commonbook does not build code graphs and should not. graphify already does it,
and it installs its own skill file into the agent's config directory at install
time — so vendoring it would fork the very skill it maintains for itself. This
adapter only answers three questions the graph tool does not answer itself:

  * is a graph present for this repo, and is it STALE?
    graph.json records built_at_commit. Comparing that to HEAD is the difference
    between an answer and a confidently wrong answer, and nothing surfaces it.
  * does the file actually match the schema we expect?
    community, _origin and _callable are undocumented internals. Reading them
    without asserting they exist turns an upstream format change into a wrong
    answer instead of an error.
  * what should happen when no graph exists?
    Say so plainly and fall back to grep. Never auto-install anything.

    graph.py status        is a graph present, valid, current
    graph.py scope         which directory this repo's graph covers

Absent tool or absent graph is a normal state, not an error: exit 0 with a clear
message, so a caller can branch without parsing prose.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# The keys we actually read. Asserted rather than assumed, because an upstream
# rename would otherwise degrade silently into wrong answers.
REQUIRED_TOP = {"nodes", "links"}
OPTIONAL_TOP = {"built_at_commit", "directed", "multigraph", "hyperedges", "graph"}
REQUIRED_NODE = {"id", "label"}

def plural(n: int, one: str, many: "str | None" = None) -> str:
    """"1 note" / "2 notes". Written out rather than "note(s)" because a public
    tool is judged on its first screen, and (s) reads as unfinished."""
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


SCOPE_MARKER = ".commonbook/graph-scope"


def git(*args, cwd=None):
    try:
        p = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None,
                           capture_output=True, text=True, timeout=10)
        return p.stdout.strip(), p.returncode
    except (OSError, subprocess.SubprocessError):
        return "", 1


def repo_root(start: Path) -> "Path | None":
    common, rc = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=start)
    if rc != 0 or not common:
        return None
    p = Path(common)
    return p.parent if p.name == ".git" else p


def graph_scope(start: Path) -> "tuple[Path, str]":
    """Which directory the graph covers.

    Defaults to the repo. A marker file widens it to a product directory holding
    several repos that genuinely share an interface — and because the search
    stops at the first marker found walking up, the scope can never silently
    expand to a whole workspace, where a graph would be meaningless: unrelated
    repos share no imports, so clustering returns one community per repo, which
    is just the directory names again.
    """
    root = repo_root(start) or start
    cur = root
    for _ in range(4):
        if (cur / SCOPE_MARKER).is_file():
            return cur, "marker"
        if cur.parent == cur:
            break
        cur = cur.parent
    return root, "repo"


def find_graph(scope: Path) -> "Path | None":
    p = scope / "graphify-out" / "graph.json"
    return p if p.is_file() else None


def validate(path: Path) -> "tuple[dict | None, list[str]]":
    """Load the graph and check the shape we depend on."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"unreadable: {exc.__class__.__name__}"]

    problems = []
    missing = REQUIRED_TOP - set(data)
    if missing:
        problems.append(f"missing top-level key(s): {', '.join(sorted(missing))}")
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        problems.append("nodes is not a list")
    elif nodes:
        gaps = REQUIRED_NODE - set(nodes[0])
        if gaps:
            problems.append(f"node missing: {', '.join(sorted(gaps))}")
    return data, problems


def cmd_status(a) -> int:
    start = Path(a.path).expanduser().resolve()
    scope, how = graph_scope(start)
    tool = shutil.which("graphify")

    print(f"  scope     {scope}  ({how})")
    print(f"  tool      {tool or 'not installed'}")

    gp = find_graph(scope)
    if not gp:
        print("  graph     none")
        print()
        print("  No code graph for this scope. Answer from `git grep` and say so —")
        if not tool:
            print("  the graph tool is not installed, and this will not install it.")
        else:
            print("  build one with the graph tool if you want structural answers.")
        return 0

    data, problems = validate(gp)
    size = gp.stat().st_size
    print(f"  graph     {gp}  ({size:,} B)")

    if data:
        n, e = len(data.get("nodes") or []), len(data.get("links") or [])
        print(f"  size      {n:,} nodes, {e:,} edges")

        built = data.get("built_at_commit")
        if built:
            head, rc = git("rev-parse", "HEAD", cwd=scope)
            if rc == 0 and head:
                if head.startswith(built) or built.startswith(head[:12]):
                    print(f"  freshness current ({built[:12]})")
                else:
                    behind, rc2 = git("rev-list", "--count", f"{built}..HEAD", cwd=scope)
                    n_behind = behind if rc2 == 0 and behind.isdigit() else "?"
                    print(f"  freshness STALE — built at {built[:12]}, "
                          f"HEAD is {n_behind} commit(s) later")
                    print("            structural answers may describe code that has changed")
        else:
            print("  freshness unknown — no built_at_commit recorded")

    if problems:
        print()
        for p in problems:
            print(f"  ! schema  {p}")
        print("  The graph format has changed. Treat structural answers as unreliable")
        print("  until this adapter is updated — a wrong answer is worse than none.")
        return 1

    return 0


def cmd_scope(a) -> int:
    scope, how = graph_scope(Path(a.path).expanduser().resolve())
    print(json.dumps({"scope": str(scope), "how": how,
                      "graph": str(find_graph(scope) or ""),
                      "tool": shutil.which("graphify") or ""}))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=os.environ.get("COMMONBOOK_PROG", "graph.py"), description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("status", cmd_status), ("scope", cmd_scope)):
        p = sub.add_parser(name)
        p.add_argument("path", nargs="?", default=".")
        p.set_defaults(func=fn)
    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
