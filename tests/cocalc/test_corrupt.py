"""Unit tests for corrupt.py's helpers (no rustic binary needed)."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corrupt import parse_index_list  # noqa: E402


class ParseIndexList(unittest.TestCase):
    def test_ids_starting_and_ending_in_letters_of_the_prefix(self):
        # `str.strip("IndexId()")` would eat a leading/trailing d or e
        ids = ["de" + "0" * 60 + "ed", "e" * 64, "d" + "1" * 62 + "e", "0123456789abcdef" * 4]
        text = "".join(f"IndexId({i})\n" for i in ids)
        self.assertEqual(parse_index_list(text), ids)

    def test_blank_lines_are_ignored(self):
        i = "a" * 64
        self.assertEqual(parse_index_list(f"\nIndexId({i})\n\n"), [i])

    def test_unexpected_lines_fail_loudly(self):
        for bad in ("[INFO] something", "IndexId(" + "a" * 63 + ")", "IndexId(" + "A" * 64 + ")",
                    "IndexId(" + "a" * 64 + ") extra", "a" * 64):
            with self.assertRaises(ValueError, msg=bad):
                parse_index_list(bad + "\n")


if __name__ == "__main__":
    unittest.main()
