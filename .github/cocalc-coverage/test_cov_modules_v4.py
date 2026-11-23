#!/usr/bin/env python3
"""Tests for cov_modules_v4.py: valid and minimal inputs, and every malformed class must exit 2
with empty stdout.  The shape here is the documented llvm-cov export; it is re-checked
against the first REAL export before any table counts (SPEC v1.2)."""
import copy
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("cm", os.path.join(HERE, "cov_modules_v4.py"))
cm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cm)

ROOT = "/repo"


def summ(lc, ln, rc=1, rn=2, fc=1, fn=1):
    return {"lines": {"count": ln, "covered": lc}, "regions": {"count": rn, "covered": rc},
            "functions": {"count": fn, "covered": fc}}


def export(files):
    tot = {k: {"count": 0, "covered": 0} for k in cm.KINDS}
    for f in files:
        for k in cm.KINDS:
            tot[k]["count"] += f["summary"][k]["count"]; tot[k]["covered"] += f["summary"][k]["covered"]
    return {"data": [{"files": files, "totals": tot}], "type": "llvm.coverage.json.export"}


VALID = export([
    {"filename": "/repo/crates/core/src/commands/prune.rs", "summary": summ(5, 10)},
    {"filename": "/repo/crates/core/src/commands/check.rs", "summary": summ(1, 4)},
    {"filename": "/repo/crates/core/src/lib.rs", "summary": summ(2, 2)},
    {"filename": "/repo/src/main.rs", "summary": summ(0, 3)},
    {"filename": "/home/x/.cargo/registry/dep.rs", "summary": summ(7, 9)},
])


def run(obj, root=ROOT, raw=None):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as t:
        t.write(raw if raw is not None else json.dumps(obj))
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cm.main(["x", t.name, root])
    finally:
        os.unlink(t.name)
    return rc, out.getvalue(), err.getvalue()


class T(unittest.TestCase):
    def ok(self, obj):
        rc, out, err = run(obj)
        self.assertEqual(rc, 0, err)
        return out

    def bad(self, obj=None, **kw):
        rc, out, err = run(obj, **kw)
        self.assertEqual((rc, out), (2, ""), err)
        self.assertTrue(err.startswith("cov_modules: "))

    def test_valid(self):
        out = self.ok(VALID)
        self.assertIn("| core | commands | 42.9% (6/14)", out)
        self.assertIn("| core | lib.rs | 100.0% (2/2)", out)
        self.assertIn("| (root) | main.rs | 0.0% (0/3)", out)
        self.assertIn("| core | commands | 42.9% (6/14)", out)
        self.assertIn("files outside /repo: 1", out)
        self.assertNotIn("dep.rs", out)

    def test_minimal(self):
        self.assertIn("| (root) | a/x.rs |", self.ok(export([{"filename": "/repo/a/x.rs", "summary": summ(0, 0, 0, 0, 0, 0)}])))

    def test_argv_and_io(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cm.main(["x"]), 2)
            self.assertEqual(cm.main(["x", "/nonexistent.json", ROOT]), 2)
        self.assertEqual(out.getvalue(), "")
        self.bad(VALID, root="relative/root")
        self.bad(raw="{not json")
        self.bad(raw="")

    def test_shapes(self):
        self.bad([])
        self.bad({})
        self.bad({"data": []})
        self.bad({"data": [VALID["data"][0], VALID["data"][0]]})
        self.bad({"data": [{"totals": VALID["data"][0]["totals"]}]})
        self.bad({"data": [{"files": VALID["data"][0]["files"]}]})  # no totals

    def mutate(self, fn):
        o = copy.deepcopy(VALID); fn(o); self.bad(o)

    def mutate_retotal(self, fn):
        """Mutate a file record, then make data[0].totals consistent again, so only the
        targeted check (not the totals self-check) can reject the input."""
        o = copy.deepcopy(VALID); fn(o)
        tot = {k: {"count": 0, "covered": 0} for k in cm.KINDS}
        for f in o["data"][0]["files"]:
            for k in cm.KINDS:
                tot[k]["count"] += f["summary"][k]["count"]; tot[k]["covered"] += f["summary"][k]["covered"]
        o["data"][0]["totals"] = tot
        self.bad(o)

    def test_isolated_checks(self):
        s = lambda o: o["data"][0]["files"][0]["summary"]["lines"]
        self.mutate_retotal(lambda o: s(o).__setitem__("covered", True))   # bool, would pass as 1
        self.mutate_retotal(lambda o: s(o).__setitem__("count", True) or s(o).__setitem__("covered", 0))
        self.mutate_retotal(lambda o: s(o).__setitem__("covered", 11))     # covered > count, totals fine
        self.mutate_retotal(lambda o: o["data"][0]["files"].append(copy.deepcopy(o["data"][0]["files"][0])))  # duplicate

    def test_file_entries(self):
        f = lambda o: o["data"][0]["files"][0]
        self.mutate(lambda o: f(o).pop("filename"))
        self.mutate(lambda o: f(o).__setitem__("filename", 7))
        self.mutate(lambda o: f(o).__setitem__("filename", ""))
        self.mutate(lambda o: f(o).__setitem__("filename", "crates/core/src/a.rs"))
        self.mutate(lambda o: f(o).__setitem__("filename", ROOT))
        self.mutate(lambda o: o["data"][0]["files"].append(copy.deepcopy(f(o))))
        self.mutate(lambda o: f(o).pop("summary"))
        self.mutate(lambda o: f(o)["summary"].pop("regions"))
        self.mutate(lambda o: o["data"][0]["files"].__setitem__(0, "x"))

    def test_counts(self):
        s = lambda o: o["data"][0]["files"][0]["summary"]["lines"]
        for bad in (True, -1, 1.5, "3", None):
            self.mutate(lambda o, b=bad: s(o).__setitem__("count", b))
            self.mutate(lambda o, b=bad: s(o).__setitem__("covered", b))
        self.mutate(lambda o: s(o).__setitem__("covered", 11))  # covered > count

    def test_totals_must_add_up(self):
        self.mutate(lambda o: o["data"][0]["totals"]["lines"].__setitem__("count", 999))
        self.mutate(lambda o: o["data"][0]["files"].pop())  # an outside file dropped

    def test_noncanonical_paths(self):
        f = lambda o: o["data"][0]["files"][0]
        for name in ("/repo/../outside.rs", "/repo/./crates/core/src/a.rs", "/repo//crates/a.rs",
                     "/repo/crates/a.rs/", "//repo/crates/a.rs", "/repo/crates/..", "/repo/."):
            self.mutate_retotal(lambda o, n=name: f(o).__setitem__("filename", n))
        for root in ("/repo/", "/repo/.", "/x/../repo", "//repo"):
            self.bad(VALID, root=root)

    def test_traversal_not_counted_inside(self):
        """The root-traversal spelling alone, totals consistent: rejected, not counted as /repo."""
        self.mutate_retotal(lambda o: o["data"][0]["files"].append(
            {"filename": "/repo/../etc/x.rs", "summary": summ(1, 1)}))

    def test_alias_duplicate(self):
        """The same file under a second, non-canonical spelling: rejected."""
        self.mutate_retotal(lambda o: o["data"][0]["files"].append(
            {"filename": "/repo/crates/core/src/./lib.rs", "summary": summ(2, 2)}))

    def test_module_keys(self):
        """Each path shape lands in its own crate and module (key() directly)."""
        P = cm.PurePosixPath
        cases = {
            "crates/core/src/commands/prune.rs": ("core", "commands"),
            "crates/core/src/lib.rs": ("core", "lib.rs"),
            "crates/core/tests/integration.rs": ("core", "tests/integration.rs"),
            "crates/core/tests/integration/prune.rs": ("core", "tests/integration"),
            "crates/backend/tests/x.rs": ("backend", "tests/x.rs"),
            "crates/core/build.rs": ("core", "build.rs"),
            "crates/core/benches/b/c.rs": ("core", "benches/b"),
            "examples/backup/src/main.rs": ("examples/backup", "main.rs"),
            "examples/backup/build.rs": ("examples/backup", "build.rs"),
            "src/commands/backup.rs": ("(root)", "commands"),
            "src/main.rs": ("(root)", "main.rs"),
            "tests/backup_restore.rs": ("(root)", "tests/backup_restore.rs"),
            "tests/generated/a/b.rs": ("(root)", "tests/generated"),
            "build.rs": ("(root)", "build.rs"),
            "src": ("(root)", "src"),
            "crates/core/src": ("core", "src"),
        }
        for rel, want in cases.items():
            self.assertEqual(cm.key(P(rel)), want, rel)

    def test_tests_dirs_not_merged_across_crates(self):
        """Two crates' tests stay in their own crates' rows (the v3 bug merged them under 'crates')."""
        out = self.ok(export([
            {"filename": "/repo/crates/core/tests/integration.rs", "summary": summ(1, 2)},
            {"filename": "/repo/crates/backend/tests/integration.rs", "summary": summ(3, 4)},
        ]))
        self.assertIn("| core | tests/integration.rs | 50.0% (1/2)", out)
        self.assertIn("| backend | tests/integration.rs | 75.0% (3/4)", out)
        self.assertNotIn("| crates |", out)

    def test_nothing_under_root(self):
        self.bad(export([{"filename": "/elsewhere/a.rs", "summary": summ(1, 1)}]))


if __name__ == "__main__":
    unittest.main()
