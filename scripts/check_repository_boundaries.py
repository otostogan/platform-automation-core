#!/usr/bin/env python3
"""Reject customer data, private material, and local files in the core tree."""

import subprocess
from pathlib import Path

from .boundary_policy import (
    describe_issues,
    runtime_forbidden_markers,
    validate_content,
    validate_path,
)


def tracked_paths() -> list[str]:
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
    )
    return [
        item.decode("utf-8")
        for item in output.split(b"\0")
        if item and Path(item.decode("utf-8")).exists()
    ]


def main() -> int:
    try:
        runtime_markers = runtime_forbidden_markers()
    except (OSError, UnicodeError, ValueError) as error:
        print(f"repository boundary policy configuration failed: {error}")
        return 1

    paths = tracked_paths()
    failures = []

    for name in paths:
        path_issues = validate_path(name)
        try:
            content = Path(name).read_bytes()
        except OSError as error:
            failures.append(f"{name}: cannot read tracked file: {error}")
            continue
        content_issues = validate_content(content)

        issues = [*path_issues, *content_issues]
        if issues:
            failures.append(f"{name}: {describe_issues(issues)}")

    if failures:
        print("repository boundary check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(
        "repository boundary check passed: "
        f"{len(paths)} tracked files, "
        f"{len(runtime_markers)} runtime marker(s) loaded"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
