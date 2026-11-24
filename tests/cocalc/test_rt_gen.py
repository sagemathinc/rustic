"""Self-tests of the tree generator and the shrinker (rt/gen.py).

Run with: python3 -B -m unittest discover -s tests/cocalc -p 'test_*.py'
No rustic binary and no root are needed.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rt import gen  # noqa: E402
from rt.manifest import compare, manifest  # noqa: E402

SEEDS = [f"self-test-{i}" for i in range(50)]


class Generator(unittest.TestCase):
    def test_same_seed_same_spec(self):
        self.assertEqual(gen.generate("s"), gen.generate("s"))
        self.assertEqual(json.loads(json.dumps(gen.generate("s"))), gen.generate("s"))  # specs survive JSON

    def test_seeds_differ(self):
        # the entries, not the whole spec: every spec also records its own seed string
        self.assertEqual(len({json.dumps(gen.generate(s)["entries"]) for s in SEEDS[:10]}), 10)

    def test_every_shape_is_generated(self):
        seen = set()
        for s in SEEDS:
            seen |= gen.shapes(gen.generate(s))
        self.assertEqual(seen, set(gen.SHAPES))

    def test_bounds_and_names(self):
        for s in SEEDS:
            spec = gen.generate(s)
            st = gen.stats(spec)
            with self.subTest(s):
                self.assertLessEqual(st["entries"], gen.MAX_ENTRIES)
                self.assertLessEqual(st["bytes"], gen.MAX_BYTES)
                self.assertLessEqual(st["depth"], gen.MAX_DEPTH)
                names = [p for e in spec["entries"] for p in e["path"].split("/")]
                self.assertNotIn(".snapshots", names)
                self.assertFalse({"", ".", ".."} & set(names))


    def test_depth_bound_is_enforced(self):
        # no seed above reaches depth 12, so the bound is checked at a depth they all exceed
        saved = gen.MAX_DEPTH
        gen.MAX_DEPTH = 2
        try:
            depths = [gen.stats(gen.generate(s))["depth"] for s in SEEDS[:20]]
        finally:
            gen.MAX_DEPTH = saved
        self.assertLessEqual(max(depths), 2, depths)
        self.assertEqual(max(depths), 2, "the seeds must reach the bound, or this test proves nothing")


class Build(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def small_spec(self):
        # the first seed with every entry kind and at most 60 entries: quick to build
        for s in (f"build-{i}" for i in range(500)):
            spec = gen.generate(s)
            kinds = {e["kind"] for e in spec["entries"]}
            if len(spec["entries"]) <= 60 and {"file", "dir", "hardlink", "reflink", "symlink"} <= kinds:
                return spec
        self.fail("no small spec with every kind in 500 seeds")

    def test_reflink_fallback_is_recorded(self):
        spec = self.small_spec()
        (self.tmp / "a").mkdir()
        fallbacks = gen.build(spec, self.tmp / "a", reflink=False)
        reflinks = [e["path"] for e in spec["entries"] if e["kind"] == "reflink"]
        self.assertEqual(sorted(f.split(": ", 1)[1] for f in fallbacks if f.startswith("reflink->copy")), sorted(reflinks))
        for e in spec["entries"]:
            if e["kind"] == "reflink":
                self.assertEqual((self.tmp / "a" / e["path"]).read_bytes(), (self.tmp / "a" / e["source"]).read_bytes())

    def test_a_printed_spec_replays_the_same_tree(self):
        spec = self.small_spec()
        replayed = json.loads(json.dumps(spec))
        (self.tmp / "a").mkdir()
        (self.tmp / "b").mkdir()
        gen.build(spec, self.tmp / "a", reflink=False)
        gen.build(replayed, self.tmp / "b", reflink=False)
        self.assertEqual(compare(manifest(self.tmp / "a"), manifest(self.tmp / "b")), [])


class Shrink(unittest.TestCase):
    def spec(self):
        # a mid-sized spec with a file below the top level
        for s in (f"shrink-{i}" for i in range(500)):
            spec = gen.generate(s)
            if 40 <= len(spec["entries"]) <= 150 and any(e["kind"] == "file" and "/" in e["path"] for e in spec["entries"]):
                return spec
        self.fail("no mid-sized spec in 500 seeds")

    def needle(self, spec):
        return next(e for e in spec["entries"] if e["kind"] == "file" and "/" in e["path"])

    def test_reaches_the_minimal_failing_spec(self):
        spec = self.spec()
        needle = self.needle(spec)["path"]
        fails = lambda s: "missing" if any(e["path"] == needle for e in s["entries"]) else None  # noqa: E731
        small, _ = gen.shrink(spec, fails, max_runs=10_000)
        ancestors = {needle.rsplit("/", i)[0] for i in range(1, needle.count("/") + 1)}
        self.assertEqual({e["path"] for e in small["entries"]}, ancestors | {needle})

    def test_never_drops_the_failing_entry_and_keeps_its_class(self):
        spec = self.spec()
        needle = self.needle(spec)["path"]
        calls = []

        def fails(s):
            calls.append(s)
            return "differs" if any(e["path"] == needle for e in s["entries"]) else "error"

        small, runs = gen.shrink(spec, fails, max_runs=40)
        self.assertIn(needle, {e["path"] for e in small["entries"]})
        self.assertEqual(fails(small), "differs")
        self.assertEqual(runs, len(calls) - 1)  # the last call above is ours
        self.assertLessEqual(runs, 40)


if __name__ == "__main__":
    unittest.main()
