"""Validate the single semantic version assigned to a release PR."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def parse_version(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("VERSION must use stable MAJOR.MINOR.PATCH form, e.g. 1.0.0")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def is_next_version(previous: tuple[int, int, int] | None, current: tuple[int, int, int]) -> bool:
    if previous is None:
        return current == (1, 0, 0)
    major, minor, patch = previous
    return current in {
        (major + 1, 0, 0),
        (major, minor + 1, 0),
        (major, minor, patch + 1),
    }


def _version_at_ref(ref: str) -> str | None:
    git_command = shutil.which("git") or shutil.which("git.exe")
    if git_command is None:
        raise RuntimeError("Git is required to compare release versions")
    result = subprocess.run(
        [git_command, "show", f"{ref}:VERSION"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def validate(current_value: str, previous_value: str | None) -> str:
    current = parse_version(current_value)
    previous = parse_version(previous_value) if previous_value is not None else None
    if not is_next_version(previous, current):
        if previous is None:
            raise ValueError("The first release must be 1.0.0")
        raise ValueError(
            f"VERSION must bump exactly one SemVer component from {previous_value}; "
            "choose the next patch, minor, or major version"
        )
    return ".".join(str(part) for part in current)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True, help="Git ref to compare VERSION against")
    args = parser.parse_args(argv)

    current_value = Path("VERSION").read_text(encoding="utf-8").strip()
    previous_value = _version_at_ref(args.base_ref)
    try:
        version = validate(current_value, previous_value)
    except ValueError as exc:
        print(f"Release version check failed: {exc}", file=sys.stderr)
        return 1

    print(f"Validated release version {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
