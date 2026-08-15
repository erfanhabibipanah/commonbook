#!/usr/bin/env python3
"""Render the memory-state document as one self-contained HTML page.

Deliberately no JavaScript and no vendored library. The pitch is one file,
standard library only, and shipping half a megabyte of graph engine to draw
forty rows would make that untrue. Everything here is server-rendered HTML plus
a little inline SVG, so the page works from file://, offline, with scripting
disabled, and in a browser that is ten years old.

It is a state view, not a dashboard: no token counts, no cost charts, no
sparklines of activity. Those are answered better elsewhere. What nothing else
shows is which repos are bound, which are one `mv` away from losing their notes,
and what is already unreachable.

    commonbook view | commonbook render          write commonbook.html
    commonbook render --in doc.json --out x.html

Warnings are grouped by kind rather than listed flat, because a real machine
produced 109 of them and a flat feed of 109 lines is not a finding, it is wallpaper.
"""

from __future__ import annotations

import argparse
import collections
import html
import json
import os
import sys
from pathlib import Path

def plural(n: int, one: str, many: "str | None" = None) -> str:
    """"1 note" / "2 notes". Written out rather than "note(s)" because a public
    tool is judged on its first screen, and (s) reads as unfinished."""
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


STATE_ORDER = ["orphaned", "refused", "no-stable-identity", "unbound", "bound"]
STATE_LABEL = {
    "bound": "bound",
    "unbound": "unbound",
    "orphaned": "orphaned",
    "no-stable-identity": "no stable identity",
    "refused": "refused",
}
# Severity, not decoration: the colour says how close this repo is to losing
# notes, so the eye lands on the rows that need action first.
STATE_TONE = {
    "bound": "ok",
    "unbound": "warn",
    "orphaned": "crit",
    "no-stable-identity": "warn",
    "refused": "crit",
}

WARNING_HELP = {
    "store-outside-search-root": "Live memory outside the searched tree. Widen --search to attribute it.",
    "orphan-unmatched": "Orphaned memory that matches no live repo. Move the repo back, or adopt it manually.",
    "bound-elsewhere": "Bound to a directory this tool did not choose. Harmless if deliberate.",
    "bound-book-missing": "Bound to a directory that does not exist yet. It appears on first write.",
    "no-stable-identity": "Keyed on the path. Add a git remote or this will orphan again on the next move.",
    "settings-committable": "The binding could be committed and would leak a local path.",
    "settings-unparseable": "Settings file is not usable, so the repo counts as unbound.",
    "book-empty": "A book with no notes — a bind that was never written to.",
    "book-without-repo": "A book whose repo was not found in the searched tree.",
    "book-index-over-cap": "This index is truncated when loaded. Move detail into topic files.",
    "index-over-cap": "The aggregate index does not fit its budget; entries were dropped.",
    "note-without-frontmatter": "A note with no type, so its contract cannot be checked.",
    "notes-skipped": "Files that could not be read while scanning.",
    "duplicate-slug": "Two projects resolve to one name.",
    "books-root-missing": "No books directory. Pass --books, or bind a repo first.",
    "search-root-missing": "The searched directory does not exist.",
}

CSS = """
:root{
  --bg:#faf9f7; --panel:#fff; --sunk:#f1efec; --line:#e2ded8;
  --ink:#1b1d1f; --mid:#5f6772; --faint:#8d949c;
  --ok:#2f6f4f; --ok-bg:#e6f0e9;
  --warn:#8a6314; --warn-bg:#f6eeda;
  --crit:#9c3030; --crit-bg:#f6e3e3;
  --accent:#1d6b66;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#131618; --panel:#1a1e21; --sunk:#20252a; --line:#2b3238;
    --ink:#e9eae7; --mid:#a3aab2; --faint:#727b84;
    --ok:#74c193; --ok-bg:#16291d;
    --warn:#d9ac52; --warn-bg:#2b2414;
    --crit:#e08585; --crit-bg:#2e1b1b;
    --accent:#57b0aa;
  }
}
*{box-sizing:border-box}
body{
  margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1080px;margin:0 auto;padding:2.5rem 1.25rem 5rem}
h1{font-size:1.5rem;letter-spacing:-.02em;margin:0 0 .2rem}
h2{font-size:.95rem;letter-spacing:.02em;margin:2.5rem 0 .8rem;font-weight:700}
.sub{color:var(--mid);font-size:.87rem;margin:0}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.85em}

.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-top:1.6rem}
.tile{background:var(--panel);padding:.85rem 1rem;display:flex;flex-direction:column;gap:.15rem}
.tile .n{font-size:1.6rem;font-weight:700;letter-spacing:-.03em;font-variant-numeric:tabular-nums;line-height:1.1}
.tile .l{font-size:.76rem;color:var(--mid)}
.tile.ok .n{color:var(--ok)} .tile.warn .n{color:var(--warn)} .tile.crit .n{color:var(--crit)}

.bar{display:flex;height:12px;border-radius:6px;overflow:hidden;margin:1.1rem 0 .5rem;background:var(--sunk)}
.bar span{display:block}
.legend{display:flex;flex-wrap:wrap;gap:.9rem;font-size:.78rem;color:var(--mid)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:.35rem;vertical-align:baseline}

.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.86rem}
th,td{text-align:left;padding:.55rem .8rem;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:.7rem;letter-spacing:.07em;text-transform:uppercase;color:var(--faint);background:var(--sunk);
  font-weight:700;white-space:nowrap;position:sticky;top:0}
tr:last-child td{border-bottom:none}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--mid)}
td.path{max-width:34ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.chip{display:inline-block;font-size:.7rem;letter-spacing:.03em;padding:.15em .5em;border-radius:4px;
  font-weight:700;white-space:nowrap}
.chip.ok{background:var(--ok-bg);color:var(--ok)}
.chip.warn{background:var(--warn-bg);color:var(--warn)}
.chip.crit{background:var(--crit-bg);color:var(--crit)}

.wgroup{border-bottom:1px solid var(--line);padding:.75rem .9rem}
.wgroup:last-child{border-bottom:none}
.whead{display:flex;align-items:baseline;gap:.6rem}
.wcount{font-variant-numeric:tabular-nums;font-weight:700;color:var(--warn);min-width:2.2em}
.wkind{font-family:ui-monospace,Menlo,monospace;font-size:.8rem}
.wsay{color:var(--mid);font-size:.83rem;margin:.25rem 0 0 2.8em}
.wlist{margin:.35rem 0 0 2.8em;color:var(--faint);font-size:.78rem}
.empty{padding:1.4rem;color:var(--mid);text-align:center;font-size:.88rem}
footer{margin-top:3rem;padding-top:1rem;border-top:1px solid var(--line);color:var(--faint);font-size:.78rem}
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def tile(n, label, tone=""):
    return (f'<div class="tile {tone}"><span class="n">{n:,}</span>'
            f'<span class="l">{esc(label)}</span></div>')


def state_bar(states: dict) -> str:
    total = sum(states.values()) or 1
    tone_col = {"ok": "var(--ok)", "warn": "var(--warn)", "crit": "var(--crit)"}
    segs, legend = [], []
    for st in STATE_ORDER:
        n = states.get(st, 0)
        if not n:
            continue
        col = tone_col[STATE_TONE[st]]
        segs.append(f'<span style="width:{n/total*100:.4f}%;background:{col}"></span>')
        legend.append(f'<span><i style="background:{col}"></i>'
                      f'{esc(STATE_LABEL[st])} · {n}</span>')
    if not segs:
        return ""
    return (f'<div class="bar">{"".join(segs)}</div>'
            f'<div class="legend">{"".join(legend)}</div>')


def repo_rows(repos) -> str:
    # Most urgent first: a bound repo needs no attention and belongs at the end.
    rank = {s: i for i, s in enumerate(STATE_ORDER)}
    rows = []
    for r in sorted(repos, key=lambda r: (rank.get(r["state"], 99), r["name"])):
        st = r["state"]
        stores = r.get("pathStores") or []
        extra = f' <span class="mono" style="color:var(--faint)">+{len(stores)-1}</span>' if len(stores) > 1 else ""
        rows.append(
            "<tr>"
            f'<td><strong>{esc(r["name"])}</strong>{extra}<br>'
            f'<span class="mono" style="color:var(--faint)">{esc(r.get("identity",{}).get("kind",""))}</span></td>'
            f'<td><span class="chip {STATE_TONE.get(st,"warn")}">{esc(STATE_LABEL.get(st,st))}</span></td>'
            f'<td class="path mono" title="{esc(r["path"])}">{esc(r["path"])}</td>'
            f'<td class="num">{r.get("notes",0):,}</td>'
            f'<td style="color:var(--mid)">{esc(r.get("why") or "")}</td>'
            "</tr>")
    if not rows:
        return '<div class="empty">No repositories found in the searched tree.</div>'
    return ('<div class="tw"><table><thead><tr>'
            "<th>Repo</th><th>State</th><th>Path</th><th>Notes</th><th>Why</th>"
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def warning_groups(warnings) -> str:
    if not warnings:
        return '<div class="empty">Nothing to report.</div>'
    by = collections.OrderedDict()
    for w in warnings:
        by.setdefault(w.get("kind", "?"), []).append(w)
    out = []
    for kind, items in sorted(by.items(), key=lambda kv: -len(kv[1])):
        say = WARNING_HELP.get(kind, "")
        sample = ", ".join(esc(Path(str(i.get("subject", ""))).name or i.get("subject", ""))
                           for i in items[:4])
        more = f" and {len(items)-4} more" if len(items) > 4 else ""
        out.append(
            f'<div class="wgroup"><div class="whead">'
            f'<span class="wcount">{len(items)}</span>'
            f'<span class="wkind">{esc(kind)}</span></div>'
            + (f'<p class="wsay">{esc(say)}</p>' if say else "")
            + f'<div class="wlist">{sample}{more}</div></div>')
    return "".join(out)


def topic_rows(topics) -> str:
    if not topics:
        return ('<div class="empty">No topic appears in more than one book yet. '
                "Cross-book structure shows up here as books fill.</div>")
    rows = []
    for t in topics[:40]:
        rows.append("<tr>"
                    f'<td><strong>{esc(t.get("topic"))}</strong></td>'
                    f'<td class="num">{len(t.get("books") or [])}</td>'
                    f'<td style="color:var(--mid)">{esc(", ".join(t.get("books") or []))}</td>'
                    "</tr>")
    return ('<div class="tw"><table><thead><tr>'
            "<th>Topic</th><th>Books</th><th>Appears in</th>"
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def render(doc: dict) -> str:
    t = doc.get("totals", {})
    states = t.get("states", {}) or {}
    at_risk = sum(states.get(s, 0) for s in ("unbound", "no-stable-identity",
                                             "orphaned", "refused"))
    tiles = "".join([
        tile(t.get("repos", 0), "repositories"),
        tile(states.get("bound", 0), "bound", "ok"),
        tile(at_risk, "at risk", "warn" if at_risk else ""),
        tile(t.get("orphanNotes", 0), "orphaned notes", "crit" if t.get("orphanNotes") else ""),
        tile(t.get("books", 0), "books"),
        tile(t.get("notes", 0), "notes"),
    ])
    headline = ("Every repository is bound." if not at_risk else
                f"{at_risk} repositor{'y is' if at_risk == 1 else 'ies are'} one move away "
                "from losing notes.")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Commonbook — memory state</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<h1>Memory state</h1>
<p class="sub">{esc(headline)}</p>
<div class="tiles">{tiles}</div>
{state_bar(states)}

<h2>Repositories</h2>
<div class="card">{repo_rows(doc.get("repos") or [])}</div>

<h2>Needs attention</h2>
<div class="card">{warning_groups(doc.get("warnings") or [])}</div>

<h2>Topics across books</h2>
<div class="card">{topic_rows(doc.get("topics") or [])}</div>

<footer>
Generated {esc(doc.get("generatedAt", "—"))} by {esc(doc.get("tool", "commonbook"))}.
Searched <code>{esc((doc.get("scan") or {}).get("searchRoot", "—"))}</code>.
This page is a snapshot; re-run to refresh.
</footer>
</div></body></html>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog=os.environ.get("COMMONBOOK_PROG", "render.py"),
        description=__doc__.split("\n")[0])
    ap.add_argument("--in", dest="src", metavar="FILE",
                    help="the view document (default: stdin)")
    ap.add_argument("--out", metavar="FILE", default="commonbook.html")
    a = ap.parse_args(argv)

    try:
        raw = Path(a.src).read_text(encoding="utf-8") if a.src else sys.stdin.read()
    except OSError as exc:
        print(f"cannot read input: {exc}", file=sys.stderr)
        return 2
    if not raw.strip():
        print("no input — pipe `commonbook view` into this, or pass --in",
              file=sys.stderr)
        return 2
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"input is not the view document: {exc}", file=sys.stderr)
        return 2
    if not isinstance(doc, dict) or "totals" not in doc:
        print("input does not look like a view document (no totals)", file=sys.stderr)
        return 2

    out = Path(a.out).expanduser()
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(render(doc), encoding="utf-8")
    os.replace(tmp, out)
    print(f"  wrote {out}  ({out.stat().st_size:,} B)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
