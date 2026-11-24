"""Self-tests of rt_snapshot.py's generated round trips, with stub rustic and btrfs.

The stubs copy trees with `cp -a --sparse=always` and log every call, so the real
Generated code runs without root, btrfs or rustic.  They check that every round
trip uses its own repository and leaves nothing behind: after a success, after
an injected failure, across shrink retries and when an unexpected exception
propagates.  Run with: python3 -B -m unittest discover -s tests/cocalc -p 'test_*.py'
"""
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rt_snapshot  # noqa: E402
from rt import gen  # noqa: E402
from rt.snapshot import Snap  # noqa: E402

RUSTIC = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
repo = args[args.index("--repository") + 1]
rest = [a for a in args if a not in ("--repository", repo, "--no-progress")]
cmd = rest[0]
with open(os.environ["RT_STUB_LOG"], "a") as log:
    log.write(f"rustic {cmd} {repo}\n")
if os.environ.get("RT_STUB_FAIL") == cmd:
    print(f"stub: injected failure in {cmd}", file=sys.stderr)
    sys.exit(1)
if cmd == "init":
    os.mkdir(repo)
    os.mkdir(os.path.join(repo, "snapshots"))
elif cmd == "backup":
    n = str(len(os.listdir(os.path.join(repo, "snapshots"))))
    subprocess.run(["cp", "-a", "--sparse=always", ".", os.path.join(repo, "snapshots", n)], check=True)
    print(json.dumps({"id": n}))
elif cmd == "restore":
    what, dest = rest[-2], rest[-1]
    subprocess.run(["cp", "-a", "--sparse=always", os.path.join(repo, "snapshots", what) + "/.", dest], check=True)
else:
    sys.exit(f"stub rustic: unknown command {cmd}")
'''

BTRFS = r'''#!/usr/bin/env python3
import os, shutil, subprocess, sys
a = sys.argv[1:]
with open(os.environ["RT_STUB_LOG"], "a") as log:
    log.write("btrfs " + " ".join(a) + "\n")
if a[:2] == ["subvolume", "create"]:
    os.mkdir(a[2])
elif a[:2] == ["subvolume", "snapshot"]:
    src, dst = a[-2], a[-1]
    subprocess.run(["cp", "-a", "--sparse=always", src, dst], check=True)
elif a[:2] == ["subvolume", "delete"]:
    shutil.rmtree(a[2])
elif a[:2] == ["property", "set"]:
    pass
else:
    sys.exit("stub btrfs: unknown " + " ".join(a))
'''

SPEC = {"version": 1, "seed": "stub", "entries": [
    {"path": "d", "kind": "dir", "mode": 0o755, "mtime_ns": 1_500_000_000_000_000_001},
    {"path": "e", "kind": "dir", "mode": 0o700, "mtime_ns": 1_500_000_000_000_000_002},
    {"path": "d/f", "kind": "file", "layout": "dense", "size": 3000, "seed": 7, "mode": 0o644, "mtime_ns": 1_500_000_000_000_000_003},
    {"path": "d/h", "kind": "hardlink", "target": "d/f"},
    {"path": "s", "kind": "symlink", "target": "d/f", "mtime_ns": 1_500_000_000_000_000_004},
    {"path": "t", "kind": "file", "layout": "sparse-tail", "size": 4 * 1024 * 1024, "seed": 8, "mode": 0o600, "mtime_ns": 1_500_000_000_000_000_005},
]}


class Runner(unittest.TestCase):
    on_btrfs = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        stubs = self.tmp / "bin"
        stubs.mkdir()
        for name, text in (("rustic", RUSTIC), ("btrfs", BTRFS)):
            (stubs / name).write_text(text)
            (stubs / name).chmod(0o755)
        self.log = self.tmp / "calls.log"
        self.log.write_text("")
        self._env = dict(os.environ)
        os.environ["PATH"] = f"{stubs}:{os.environ['PATH']}"
        os.environ["RT_STUB_LOG"] = str(self.log)
        os.environ.pop("RT_STUB_FAIL", None)
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.s = Snap(str(stubs / "rustic"), self.work, as_root=False)
        rt_snapshot.ensure_staging(self.s)
        self.g = rt_snapshot.Generated(self.s, self.on_btrfs)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def leftovers(self):
        return sorted(str(p.relative_to(self.work)) for p in [*self.work.glob("gen-*"), *self.s.staging.glob("gen-*")])

    def repos(self):
        return [line.split()[2] for line in self.log.read_text().splitlines() if line.startswith("rustic init ")]

    def test_success_uses_a_fresh_repo_each_time_and_removes_everything(self):
        self.assertEqual(self.g.problems(SPEC), [])
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.g.problems(SPEC), [])
        self.assertEqual(self.leftovers(), [])
        repos = self.repos()
        self.assertEqual(len(repos), 2)
        self.assertNotEqual(repos[0], repos[1])
        self.assertFalse(any(os.path.lexists(r) for r in repos))
        self.assertFalse((self.work / "repo").exists(), "no shared repository")

    def test_injected_failures_still_remove_everything(self):
        for step in ("init", "backup", "restore"):
            with self.subTest(step):
                os.environ["RT_STUB_FAIL"] = step
                problems = self.g.problems(SPEC)
                self.assertTrue(problems and problems[0].startswith("error: "), problems)
                self.assertIn(step, problems[0])  # the failing step itself is reported, not a later symptom
                self.assertFalse(any(p.startswith("cleanup failed") for p in problems), problems)
                self.assertEqual(self.leftovers(), [])
        self.assertEqual(len(set(self.repos())), 3)

    def test_shrink_retries_each_get_their_own_repo(self):
        os.environ["RT_STUB_FAIL"] = "restore"
        calls = []

        def fails(spec):
            calls.append(1)
            return gen.problem_class(self.g.problems(spec))

        gen.shrink(SPEC, fails, max_runs=4)
        self.assertEqual(len(self.repos()), len(calls))
        self.assertEqual(len(set(self.repos())), len(calls))
        self.assertEqual(self.leftovers(), [])

    def test_an_unexpected_exception_propagates_after_cleanup(self):
        saved = gen.build

        def broken(*args, **kw):
            raise TypeError("unexpected")

        gen.build = broken
        try:
            with self.assertRaises(TypeError):
                self.g.problems(SPEC)
        finally:
            gen.build = saved
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(any(os.path.lexists(r) for r in self.repos()))

    def failing_repo_removal(self):
        """Make every round trip's repository impossible to remove; returns the undo."""
        saved = rt_snapshot.Generated.remove

        def remove(gself, path, kind):
            if path.name.endswith("-repo"):
                raise OSError("injected: cannot remove")
            return saved(gself, path, kind)

        rt_snapshot.Generated.remove = remove
        return lambda: setattr(rt_snapshot.Generated, "remove", saved)

    def test_a_cleanup_failure_stops_the_generated_run(self):
        # after a clean round trip, and after a failing one (which would otherwise be shrunk)
        for label, fail in (("clean", None), ("failing", "restore")):
            with self.subTest(label):
                self.log.write_text("")
                if fail:
                    os.environ["RT_STUB_FAIL"] = fail
                else:
                    os.environ.pop("RT_STUB_FAIL", None)
                os.environ["RT_GEN_SEEDS"] = "3"
                os.environ["RT_GEN_SEED_BASE"] = label
                undo = self.failing_repo_removal()
                saved_gen = rt_snapshot.gen.generate
                rt_snapshot.gen.generate = lambda seed: SPEC
                try:
                    out = rt_snapshot.run_generated(self.s, self.on_btrfs)
                finally:
                    rt_snapshot.gen.generate = saved_gen
                    undo()
                self.assertIsNotNone(out["fatal"])
                self.assertEqual(out["fatal"]["seed"], f"{label}-0")  # stopped at the first seed
                self.assertTrue(any("cleanup failed" in x for x in out["fatal"]["leftovers"]), out["fatal"])
                self.assertEqual(out["failed"], {})  # no shrink result: nothing was shrunk
                self.assertEqual(len(self.repos()), 1, "exactly one round trip: no shrink retry, no next seed")

    def test_a_cleanup_failure_during_an_unexpected_exception_is_reported(self):
        undo = self.failing_repo_removal()
        saved = gen.build

        def broken(*args, **kw):
            raise TypeError("unexpected")

        gen.build = broken
        raised = None
        try:
            self.g.problems(SPEC)
        except Exception as err:  # noqa: BLE001  (any other type is a FAIL below, not an ERROR)
            raised = err
        finally:
            gen.build = saved
            undo()
        self.assertIsInstance(raised, rt_snapshot.CleanupFailed, f"raised {raised!r}")
        self.assertIsInstance(raised.__cause__, TypeError)
        self.assertTrue(raised.leftovers)
        self.assertIn("unexpected TypeError", raised.problems[0])

    def test_generated_only_creates_no_shared_repo(self):
        saved = rt_snapshot.gen.generate
        rt_snapshot.gen.generate = lambda seed: SPEC
        os.environ["RT_GEN_SEEDS"] = "2"
        try:
            out = rt_snapshot.run_generated(self.s, self.on_btrfs)
        finally:
            rt_snapshot.gen.generate = saved
        self.assertEqual(out["failed"], {})
        self.assertFalse((self.work / "repo").exists())
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(len(set(self.repos())), 2)


class RunnerOnBtrfs(Runner):
    """The same checks on the btrfs path: subvolumes, a read-only snapshot, subvolume deletes."""

    on_btrfs = True

    def test_the_btrfs_path_snapshots_and_deletes_subvolumes(self):
        self.assertEqual(self.g.problems(SPEC), [])
        calls = self.log.read_text().splitlines()
        self.assertTrue(any(c.startswith("btrfs subvolume snapshot -r ") for c in calls), calls)
        created = [c.split()[3] for c in calls if c.startswith("btrfs subvolume create ")]
        deleted = [c.split()[3] for c in calls if c.startswith("btrfs subvolume delete ")]
        self.assertEqual(len(created), 3)  # src, dense, sparse
        self.assertEqual(sorted(deleted), sorted(created + [c.split()[-1] for c in calls if c.startswith("btrfs subvolume snapshot ")]))


if __name__ == "__main__":
    unittest.main()
