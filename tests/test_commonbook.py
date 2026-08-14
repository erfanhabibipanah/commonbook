#!/usr/bin/env python3
"""Tests for commonbook.

Every test runs against a temporary HOME and temporary repos, so a bug here can
never reach the real memory store. `CLAUDE_CONFIG_DIR` is redirected per test.

Run:  python3 -m unittest discover -s tests -v
      python3 tests/test_commonbook.py
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "plugins" / "commonbook" / "bin" / "commonbook.py"


def load_module(config_dir: Path):
    """Import commonbook fresh, with CLAUDE_CONFIG_DIR pointing at a temp dir.

    The module reads the env var at import time, so each test needs its own
    import rather than a shared one.
    """
    os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
    spec = importlib.util.spec_from_file_location(f"cb_{id(config_dir)}", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def make_repo(path: Path, remote: "str | None" = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", ".", cwd=path)
    git("config", "user.email", "t@example.com", cwd=path)
    git("config", "user.name", "t", cwd=path)
    (path / "README.md").write_text("x\n")
    git("add", "-A", cwd=path)
    git("commit", "-qm", "init", cwd=path)
    if remote:
        git("remote", "add", "origin", remote, cwd=path)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cb-test-"))
        self.config = self.tmp / "claude"
        (self.config / "projects").mkdir(parents=True)
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.cb = load_module(self.config)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def book_of(self, repo: Path) -> "str | None":
        settings, ok = self.cb.read_settings(repo)
        return settings.get("autoMemoryDirectory") if ok else None


class TestIdentity(Base):
    def test_same_repo_across_transports_is_one_identity(self):
        """A second clone added over ssh must not get its own book."""
        urls = [
            "git@github.com:Acme/Demo-App.git",
            "https://github.com/acme/demo-app",
            "ssh://git@github.com/acme/demo-app/",
            "https://github.com/acme/demo-app.git",
        ]
        ids = set()
        repo = make_repo(self.work / "demo")
        for u in urls:
            git("remote", "remove", "origin", cwd=repo) if u != urls[0] else None
            git("remote", "add", "origin", u, cwd=repo)
            ids.add(self.cb.identity(repo)[0])
        self.assertEqual(len(ids), 1, f"transports disagreed: {ids}")

    def test_different_repos_get_different_identities(self):
        a = make_repo(self.work / "a", "https://github.com/acme/a")
        b = make_repo(self.work / "b", "https://github.com/acme/b")
        self.assertNotEqual(self.cb.identity(a)[0], self.cb.identity(b)[0])

    def test_no_remote_falls_back_to_root_commit(self):
        repo = make_repo(self.work / "local-only")
        book_id, kind, _ = self.cb.identity(repo)
        self.assertEqual(kind, "root-commit")
        self.assertTrue(book_id)

    def test_no_git_falls_back_to_path_and_says_so(self):
        plain = self.work / "not-a-repo"
        plain.mkdir()
        _, kind, _ = self.cb.identity(plain)
        self.assertEqual(kind, "path")

    def test_identity_survives_a_move(self):
        """The property the tool exists for."""
        repo = make_repo(self.work / "before", "https://github.com/acme/movable")
        before = self.cb.identity(repo)[0]
        moved = self.work / "after"
        shutil.move(str(repo), str(moved))
        self.assertEqual(self.cb.identity(moved)[0], before)


class TestBind(Base):
    def _bind(self, repo, vault=None, dry_run=False):
        import argparse
        return self.cb.cmd_bind(argparse.Namespace(
            path=str(repo), vault=vault, dry_run=dry_run))

    def test_bind_writes_the_setting(self):
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        self.assertEqual(self._bind(repo), 0)
        self.assertIn("autoMemoryDirectory", json.loads(
            (repo / ".claude/settings.local.json").read_text()))

    def test_dry_run_writes_nothing(self):
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        self._bind(repo, dry_run=True)
        self.assertFalse((repo / ".claude/settings.local.json").exists())

    def test_bind_preserves_existing_settings(self):
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        sf = repo / ".claude/settings.local.json"
        sf.parent.mkdir(parents=True)
        sf.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
        self._bind(repo)
        data = json.loads(sf.read_text())
        self.assertIn("permissions", data)
        self.assertIn("autoMemoryDirectory", data)

    def test_refuses_to_clobber_unparseable_settings(self):
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        sf = repo / ".claude/settings.local.json"
        sf.parent.mkdir(parents=True)
        sf.write_text("{ not json")
        self.assertEqual(self._bind(repo), 2)
        self.assertEqual(sf.read_text(), "{ not json")

    def test_vault_flag_redirects_the_book(self):
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        vault = self.tmp / "myvault"
        vault.mkdir()
        self._bind(repo, vault=str(vault))
        # The stored path is canonicalised, which matters on macOS where /var is
        # a symlink to /private/var — compare resolved against resolved, or this
        # passes locally and fails in CI (or the reverse).
        self.assertTrue(self.book_of(repo).startswith(str(vault.resolve())))

    def test_worktree_shares_the_main_repo_book(self):
        """A linked worktree must not get a second brain."""
        repo = make_repo(self.work / "main", "https://github.com/acme/wt")
        wt = self.work / "wt-feature"
        git("worktree", "add", "-q", str(wt), "-b", "feature", cwd=repo)
        self.assertEqual(self.cb.identity(self.cb.repo_root(wt))[0],
                         self.cb.identity(repo)[0])


class TestAdoptAndPrune(Base):
    def _orphan(self, name, cwd, notes=("MEMORY.md", "topic.md")):
        p = self.config / "projects" / name
        (p / "memory").mkdir(parents=True)
        (p / "s.jsonl").write_text(json.dumps({"cwd": cwd}) + "\n")
        for n in notes:
            (p / "memory" / n).write_text(f"# {n}\ncontent\n")
        return p

    def test_cwd_is_read_from_transcript_not_decoded(self):
        """Directory names are lossy; the transcript cwd is authoritative."""
        p = self._orphan("-any-encoded-name", "/real/path/here")
        self.assertEqual(self.cb.cwd_of(p), "/real/path/here")

    def test_live_store_is_not_an_orphan(self):
        live = make_repo(self.work / "live", "https://github.com/acme/live")
        self._orphan("live-store", str(live))
        self.assertEqual([s for s in self.cb.scan_stores() if not s["alive"]], [])

    def test_adopt_copies_and_binds(self):
        import argparse
        repo = make_repo(self.work / "widget", "https://github.com/acme/widget")
        self._orphan("-gone-widget", "/gone/widget")
        rc = self.cb.cmd_adopt(argparse.Namespace(
            vault=None, search=str(self.work), depth=3, dry_run=False, yes=True))
        self.assertEqual(rc, 0)
        book = Path(self.book_of(repo))
        self.assertEqual(len({p.name for p in book.glob("*.md")}), 2)

    def test_adopt_never_overwrites_an_existing_note(self):
        """Orphan notes land beside live ones — merging loses information."""
        import argparse
        repo = make_repo(self.work / "widget", "https://github.com/acme/widget")
        self.cb.cmd_bind(argparse.Namespace(path=str(repo), vault=None, dry_run=False))
        book = Path(self.book_of(repo))
        book.mkdir(parents=True, exist_ok=True)
        (book / "MEMORY.md").write_text("ORIGINAL\n")
        self._orphan("-gone-widget", "/gone/widget", notes=("MEMORY.md",))
        self.cb.cmd_adopt(argparse.Namespace(
            vault=None, search=str(self.work), depth=3, dry_run=False, yes=True))
        self.assertEqual((book / "MEMORY.md").read_text(), "ORIGINAL\n")
        self.assertTrue(list(book.glob("MEMORY.from-*.md")))

    def test_dry_run_adopts_nothing(self):
        import argparse
        repo = make_repo(self.work / "widget", "https://github.com/acme/widget")
        self._orphan("-gone-widget", "/gone/widget")
        self.cb.cmd_adopt(argparse.Namespace(
            vault=None, search=str(self.work), depth=3, dry_run=True, yes=True))
        self.assertIsNone(self.book_of(repo))

    def test_prune_refuses_when_notes_are_not_in_the_book(self):
        """The destructive path must fail closed — these notes exist nowhere else."""
        import argparse
        p = self._orphan("-gone-liar", "/gone/liar", notes=("precious.md",))
        (p / "memory" / self.cb.ADOPTED_MARKER).write_text(json.dumps(
            {"adoptedInto": str(self.tmp / "nowhere"), "notes": 1}))
        rc = self.cb.cmd_prune(argparse.Namespace(dry_run=False, yes=True))
        self.assertEqual(rc, 1)
        self.assertTrue((p / "memory" / "precious.md").exists())

    def test_prune_deletes_only_verified_stores(self):
        import argparse
        repo = make_repo(self.work / "widget", "https://github.com/acme/widget")
        p = self._orphan("-gone-widget", "/gone/widget")
        self.cb.cmd_adopt(argparse.Namespace(
            vault=None, search=str(self.work), depth=3, dry_run=False, yes=True))
        self.assertEqual(self.cb.cmd_prune(argparse.Namespace(dry_run=False, yes=True)), 0)
        self.assertFalse((p / "memory").exists())


class TestDoctor(Base):
    def test_unbound_repo_is_a_problem(self):
        import argparse
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        self.assertEqual(self.cb.cmd_doctor(
            argparse.Namespace(path=str(repo), vault=None)), 1)

    def test_bound_clean_repo_passes(self):
        import argparse
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        self.cb.cmd_bind(argparse.Namespace(path=str(repo), vault=None, dry_run=False))
        self.assertEqual(self.cb.cmd_doctor(
            argparse.Namespace(path=str(repo), vault=None)), 0)

    def test_caps_is_valid_json(self):
        import argparse, io, contextlib
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.cb.cmd_caps(argparse.Namespace(path=str(repo), vault=None))
        data = json.loads(buf.getvalue())
        self.assertIn("bound", data)
        self.assertFalse(data["bound"])


class TestSkills(unittest.TestCase):
    """Frontmatter must stay portable — some keys hard-fail on upload."""

    PORTABLE = {"name", "description", "license", "compatibility",
                "metadata", "allowed-tools"}

    def test_every_skill_is_portable(self):
        import re
        skills = sorted((ROOT / "plugins/commonbook/skills").glob("*/SKILL.md"))
        self.assertTrue(skills, "no skills found")
        for path in skills:
            with self.subTest(skill=path.parent.name):
                text = path.read_text()
                m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
                self.assertIsNotNone(m, "missing frontmatter")
                keys = {ln.split(":", 1)[0].strip()
                        for ln in m.group(1).splitlines()
                        if ":" in ln and not ln.startswith((" ", "\t"))}
                self.assertLessEqual(keys, self.PORTABLE, f"non-portable keys: {keys - self.PORTABLE}")
                self.assertIn("name", keys, "name is required by the spec")
                self.assertEqual(
                    re.search(r"^name:\s*(\S+)", m.group(1), re.M).group(1),
                    path.parent.name, "name must equal the directory name")


class TestManifests(unittest.TestCase):
    def test_manifests_are_valid_json(self):
        for rel in (".claude-plugin/marketplace.json",
                    "plugins/commonbook/.claude-plugin/plugin.json"):
            with self.subTest(file=rel):
                json.loads((ROOT / rel).read_text())

    def test_versions_agree(self):
        market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        plugin = json.loads((ROOT / "plugins/commonbook/.claude-plugin/plugin.json").read_text())
        src = SRC.read_text()
        import re
        code_version = re.search(r'__version__\s*=\s*"([^"]+)"', src).group(1)
        self.assertEqual(plugin["version"], code_version)
        self.assertEqual(market["plugins"][0]["version"], code_version)


if __name__ == "__main__":
    unittest.main(verbosity=2)


AGG = ROOT / "plugins" / "commonbook" / "bin" / "aggregate.py"


class TestAggregate(unittest.TestCase):
    """The index must never exceed the cap — content past it is dropped
    silently at load time, so an over-budget index reads as complete."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cb-agg-"))
        spec = importlib.util.spec_from_file_location("agg", AGG)
        self.agg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.agg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _book(self, name, count, tags="", body="filler "):
        d = self.tmp / name / "memory"
        d.mkdir(parents=True)
        for i in range(count):
            (d / f"note-{i}.md").write_text(
                f"---\nname: {name} note {i}\ndescription: {body * 12}\n"
                f"tags: [{tags}]\n---\n\n# {name} note {i}\n")

    def test_respects_both_caps(self):
        self._book("alpha", 200)
        self._book("beta", 200)
        notes = self.agg.scan_books(self.tmp)
        text, dropped = self.agg.render(self.agg.rank(notes), self.agg.BYTE_BUDGET)
        self.assertLessEqual(len(text.encode()), self.agg.BYTE_BUDGET)
        self.assertLessEqual(len(text.splitlines()), self.agg.LINE_BUDGET)
        self.assertGreater(dropped, 0)

    def test_overflow_is_reported_not_hidden(self):
        self._book("alpha", 300)
        notes = self.agg.scan_books(self.tmp)
        text, dropped = self.agg.render(self.agg.rank(notes), 2000)
        self.assertIn("did not fit", text)
        self.assertIn(str(dropped), text)

    def test_small_book_is_not_truncated(self):
        self._book("alpha", 3)
        notes = self.agg.scan_books(self.tmp)
        text, dropped = self.agg.render(self.agg.rank(notes), self.agg.BYTE_BUDGET)
        self.assertEqual(dropped, 0)
        self.assertNotIn("did not fit", text)

    def test_pinned_outranks_everything(self):
        self._book("alpha", 5)
        p = self.tmp / "alpha" / "memory" / "note-0.md"
        p.write_text("---\nname: pinned one\npin: true\n---\n\n# pinned one\n")
        ranked = self.agg.rank(self.agg.scan_books(self.tmp))
        self.assertEqual(ranked[0]["title"], "pinned one")

    def test_shared_topic_outranks_a_local_note(self):
        self._book("alpha", 4, tags="postgres")
        self._book("beta", 4, tags="postgres")
        self._book("gamma", 4, tags="only-here")
        ranked = self.agg.rank(self.agg.scan_books(self.tmp))
        self.assertGreater(ranked[0]["_shared"], 1)

    def test_empty_tree_is_not_an_error(self):
        self.assertEqual(self.agg.scan_books(self.tmp / "nothing"), [])
