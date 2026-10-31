"""Self-tests of the round-trip harness: ways it could pass without testing anything.

Run with: python3 -B -m unittest discover -s tests/cocalc -p 'test_*.py'
No rustic binary or root is needed.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rt import scenarios  # noqa: E402
from rt.scenarios import RusticFailed  # noqa: E402

HERE = Path(__file__).resolve().parent


class FakeContext:
    """Stands in for rt.scenarios.Context: every failing command fails with `stderr`."""

    def __init__(self, work: Path, stderr: str):
        self.work = work
        self.stderr = stderr
        self.as_root = False

    def fresh(self, name):
        path = self.work / name
        path.mkdir()
        return path

    def backup(self, source, *args):
        return "0" * 64

    def run(self, *args, cwd=None):
        if "--strict" in args:
            raise RusticFailed(args, 1, self.stderr)
        if "--json" in args:
            return json.dumps({"id": "0" * 64, "summary": {"error_count": 2}}).encode()
        return b""

    def restore(self, what, target, *args):
        raise RusticFailed(["restore", what, str(target), *args], 1, self.stderr)


class CommandFailures(unittest.TestCase):
    """A scenario that expects a command to fail accepts only its own reason."""

    expected = {
        "deep_path_recorded": "Strict backup failed because source inspection or file processing was incomplete.",
        "mismatch_without_delete": "Cannot restore the directory `empty-dir`: the destination holds another type",
        "file_over_dir_without_delete": "Failed to set restore file length for `file`.\n"
        "Failed to set the length of the file `empty-file`.",
    }

    def run_scenario(self, name, stderr):
        with tempfile.TemporaryDirectory() as tmp:
            return getattr(scenarios, name)(FakeContext(Path(tmp), stderr))

    def test_an_unrelated_failure_is_a_problem(self):
        for name in self.expected:
            with self.subTest(name):
                problems = self.run_scenario(name, "error: repository is locked by another process")
                self.assertTrue(problems, f"{name} accepted an unrelated failure")
                self.assertTrue(all("failed for another reason" in p for p in problems), problems)

    def test_the_expected_failure_is_not(self):
        for name, stderr in self.expected.items():
            with self.subTest(name):
                self.assertEqual(self.run_scenario(name, stderr), [])


class Selection(unittest.TestCase):
    def test_unknown_names_are_an_error(self):
        with self.assertRaises(scenarios.UnknownScenario):
            scenarios.select({"full_dense", "no_such_scenario"})
        self.assertEqual([s.__name__ for s in scenarios.select({"subtree"})], ["subtree"])
        self.assertEqual(scenarios.select(set()), scenarios.SCENARIOS)

    def test_the_runner_fails_on_an_unknown_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-B", str(HERE / "roundtrip.py"), sys.executable, tmp, "ful_dense"],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unknown scenarios", result.stderr)


class ExpectedFailures(unittest.TestCase):
    def test_only_matching_problems_are_expected(self):
        self.assertEqual(scenarios.unexplained("over_existing", ["missing: 'empty-dir'"]), [])
        for other in ["missing: 'empty-dir/x'", "differs: 'plain' {...}", "error: rustic restore exited 1"]:
            with self.subTest(other):
                self.assertEqual(scenarios.unexplained("over_existing", ["missing: 'empty-dir'", other]), [other])

    def test_unlisted_scenarios_keep_every_problem(self):
        self.assertEqual(scenarios.unexplained("full_dense", ["missing: 'empty-dir'"]), ["missing: 'empty-dir'"])

    def test_every_listed_scenario_exists(self):
        names = {s.__name__ for s in scenarios.SCENARIOS}
        self.assertLessEqual(set(scenarios.EXPECTED_FAILURES), names)


if __name__ == "__main__":
    unittest.main()
