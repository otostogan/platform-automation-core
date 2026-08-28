#!/usr/bin/env python3
"""Reject private data and repository-coupled paths in release archives."""

import argparse
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable, Iterator, Tuple

from .boundary_policy import (
    describe_issues,
    runtime_forbidden_markers,
    validate_content,
    validate_path,
)


def archive_members(path: Path) -> Iterator[Tuple[str, bytes]]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    yield info.filename, archive.read(info)
        return

    if tarfile.is_tarfile(path):
        with tarfile.open(path, mode="r:*") as archive:
            for info in archive.getmembers():
                if info.isfile():
                    extracted = archive.extractfile(info)
                    if extracted is None:
                        raise ValueError(f"cannot read archive member: {info.name}")
                    yield info.name, extracted.read()
        return

    raise ValueError(f"unsupported release artifact: {path}")


def validate_member_name(artifact: Path, name: str) -> None:
    issues = validate_path(name, forbid_inventory=True)
    if issues:
        raise ValueError(f"{artifact}: {describe_issues(issues)} in {name}")


def validate_artifact(path: Path) -> int:
    members = 0

    for name, content in archive_members(path):
        members += 1
        validate_member_name(path, name)

        issues = validate_content(content)
        if issues:
            raise ValueError(f"{path}: {describe_issues(issues)} in {name}")

    if members == 0:
        raise ValueError(f"{path}: artifact contains no regular files")

    return members


def parse_arguments(arguments: Iterable[str] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    return parser.parse_args(arguments)


def main(arguments: Iterable[str] = None) -> int:
    options = parse_arguments(arguments)

    try:
        runtime_markers = runtime_forbidden_markers()
        print(
            f"artifact boundary policy: {len(runtime_markers)} "
            "runtime marker(s) loaded"
        )
        for artifact in options.artifacts:
            members = validate_artifact(artifact)
            print(f"artifact boundary check passed: {artifact} ({members} files)")
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        print(f"artifact boundary check failed: {error}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
