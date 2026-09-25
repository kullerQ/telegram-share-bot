"""Tests for release version syntax and one-step Semantic Versioning bumps."""

from __future__ import annotations

import unittest

from scripts.check_release_version import is_next_version, parse_version, validate


class TestReleaseVersion(unittest.TestCase):
    def test_parses_three_part_stable_versions(self) -> None:
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3))

    def test_rejects_non_stable_or_non_canonical_versions(self) -> None:
        for value in ("v1.2.3", "01.2.3", "1.2", "1.2.3-beta.1", "1.2.3+build"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_version(value)

    def test_accepts_first_release_only_as_one_zero_zero(self) -> None:
        self.assertEqual(validate("1.0.0", None), "1.0.0")
        with self.assertRaises(ValueError):
            validate("0.1.0", None)

    def test_accepts_one_patch_minor_or_major_step(self) -> None:
        self.assertTrue(is_next_version((1, 2, 3), (1, 2, 4)))
        self.assertTrue(is_next_version((1, 2, 3), (1, 3, 0)))
        self.assertTrue(is_next_version((1, 2, 3), (2, 0, 0)))

    def test_rejects_skipped_or_mixed_steps(self) -> None:
        for current in ((1, 2, 5), (1, 4, 0), (2, 1, 0), (1, 2, 3)):
            with self.subTest(current=current):
                self.assertFalse(is_next_version((1, 2, 3), current))


if __name__ == "__main__":
    unittest.main()
