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
import time
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


class TestContracts(Base):
    """A typed note that cannot meet its own contract is a different type."""

    def _note(self, name, text):
        d = self.tmp / "book"
        d.mkdir(exist_ok=True)
        p = d / name
        p.write_text(text)
        return p

    def test_decision_without_rejected_fails(self):
        p = self._note("d.md", "---\ntype: decision\n---\n\n# Use Postgres\n\n"
                               "## Decision\n\nWe use Postgres.\n")
        self.assertTrue(self.cb.contract_violations(p))

    def test_decision_with_empty_rejected_fails(self):
        p = self._note("d.md", "---\ntype: decision\n---\n\n# X\n\n## Rejected\n\n"
                               "| Option | Why not |\n| --- | --- |\n|  |  |\n")
        self.assertTrue(self.cb.contract_violations(p))

    def test_decision_with_real_rejected_passes(self):
        p = self._note("d.md", "---\ntype: decision\n---\n\n# X\n\n## Rejected\n\n"
                               "| Option | Why not |\n| --- | --- |\n"
                               "| MySQL | no native JSONB, and the query shape needs it |\n")
        self.assertEqual(self.cb.contract_violations(p), [])

    def test_gotcha_without_verbatim_error_fails(self):
        p = self._note("g.md", "---\ntype: gotcha\n---\n\n# It broke\n\n"
                               "## Symptom\n\nThe build failed with a type error.\n")
        self.assertTrue(self.cb.contract_violations(p))

    def test_gotcha_with_fenced_error_passes(self):
        p = self._note("g.md", "---\ntype: gotcha\n---\n\n# It broke\n\n## Symptom\n\n"
                               "```\nTypeError: cannot read property 'x' of undefined\n```\n")
        self.assertEqual(self.cb.contract_violations(p), [])

    def test_untyped_notes_are_not_policed(self):
        p = self._note("n.md", "---\ntype: note\n---\n\n# Anything\n\nfree form\n")
        self.assertEqual(self.cb.contract_violations(p), [])

    def test_note_with_no_frontmatter_is_not_policed(self):
        p = self._note("n.md", "# Just a heading\n\ntext\n")
        self.assertEqual(self.cb.contract_violations(p), [])

    def test_lint_exits_nonzero_on_violations(self):
        import argparse
        self._note("d.md", "---\ntype: decision\n---\n\n# X\n\nno rejected section\n")
        rc = self.cb.cmd_lint(argparse.Namespace(
            path=str(self.work), vault=None, book=str(self.tmp / "book")))
        self.assertEqual(rc, 1)

    def test_lint_exits_zero_when_clean(self):
        import argparse
        self._note("n.md", "---\ntype: note\n---\n\n# fine\n\ntext\n")
        rc = self.cb.cmd_lint(argparse.Namespace(
            path=str(self.work), vault=None, book=str(self.tmp / "book")))
        self.assertEqual(rc, 0)


GRAPH = ROOT / "plugins" / "commonbook" / "bin" / "graph.py"


class TestGraphAdapter(unittest.TestCase):
    """The adapter probes and reports. It must never guess, and an absent tool
    or absent graph is a normal state, not an error."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cb-graph-"))
        spec = importlib.util.spec_from_file_location("gr", GRAPH)
        self.gr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gr)
        self.repo = make_repo(self.tmp / "product" / "repo-a",
                              "https://github.com/acme/repo-a")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _graph(self, payload):
        d = self.repo / "graphify-out"
        d.mkdir(exist_ok=True)
        p = d / "graph.json"
        p.write_text(json.dumps(payload))
        return p

    def test_valid_graph_has_no_problems(self):
        p = self._graph({"nodes": [{"id": "a", "label": "A"}], "links": []})
        data, problems = self.gr.validate(p)
        self.assertEqual(problems, [])
        self.assertIsNotNone(data)

    def test_missing_top_level_key_is_reported(self):
        p = self._graph({"nodes": [{"id": "a", "label": "A"}]})
        _, problems = self.gr.validate(p)
        self.assertTrue(any("links" in x for x in problems))

    def test_missing_node_key_is_reported(self):
        p = self._graph({"nodes": [{"id": "a"}], "links": []})
        _, problems = self.gr.validate(p)
        self.assertTrue(any("label" in x for x in problems))

    def test_corrupt_json_is_reported_not_raised(self):
        d = self.repo / "graphify-out"
        d.mkdir(exist_ok=True)
        p = d / "graph.json"
        p.write_text("not json{")
        data, problems = self.gr.validate(p)
        self.assertIsNone(data)
        self.assertTrue(problems)

    def test_scope_defaults_to_the_repo(self):
        scope, how = self.gr.graph_scope(self.repo)
        self.assertEqual(how, "repo")
        self.assertEqual(scope.resolve(), self.repo.resolve())

    def test_marker_widens_scope_to_the_product_directory(self):
        marker = self.repo.parent / self.gr.SCOPE_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("product\n")
        scope, how = self.gr.graph_scope(self.repo)
        self.assertEqual(how, "marker")
        self.assertEqual(scope.resolve(), self.repo.parent.resolve())

    def test_scope_search_is_bounded(self):
        """It must not walk up far enough to reach a whole workspace, where a
        single code graph is meaningless — unrelated repos share no imports."""
        deep = self.tmp / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (self.tmp / self.gr.SCOPE_MARKER).parent.mkdir(parents=True, exist_ok=True)
        (self.tmp / self.gr.SCOPE_MARKER).write_text("too far\n")
        scope, how = self.gr.graph_scope(deep)
        self.assertEqual(how, "repo")

    def test_absent_graph_is_not_an_error(self):
        import argparse, io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.gr.cmd_status(argparse.Namespace(path=str(self.repo)))
        self.assertEqual(rc, 0)
        self.assertIn("none", buf.getvalue())

    def test_schema_drift_exits_nonzero(self):
        import argparse, io, contextlib
        self._graph({"nodes": [{"id": "a"}], "vertices": []})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = self.gr.cmd_status(argparse.Namespace(path=str(self.repo)))
        self.assertEqual(rc, 1)
        self.assertIn("schema", buf.getvalue())


AUTONOMY = ROOT / "plugins" / "commonbook" / "bin" / "autonomy.py"


class TestAutonomy(unittest.TestCase):
    """A tier is a claim. The invariants are what make it true — so each one
    must actually fire, and a clean state must actually pass."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cb-auto-"))
        self.repo = self.tmp / "repo"
        (self.repo / ".git").mkdir(parents=True)
        spec = importlib.util.spec_from_file_location("au", AUTONOMY)
        self.au = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.au)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ns(self, **kw):
        import argparse
        base = {"path": str(self.repo), "book": None, "reason": None}
        base.update(kw)
        return argparse.Namespace(**base)

    def _quiet(self, fn, ns):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = fn(ns)
        return rc, buf.getvalue()

    def _proposal(self, name="p1.md", age_days=0):
        d = self.repo / self.au.REVIEW_REL
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text("# a proposal\n")
        if age_days:
            old = time.time() - age_days * 86400
            os.utime(f, (old, old))
        return f

    def test_default_tier_is_read_only(self):
        self.assertEqual(self.au.current_tier(self.repo), 0)

    def test_tier_three_requires_a_reason(self):
        rc, _ = self._quiet(self.au.cmd_set, self._ns(tier=3))
        self.assertEqual(rc, 2)
        self.assertEqual(self.au.current_tier(self.repo), 0)

    def test_tier_three_with_a_reason_is_allowed(self):
        rc, _ = self._quiet(self.au.cmd_set, self._ns(tier=3, reason="nightly pass"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.au.current_tier(self.repo), 3)

    def test_read_only_tier_with_proposals_fails(self):
        self._proposal()
        rc, out = self._quiet(self.au.cmd_check, self._ns())
        self.assertEqual(rc, 1)
        self.assertIn("tier 0 declares no writes", out)

    def test_stale_proposals_fail(self):
        self._quiet(self.au.cmd_set, self._ns(tier=1))
        self._proposal(age_days=self.au.STALE_PROPOSAL_DAYS + 5)
        rc, out = self._quiet(self.au.cmd_check, self._ns())
        self.assertEqual(rc, 1)
        self.assertIn("review gate is not being used", out)

    def test_runaway_growth_is_detected(self):
        book = self.tmp / "book"
        book.mkdir()
        for i in range(self.au.MAX_NOTES_PER_DAY + 5):
            (book / f"n{i}.md").write_text("# n\n")
        self._quiet(self.au.cmd_set, self._ns(tier=1))
        rc, out = self._quiet(self.au.cmd_check, self._ns(book=str(book)))
        self.assertEqual(rc, 1)
        self.assertIn("looks like a loop", out)

    def test_normal_authorship_is_not_flagged(self):
        """The check exists to catch runaway, not to police a productive week."""
        book = self.tmp / "book"
        book.mkdir()
        for i in range(5):
            (book / f"n{i}.md").write_text("# n\n")
        self._quiet(self.au.cmd_set, self._ns(tier=1))
        rc, _ = self._quiet(self.au.cmd_check, self._ns(book=str(book)))
        self.assertEqual(rc, 0)

    def test_unparseable_config_is_reported_not_silently_zero(self):
        p = self.repo / self.au.CONFIG_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ broken")
        rc, out = self._quiet(self.au.cmd_check, self._ns())
        self.assertEqual(rc, 1)
        self.assertIn("does not parse", out)

    def test_tier_three_without_reason_in_config_is_flagged(self):
        p = self.repo / self.au.CONFIG_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"tier": 3}))
        rc, out = self._quiet(self.au.cmd_check, self._ns())
        self.assertEqual(rc, 1)
        self.assertIn("no recorded reason", out)

    def test_clean_state_passes(self):
        self._quiet(self.au.cmd_set, self._ns(tier=1))
        rc, out = self._quiet(self.au.cmd_check, self._ns())
        self.assertEqual(rc, 0)
        self.assertIn("all invariants hold", out)


class TestMalformedSettings(Base):
    """A file can be valid JSON and still not be settings. Every one of these
    crashed a shipped read-only command with AttributeError before the type
    check was added — doctor should never take a repo down for a hand edit."""

    def _write(self, repo, payload):
        sf = repo / ".claude/settings.local.json"
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(payload)
        return sf

    def test_non_object_json_is_treated_as_unparseable(self):
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        for payload in ("[1,2,3]", "null", '"hello"', "42", "true"):
            with self.subTest(payload=payload):
                self._write(repo, payload)
                settings, ok = self.cb.read_settings(repo)
                self.assertFalse(ok)
                self.assertEqual(settings, {})

    def test_doctor_does_not_crash_on_non_object_json(self):
        import argparse
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        for payload in ("[1,2,3]", "null", '"hello"', "42"):
            with self.subTest(payload=payload):
                self._write(repo, payload)
                rc = self.cb.cmd_doctor(argparse.Namespace(path=str(repo), vault=None))
                self.assertEqual(rc, 1)          # a problem, not an exception

    def test_bind_refuses_rather_than_clobbering_non_object_json(self):
        import argparse
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        sf = self._write(repo, "[1,2,3]")
        rc = self.cb.cmd_bind(argparse.Namespace(path=str(repo), vault=None, dry_run=False))
        self.assertEqual(rc, 2)
        self.assertEqual(sf.read_text(), "[1,2,3]")

    def test_non_string_book_path_is_treated_as_absent(self):
        """Path(12345) raises TypeError several frames from the cause."""
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        for payload in ('{"autoMemoryDirectory": 12345}',
                        '{"autoMemoryDirectory": ["a"]}',
                        '{"autoMemoryDirectory": null}',
                        '{"autoMemoryDirectory": ""}'):
            with self.subTest(payload=payload):
                self._write(repo, payload)
                settings, ok = self.cb.read_settings(repo)
                self.assertTrue(ok)
                self.assertIsNone(self.cb.bound_dir(settings))

    def test_doctor_does_not_crash_on_non_string_book_path(self):
        import argparse
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        self._write(repo, '{"autoMemoryDirectory": 12345}')
        rc = self.cb.cmd_doctor(argparse.Namespace(path=str(repo), vault=None))
        self.assertEqual(rc, 1)

    def test_a_real_string_book_path_still_works(self):
        repo = make_repo(self.work / "r", "https://github.com/acme/r")
        self._write(repo, json.dumps({"autoMemoryDirectory": "/tmp/somewhere"}))
        settings, ok = self.cb.read_settings(repo)
        self.assertTrue(ok)
        self.assertEqual(self.cb.bound_dir(settings), "/tmp/somewhere")

VIEW = ROOT / "plugins" / "commonbook" / "bin" / "view.py"


def load_view(config_dir: Path):
    """Import view fresh, with CLAUDE_CONFIG_DIR pointing at a temp dir.

    It loads commonbook and aggregate from beside itself at import time, and
    commonbook reads the env var at ITS import time, so the redirect has to be
    in place before this returns or the document would describe a real machine.
    """
    os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
    spec = importlib.util.spec_from_file_location(f"view_{id(config_dir)}", VIEW)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestView(Base):
    """The document has to describe the machine it found, including the parts
    of it that are a mess. Every fixture here is built from scratch in a temp
    directory — nothing reads a real home, a real vault or a real repo."""

    def setUp(self):
        super().setUp()
        self.books = self.tmp / "books"
        self.books.mkdir()
        self.view = load_view(self.config)

    # ─────────────────────────────────────────────────────────── fixtures ──

    def _bind(self, repo):
        import argparse, contextlib, io
        with contextlib.redirect_stdout(io.StringIO()):
            self.cb.cmd_bind(argparse.Namespace(
                path=str(repo), vault=str(self.books), dry_run=False))

    def _note(self, book, name, tags="", frontmatter=True, body="a note"):
        d = self.books / book / "memory"
        d.mkdir(parents=True, exist_ok=True)
        head = (f"---\nname: {book} {name}\ntype: note\ntags: [{tags}]\n---\n\n"
                if frontmatter else "")
        (d / f"{name}.md").write_text(f"{head}# {book} {name}\n\n{body}\n")

    def _store(self, slug, cwd, notes=("MEMORY.md", "topic.md")):
        p = self.config / "projects" / slug
        (p / "memory").mkdir(parents=True)
        if cwd is not None:
            (p / "s.jsonl").write_text(json.dumps({"cwd": cwd}) + "\n")
        for n in notes:
            (p / "memory" / n).write_text(f"# {n}\ncontent\n")
        return p

    def _doc(self, depth=3):
        return self.view.build(self.work, depth, self.books)

    def _kinds(self, doc):
        return {w["kind"] for w in doc["warnings"]}

    # ───────────────────────────────────────────────────────────── shape ──

    def test_empty_machine_is_a_valid_document(self):
        doc = self._doc()
        self.assertEqual(json.loads(self.view.render(doc))["schema"], self.view.SCHEMA)
        self.assertEqual(doc["repos"], [])
        self.assertEqual(doc["books"], [])
        self.assertEqual(doc["orphans"], [])
        self.assertEqual(doc["topics"], [])
        self.assertEqual(doc["totals"]["notes"], 0)
        self.assertEqual(doc["index"]["dropped"], 0)
        # Nothing found must not read the same as nothing wrong.
        self.assertIn(self.view.W_NO_REPOS, self._kinds(doc))

    def test_totals_agree_with_the_lists_they_summarise(self):
        make_repo(self.work / "ledger", "https://github.com/acme/ledger")
        make_repo(self.work / "widget", "https://github.com/acme/widget")
        self._note("alpha", "n1")
        self._note("beta", "n2")
        self._store("-gone-widget", "/gone/widget")
        doc = self._doc()
        t = doc["totals"]
        self.assertEqual(t["repos"], len(doc["repos"]))
        self.assertEqual(sum(t["states"].values()), len(doc["repos"]))
        self.assertEqual(t["books"], len(doc["books"]))
        self.assertEqual(t["notes"], sum(b["notes"] for b in doc["books"]))
        self.assertEqual(t["bytes"], sum(b["bytes"] for b in doc["books"]))
        self.assertEqual(t["topics"], len(doc["topics"]))
        self.assertEqual(t["warnings"], len(doc["warnings"]))
        self.assertEqual(t["orphanStores"],
                         sum(1 for o in doc["orphans"] if not o["adopted"]))

    def test_document_is_stable_across_runs(self):
        """Two scans of an unchanged machine must differ in one field only, or
        nobody can diff last week's picture against this one."""
        make_repo(self.work / "ledger", "https://github.com/acme/ledger")
        self._note("alpha", "n1", tags="postgres")
        self._note("beta", "n2", tags="postgres")
        self._store("-gone-thing", "/gone/thing")
        first, second = self._doc(), self._doc()
        for d in (first, second):
            d.pop("generatedAt")
        self.assertEqual(self.view.render(first), self.view.render(second))

    def test_out_file_is_identical_to_stdout(self):
        import contextlib, io, re
        make_repo(self.work / "ledger", "https://github.com/acme/ledger")
        self._note("alpha", "n1")
        argv = ["--search", str(self.work), "--depth", "3", "--books", str(self.books)]

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.view.main(argv), 0)

        target = self.tmp / "out" / "view.json"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.view.main(argv + ["--out", str(target)]), 0)

        def strip(text):
            return re.sub(r'^\s*"generatedAt".*\n', "", text, flags=re.M)

        self.assertEqual(strip(target.read_text()), strip(out.getvalue()))
        # The summary goes to stderr so stdout is the document or nothing.
        self.assertIn("warning(s)", err.getvalue())

    def test_the_view_writes_nothing(self):
        """It reads. A picture of the machine that changes the machine is not a
        picture of the machine."""
        make_repo(self.work / "ledger", "https://github.com/acme/ledger")
        self._note("alpha", "n1")
        self._store("-gone-thing", "/gone/thing")

        def snapshot():
            seen = {}
            for base in (self.work, self.books, self.config):
                for p in sorted(base.rglob("*")):
                    if ".git" not in p.parts and p.is_file():
                        seen[str(p)] = p.stat().st_size
            return seen

        before = snapshot()
        self._doc()
        self.assertEqual(snapshot(), before)

    # ───────────────────────────────────────────────────────────── repos ──

    def test_bound_repo_is_reported_bound(self):
        repo = make_repo(self.work / "ledger", "https://github.com/acme/ledger")
        self._bind(repo)
        doc = self._doc()
        self.assertEqual(len(doc["repos"]), 1)
        r = doc["repos"][0]
        self.assertEqual(r["state"], "bound")
        self.assertEqual(r["book"], r["expectedBook"])
        self.assertEqual(r["identity"]["kind"], "remote")
        self.assertEqual(doc["totals"]["states"]["bound"], 1)

    def test_unbound_repo_is_reported_unbound(self):
        make_repo(self.work / "ledger", "https://github.com/acme/ledger")
        doc = self._doc()
        self.assertEqual(doc["repos"][0]["state"], "unbound")
        self.assertIsNone(doc["repos"][0]["book"])

    def test_repo_with_no_remote_and_no_commits_has_no_stable_identity(self):
        fresh = self.work / "fresh"
        fresh.mkdir()
        git("init", "-q", ".", cwd=fresh)
        doc = self._doc()
        self.assertEqual(doc["repos"][0]["state"], "no-stable-identity")
        self.assertEqual(doc["repos"][0]["identity"]["kind"], "path")
        self.assertIn(self.view.W_NO_STABLE_IDENTITY, self._kinds(doc))

    def test_unparseable_settings_is_refused(self):
        repo = make_repo(self.work / "ledger", "https://github.com/acme/ledger")
        sf = repo / ".claude" / "settings.local.json"
        sf.parent.mkdir(parents=True)
        sf.write_text("{ not json")
        doc = self._doc()
        self.assertEqual(doc["repos"][0]["state"], "refused")
        self.assertIn(self.view.W_SETTINGS_UNPARSEABLE, self._kinds(doc))

    def test_binding_to_a_book_that_is_not_its_own_is_warned(self):
        repo = make_repo(self.work / "ledger", "https://github.com/acme/ledger")
        sf = repo / ".claude" / "settings.local.json"
        sf.parent.mkdir(parents=True)
        sf.write_text(json.dumps(
            {"autoMemoryDirectory": str(self.books / "somewhere-else" / "memory")}))
        doc = self._doc()
        self.assertEqual(doc["repos"][0]["state"], "bound")
        self.assertIn(self.view.W_BOUND_ELSEWHERE, self._kinds(doc))
        self.assertIn(self.view.W_BOUND_BOOK_MISSING, self._kinds(doc))

    def test_worktree_is_a_checkout_not_a_second_repo(self):
        repo = make_repo(self.work / "main", "https://github.com/acme/wt")
        git("worktree", "add", "-q", str(self.work / "wt-feature"), "-b", "feature",
            cwd=repo)
        doc = self._doc()
        self.assertEqual(len(doc["repos"]), 1)
        self.assertEqual(len(doc["repos"][0]["checkouts"]), 1)
        self.assertEqual(doc["totals"]["checkouts"], 1)

    def test_duplicate_repository_names_are_warned(self):
        """Orphan matching falls back to the directory name, so two repos with
        one name is an ambiguity the document has to admit to."""
        make_repo(self.work / "one" / "ledger", "https://github.com/acme/a")
        make_repo(self.work / "two" / "ledger", "https://github.com/acme/b")
        doc = self._doc()
        self.assertEqual(len(doc["repos"]), 2)
        self.assertIn(self.view.W_DUPLICATE_SLUG, self._kinds(doc))

    def test_missing_search_root_says_it_looked_nowhere(self):
        doc = self.view.build(self.tmp / "no-such-dir", 3, self.books)
        self.assertEqual(doc["repos"], [])
        self.assertIn(self.view.W_SEARCH_ROOT_MISSING, self._kinds(doc))

    # ─────────────────────────────────────────────────────────── orphans ──

    def test_orphan_carries_its_dead_cwd_and_its_match(self):
        repo = make_repo(self.work / "widget", "https://github.com/acme/widget")
        self._store("-gone-widget", "/gone/widget")
        doc = self._doc()
        self.assertEqual(len(doc["orphans"]), 1)
        o = doc["orphans"][0]
        self.assertEqual(o["cwd"], "/gone/widget")
        self.assertEqual(o["notes"], 2)
        self.assertGreater(o["bytes"], 0)
        self.assertEqual(Path(o["match"]).resolve(), repo.resolve())
        self.assertEqual(doc["repos"][0]["state"], "orphaned")
        self.assertEqual(doc["totals"]["orphanNotes"], 2)

    def test_unmatched_orphan_is_warned_not_dropped(self):
        make_repo(self.work / "widget", "https://github.com/acme/widget")
        self._store("-gone-elsewhere", "/gone/no-such-project")
        doc = self._doc()
        self.assertIsNone(doc["orphans"][0]["match"])
        self.assertIn(self.view.W_ORPHAN_UNMATCHED, self._kinds(doc))
        self.assertEqual(doc["repos"][0]["state"], "unbound")

    def test_store_with_no_recorded_cwd_is_warned(self):
        """Without a transcript there is nothing to identify a store by, and
        guessing from the directory name is exactly the lossy decode adopt
        refuses to do."""
        self._store("-mystery", None)
        doc = self._doc()
        self.assertIsNone(doc["orphans"][0]["cwd"])
        self.assertIn(self.view.W_STORE_NO_CWD, self._kinds(doc))

    def test_adopted_orphan_is_counted_apart_from_a_live_one(self):
        self._store("-gone-widget", "/gone/widget")
        (self.config / "projects" / "-gone-widget" / "memory" /
         self.cb.ADOPTED_MARKER).write_text(json.dumps({"adoptedInto": "x"}))
        doc = self._doc()
        self.assertTrue(doc["orphans"][0]["adopted"])
        self.assertEqual(doc["totals"]["orphanStores"], 0)
        self.assertEqual(doc["totals"]["rescuedStores"], 1)

    # ───────────────────────────────────────────────────────────── books ──

    def test_book_reports_counts_and_the_span_of_its_notes(self):
        self._note("alpha", "n1")
        self._note("alpha", "n2")
        doc = self._doc()
        b = doc["books"][0]
        self.assertEqual(b["id"], "alpha")
        self.assertEqual(b["notes"], 2)
        self.assertGreater(b["bytes"], 0)
        self.assertTrue(b["newest"].endswith("Z"))
        self.assertLessEqual(b["oldest"], b["newest"])
        self.assertEqual(b["types"], {"note": 2})

    def test_book_named_after_a_repo_is_matched_by_name(self):
        make_repo(self.work / "ledger", "https://github.com/acme/ledger")
        self._note("ledger", "n1")
        doc = self._doc()
        self.assertEqual([r["how"] for r in doc["books"][0]["repos"]], ["name"])
        self.assertNotIn(self.view.W_BOOK_WITHOUT_REPO, self._kinds(doc))

    def test_book_with_no_live_repo_is_warned(self):
        self._note("ghost", "n1")
        doc = self._doc()
        self.assertEqual(doc["books"][0]["repos"], [])
        self.assertIn(self.view.W_BOOK_WITHOUT_REPO, self._kinds(doc))

    def test_empty_book_is_reported_rather_than_omitted(self):
        (self.books / "hollow").mkdir()
        doc = self._doc()
        self.assertEqual(doc["books"][0]["notes"], 0)
        self.assertIsNone(doc["books"][0]["newest"])
        self.assertEqual(doc["totals"]["emptyBooks"], 1)
        self.assertIn(self.view.W_BOOK_EMPTY, self._kinds(doc))

    def test_note_without_frontmatter_is_warned(self):
        self._note("alpha", "n1", frontmatter=False)
        doc = self._doc()
        self.assertEqual(doc["books"][0]["withoutFrontmatter"], 1)
        self.assertEqual(doc["books"][0]["types"], {"untyped": 1})
        self.assertIn(self.view.W_NOTE_NO_FRONTMATTER, self._kinds(doc))

    def test_undecodable_note_does_not_crash_the_scan(self):
        self._note("alpha", "good")
        (self.books / "alpha" / "memory" / "broken.md").write_bytes(
            b"\xff\xfe not valid utf-8 \x00\x01")
        doc = self._doc()
        self.assertEqual(doc["books"][0]["notes"], 2)
        self.assertEqual(doc["books"][0]["unreadable"], 0)

    def test_book_index_over_the_session_cap_is_warned(self):
        """Content past the cap is dropped at load time with no error, so a book
        whose MEMORY.md overruns is loading less than it appears to."""
        self._note("alpha", "n1")
        (self.books / "alpha" / "memory" / "MEMORY.md").write_text("x" * 30_000 + "\n")
        doc = self._doc()
        self.assertTrue(doc["books"][0]["index"]["overCap"])
        self.assertEqual(doc["books"][0]["notes"], 1)     # the index is not a note
        self.assertIn(self.view.W_BOOK_INDEX_OVER_CAP, self._kinds(doc))

    def test_aggregate_index_over_the_cap_reports_what_it_dropped(self):
        for i in range(250):
            self._note("alpha", f"n{i}", body="filler " * 40)
        doc = self._doc()
        self.assertGreater(doc["index"]["dropped"], 0)
        self.assertEqual(doc["index"]["indexed"] + doc["index"]["dropped"],
                         doc["index"]["candidates"])
        self.assertLessEqual(doc["index"]["bytes"], self.view.agg.BYTE_BUDGET)
        self.assertIn(self.view.W_INDEX_OVER_CAP, self._kinds(doc))

    # ──────────────────────────────────────────────────────────── topics ──

    def test_cross_book_topic_spans_the_books_it_appears_in(self):
        self._note("alpha", "n1", tags="postgres")
        self._note("beta", "n2", tags="postgres")
        self._note("alpha", "n3", tags="only-here")
        doc = self._doc()
        found = {t["topic"]: t for t in doc["topics"]}
        self.assertIn("postgres", found)
        self.assertEqual(found["postgres"]["books"], ["alpha", "beta"])
        self.assertEqual(found["postgres"]["notes"], 2)
        self.assertEqual(found["postgres"]["kind"], "tag")
        # A topic living in one book is not cross-project structure.
        self.assertNotIn("only-here", found)

    def test_a_shared_title_is_a_topic_too(self):
        self._note("alpha", "deploys")
        self._note("beta", "deploys")
        d = self.books / "beta" / "memory" / "deploys.md"
        d.write_text("---\nname: alpha deploys\ntype: note\ntags: []\n---\n\n# x\n")
        doc = self._doc()
        found = {t["topic"]: t for t in doc["topics"]}
        self.assertIn("alpha deploys", found)
        self.assertEqual(found["alpha deploys"]["kind"], "title")

    def test_topic_definition_stays_in_step_with_the_index(self):
        """The document and the index are built from the same notes; if they
        stop agreeing on what a shared topic is, the document says so."""
        self._note("alpha", "n1", tags="postgres")
        self._note("beta", "n2", tags="postgres")
        doc = self._doc()
        self.assertNotIn(self.view.W_TOPIC_DRIFT, self._kinds(doc))



class TestViewOmissions(unittest.TestCase):
    """Regressions for the two silent drops an adversarial review found.

    Both hid behind `totals.stores`: the number was right while the lists that
    should account for it were not. A document that omits things while looking
    complete is the exact failure this project exists to prevent, so each of
    these asserts reconciliation, not just presence.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cb-vom-"))
        self.cfg = self.tmp / "cfg"
        (self.cfg / "projects").mkdir(parents=True)
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.books = self.tmp / "books"
        self.books.mkdir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.cfg)
        spec = importlib.util.spec_from_file_location(f"vw_{id(self)}", VIEW)
        self.vw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.vw)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def _store(self, name, cwd, notes=2):
        p = self.cfg / "projects" / name
        (p / "memory").mkdir(parents=True)
        (p / "s.jsonl").write_text(json.dumps({"cwd": str(cwd)}) + "\n")
        for i in range(notes):
            (p / "memory" / f"{name}-{i}.md").write_text(f"# {name} {i}\n")
        return p

    def _doc(self, search=None):
        return self.vw.build(search or self.work, 3, self.books)

    def _accounted(self, doc):
        return (sum(len(r.get("pathStores") or []) for r in doc["repos"])
                + len(doc.get("unattributed", []))
                + len(doc["orphans"]))

    def test_every_store_of_a_multi_store_repo_survives(self):
        """Claude Code keys a store per cwd, so one repo commonly owns several.
        The previous shape kept whichever sorted last and dropped the rest."""
        repo = make_repo(self.work / "app", "https://github.com/acme/app")
        (repo / "packages" / "api").mkdir(parents=True)
        (repo / "packages" / "web").mkdir(parents=True)
        self._store("s1", repo)
        self._store("s2", repo / "packages" / "api")
        self._store("s3", repo / "packages" / "web")
        doc = self._doc()
        self.assertEqual(doc["totals"]["stores"], 3)
        self.assertEqual(self._accounted(doc), 3)
        self.assertEqual(sum(e["notes"] for r in doc["repos"]
                             for e in (r.get("pathStores") or [])), 6)

    def test_totals_reconcile_with_the_lists(self):
        repo = make_repo(self.work / "app", "https://github.com/acme/app")
        self._store("s1", repo)
        self._store("s2", repo)
        doc = self._doc()
        self.assertEqual(self._accounted(doc), doc["totals"]["stores"])

    def test_live_store_outside_the_search_root_is_recorded_and_warned(self):
        """Its cwd exists, so it is not an orphan; no repo in scope claims it,
        so it belonged to no list at all and vanished."""
        make_repo(self.work / "inside", "https://github.com/acme/inside")
        outside = self.tmp / "outside" / "elsewhere"
        outside.mkdir(parents=True)
        self._store("far", outside)
        doc = self._doc()
        self.assertEqual(len(doc["unattributed"]), 1)
        self.assertEqual(doc["totals"]["unattributedStores"], 1)
        self.assertIn(self.vw.W_STORE_OUTSIDE_ROOT,
                      {w["kind"] for w in doc["warnings"]})
        self.assertEqual(self._accounted(doc), doc["totals"]["stores"])

    def test_missing_books_root_warns_rather_than_reading_as_clean(self):
        make_repo(self.work / "r", "https://github.com/acme/r")
        doc = self.vw.build(self.work, 3, self.tmp / "does-not-exist")
        self.assertIn(self.vw.W_BOOKS_ROOT_MISSING,
                      {w["kind"] for w in doc["warnings"]})

    def test_unreadable_book_does_not_take_the_document_down(self):
        # POSIX-only by construction: chmod 000 does not make a directory
        # unreadable on Windows, and os.geteuid does not exist there at all, so
        # the premise cannot be set up rather than the behaviour being different.
        if sys.platform == "win32":
            self.skipTest("directory permissions do not work this way on Windows")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        make_repo(self.work / "r", "https://github.com/acme/r")
        for name in ("good", "locked"):
            (self.books / name / "memory").mkdir(parents=True)
            (self.books / name / "memory" / "n.md").write_text("# n\n")
        locked = self.books / "locked" / "memory"
        os.chmod(locked, 0o000)
        try:
            doc = self._doc()                       # must not raise
            self.assertGreaterEqual(len(doc["books"]), 1)
        finally:
            os.chmod(locked, 0o755)
