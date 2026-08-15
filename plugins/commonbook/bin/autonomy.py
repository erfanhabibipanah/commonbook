#!/usr/bin/env python3
"""Write-authority tiers for unattended work, and the invariants that check them.

An agent that can write to a knowledge base unattended will, over months, produce
drift, duplication, confidently-wrong notes and unbounded growth. None of those
announce themselves. The failure is not a crash — it is a run that reports
success while the corpus quietly gets worse.

So authority is explicit and graduated, and the default is the lowest one:

  0 read-only   observe and report. Cannot write. The default.
  1 propose     write proposals to a review directory. Nothing lands until a
                human moves it. This is where a note-writing agent belongs.
  2 marked      write, but only inside generated markers in files it owns, so a
                diff shows exactly what changed and hand-written text is never
                touched.
  3 full        unrestricted writes. Never the default, and never implied by a
                lower tier being comfortable.

The reason tier 1 exists at all is that "propose, human approves" is the only
model that survives being wrong. A tier-3 agent that misunderstands a convention
writes forty notes before anyone notices.

    autonomy.py show              current tier and what it permits
    autonomy.py set <tier>        change it (writes .commonbook/autonomy.json)
    autonomy.py check             assert the invariants; exit non-zero on failure
    autonomy.py review            list pending proposals

The check subcommand is the point. A tier is a claim; the invariants are what
make it true.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

def plural(n: int, one: str, many: "str | None" = None) -> str:
    """"1 note" / "2 notes". Written out rather than "note(s)" because a public
    tool is judged on its first screen, and (s) reads as unfinished."""
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


TIERS = {
    0: ("read-only", "observe and report; no writes at all"),
    1: ("propose", "write proposals to review/; nothing lands unreviewed"),
    2: ("marked", "write inside generated markers only"),
    3: ("full", "unrestricted writes"),
}

CONFIG_REL = ".commonbook/autonomy.json"
REVIEW_REL = ".commonbook/review"

# A proposal older than this has stopped being a proposal and become a backlog.
# Unreviewed work piling up is the signal that tier 1 is not actually being used
# as a review gate — which means the safety property is nominal, not real.
STALE_PROPOSAL_DAYS = 14

# Growth beyond this in one window is not authorship, it is a loop. The number is
# deliberately generous: the check exists to catch runaway, not to police a
# productive week.
MAX_NOTES_PER_DAY = 40


def find_root(start: Path) -> Path:
    cur = start.resolve()
    for _ in range(6):
        if (cur / ".git").exists() or (cur / ".commonbook").is_dir():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return start.resolve()


def load_config(root: Path) -> dict:
    p = root / CONFIG_REL
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(root: Path, cfg: dict) -> None:
    p = root / CONFIG_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def current_tier(root: Path) -> int:
    cfg = load_config(root)
    t = cfg.get("tier", 0)
    return t if isinstance(t, int) and t in TIERS else 0


def cmd_show(a) -> int:
    root = find_root(Path(a.path).expanduser())
    tier = current_tier(root)
    name, desc = TIERS[tier]
    cfg = load_config(root)

    print(f"  root      {root}")
    print(f"  tier      {tier} · {name}")
    print(f"            {desc}")
    if cfg.get("setAt"):
        print(f"  set       {time.strftime('%Y-%m-%d %H:%M', time.localtime(cfg['setAt']))}")
    if cfg.get("reason"):
        print(f"  reason    {cfg['reason']}")

    pending = list((root / REVIEW_REL).glob("*.md")) if (root / REVIEW_REL).is_dir() else []
    print(f"  pending   " + plural(len(pending), "proposal") + " awaiting review")
    if tier == 0:
        print()
        print("  Nothing writes at this tier. Raise it deliberately when you want")
        print("  proposals: autonomy.py set 1")
    return 0


def cmd_set(a) -> int:
    root = find_root(Path(a.path).expanduser())
    if a.tier not in TIERS:
        print(f"  unknown tier {a.tier} — choose from {sorted(TIERS)}", file=sys.stderr)
        return 2

    # Tier 3 is the one that can quietly ruin a corpus, so it costs a sentence.
    # Requiring a reason is not bureaucracy: it is the thing you will read in six
    # months when you are trying to remember why writes are unrestricted.
    if a.tier == 3 and not a.reason:
        print("  tier 3 grants unrestricted writes to an unattended process.",
              file=sys.stderr)
        print("  Pass --reason to record why. It will be the only explanation",
              file=sys.stderr)
        print("  anyone gets later.", file=sys.stderr)
        return 2

    cfg = load_config(root)
    was = cfg.get("tier", 0)
    cfg.update({"tier": a.tier, "setAt": int(time.time())})
    if a.reason:
        cfg["reason"] = a.reason
    save_config(root, cfg)
    print(f"  {TIERS[was][0]} -> {TIERS[a.tier][0]}  ({root / CONFIG_REL})")
    return 0


def cmd_review(a) -> int:
    root = find_root(Path(a.path).expanduser())
    d = root / REVIEW_REL
    items = sorted(d.glob("*.md")) if d.is_dir() else []
    if not items:
        print("  no proposals pending")
        return 0
    now = time.time()
    print(f"  " + plural(len(items), "proposal") + f" in {d}")
    print()
    for f in items:
        try:
            age = int((now - f.stat().st_mtime) // 86400)
        except OSError:
            age = -1
        first = ""
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("# "):
                    first = line[2:].strip()
                    break
        except OSError:
            pass
        flag = "  (stale)" if age >= STALE_PROPOSAL_DAYS else ""
        print(f"    {age:>3}d  {f.name[:40]:<40} {first[:36]}{flag}")
    return 0


def _notes_by_day(book: Path) -> "dict[str, int]":
    counts: "dict[str, int]" = {}
    if not book.is_dir():
        return counts
    for f in book.rglob("*.md"):
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(f.stat().st_mtime))
        except OSError:
            continue
        counts[day] = counts.get(day, 0) + 1
    return counts


def cmd_check(a) -> int:
    """Assert the invariants. A tier is a claim; this is what makes it true."""
    root = find_root(Path(a.path).expanduser())
    tier = current_tier(root)
    book = Path(a.book).expanduser() if a.book else None
    problems, notes = [], []

    print(f"  tier      {tier} · {TIERS[tier][0]}")

    # 1. A read-only tier that has a review backlog is not read-only. Something
    #    wrote, which means the tier is not describing reality.
    review = root / REVIEW_REL
    pending = sorted(review.glob("*.md")) if review.is_dir() else []
    if tier == 0 and pending:
        problems.append("tier 0 declares no writes, but " + plural(len(pending), "proposal") + " exist")

    # 2. Proposals that nobody reviews mean the gate is nominal. The safety
    #    property of tier 1 is the human, and an untouched queue says there is
    #    none.
    now = time.time()
    stale = [f for f in pending
             if (now - f.stat().st_mtime) > STALE_PROPOSAL_DAYS * 86400]
    if stale:
        problems.append(plural(len(stale), "proposal") + f" older than {STALE_PROPOSAL_DAYS} days — "
                        "the review gate is not being used")

    # 3. Runaway growth. Authorship has a human rhythm; a loop does not.
    if book:
        per_day = _notes_by_day(book)
        spikes = {d: n for d, n in per_day.items() if n > MAX_NOTES_PER_DAY}
        if spikes:
            worst = max(spikes.items(), key=lambda kv: kv[1])
            problems.append(f"{worst[1]} notes written on {worst[0]} "
                            f"(limit {MAX_NOTES_PER_DAY}) — looks like a loop, not authorship")
        total = sum(per_day.values())
        notes.append(f"book has " + plural(total, "note") + " across " + plural(len(per_day), "day"))

    # 4. Tier 3 with no recorded reason is the state nobody can explain later.
    cfg = load_config(root)
    if tier == 3 and not cfg.get("reason"):
        problems.append("tier 3 is set with no recorded reason")

    # 5. A config that exists but does not parse means the tier is silently 0
    #    while the operator believes otherwise — the worst kind of wrong.
    cfgfile = root / CONFIG_REL
    if cfgfile.is_file() and not load_config(root):
        problems.append(f"{CONFIG_REL} exists but does not parse — "
                        "the effective tier is 0, not what it says")

    for n in notes:
        print(f"  note      {n}")
    print(f"  pending   " + plural(len(pending), "proposal"))
    print()

    if not problems:
        print("  all invariants hold")
        return 0
    for p in problems:
        print(f"  FAIL      {p}")
    print()
    print("  " + plural(len(problems), "invariant") + " violated")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog=os.environ.get("COMMONBOOK_PROG", "autonomy.py"), description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("path", nargs="?", default=".")

    p = sub.add_parser("show"); common(p); p.set_defaults(func=cmd_show)
    p = sub.add_parser("review"); common(p); p.set_defaults(func=cmd_review)

    p = sub.add_parser("set")
    p.add_argument("tier", type=int, choices=sorted(TIERS))
    p.add_argument("--reason")
    common(p); p.set_defaults(func=cmd_set)

    p = sub.add_parser("check")
    p.add_argument("--book", help="also check this book for runaway growth")
    common(p); p.set_defaults(func=cmd_check)

    a = ap.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
