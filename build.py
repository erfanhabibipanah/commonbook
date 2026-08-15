#!/usr/bin/env python3
"""Build commonbook.pyz — the single file the README tells people to download.

The install line fetches ONE file. The tool is several modules plus a shell shim
that routes between them, so a naive single-file install silently loses every
verb that does not live in the core: `commonbook view` — the showcase command in
the launch post — answered with argparse's "invalid choice" on first contact.

A zipapp fixes that properly rather than by trimming the README: stdlib
`zipapp` packs every module and a dispatcher into one executable file, with no
dependency and no install step.

    python3 build.py              write commonbook.pyz
    python3 build.py --check      build, then run every advertised verb against it

--check is the point. Building an artifact nobody exercises is how the install
breaks again the next time a module is added.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BIN = ROOT / "plugins" / "commonbook" / "bin"

# Kept in step with the shell shim's case table. The check below fails if they
# ever disagree, so this cannot rot quietly.
ROUTED = ["view", "aggregate", "graph", "autonomy", "render"]

MAIN = '''"""Entry point for the packed build. Mirrors the shell shim's routing."""
import sys

ROUTED = {routed!r}


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] in ROUTED:
        verb = argv[0]
        import os
        # The modules read COMMONBOOK_PROG for their usage line. Setting argv[0]
        # is not enough: `usage: aggregate.py` after typing `commonbook
        # aggregate` sends people looking for a file that is not on PATH.
        os.environ["COMMONBOOK_PROG"] = "commonbook " + verb
        mod = __import__(verb)
        return mod.main(argv[1:]) or 0
    if not argv or argv[0] in ("-h", "--help"):
        import commonbook
        try:
            commonbook.main(["--help"])
        except SystemExit:
            pass
        print()
        print("also available:")
        for verb, blurb in (
            ("view", "one JSON document describing this machine's memory state"),
            ("aggregate", "a ranked, budgeted index across every book"),
            ("graph", "report whether a code graph exists, and whether it is stale"),
            ("autonomy", "write-authority tiers and the invariants that check them"),
            ("render", "turn a view document into one self-contained HTML page"),
        ):
            print("  {{:<11}} {{}}".format(verb, blurb))
        return 0
    import commonbook
    return commonbook.main(argv)


if __name__ == "__main__":
    sys.exit(main())
'''


def build(out: Path) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "app"
        stage.mkdir()
        for py in sorted(BIN.glob("*.py")):
            shutil.copy2(py, stage / py.name)
        (stage / "__main__.py").write_text(MAIN.format(routed=ROUTED), encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(stage, target=out, interpreter="/usr/bin/env python3")
    out.chmod(0o755)
    return out


def check(pyz: Path) -> int:
    """Run every verb the README advertises against the built artifact.

    Reads the verbs out of the README rather than a hand-kept list, so the
    artifact is tested against what users are actually told exists.
    """
    import re
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    verbs = sorted({m for m in re.findall(r"`commonbook (\w+)`", readme)})
    if not verbs:
        print("no verbs found in README", file=sys.stderr)
        return 1

    # The shim and the packed dispatcher must agree, or one install path has
    # verbs the other does not.
    shim = (BIN / "commonbook").read_text(encoding="utf-8")
    for verb in ROUTED:
        if verb not in shim:
            print(f"  MISMATCH: {verb} is routed in the build but not in the shim",
                  file=sys.stderr)
            return 1

    bad = 0
    for verb in verbs:
        p = subprocess.run([sys.executable, str(pyz), verb, "--help"],
                           capture_output=True, text=True, timeout=60)
        ok = p.returncode == 0
        print(f"  {'ok  ' if ok else 'FAIL'} commonbook {verb}")
        if not ok:
            bad += 1
            print("       " + (p.stderr or p.stdout).strip().splitlines()[-1][:90])
    print()
    if bad:
        print(f"{bad} advertised verb(s) do not work in the single-file build",
              file=sys.stderr)
        return 1
    print(f"all {len(verbs)} advertised verbs work in {pyz.name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=str(ROOT / "commonbook.pyz"))
    ap.add_argument("--check", action="store_true",
                    help="run every advertised verb against the artifact")
    a = ap.parse_args()

    pyz = build(Path(a.out))
    print(f"built {pyz}  ({pyz.stat().st_size:,} B)")
    return check(pyz) if a.check else 0


if __name__ == "__main__":
    sys.exit(main())
