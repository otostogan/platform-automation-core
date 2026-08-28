#!/usr/bin/env python3

import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath

from .verify_bundle import (
    METADATA_PATH,
    VerifiedBundle,
    validate_archive_path,
)


PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_ENVIRONMENTS = {"lab", "staging", "production"}


class BundleStagingError(ValueError):
    pass


def create_private_directory(
    root: Path,
    relative_path: PurePosixPath,
) -> Path:
    current = root

    for part in relative_path.parts:
        current = current / part
        current.mkdir(mode=0o700, exist_ok=True)
        current.chmod(0o700)

    return current


def write_private_file(
    root: Path,
    relative_path: str,
    content: bytes,
) -> None:
    validate_archive_path(relative_path)
    path = PurePosixPath(relative_path)

    create_private_directory(
        root,
        PurePosixPath(*path.parts[:-1]),
    )

    destination = root.joinpath(*path.parts)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL

    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        descriptor = os.open(
            destination,
            flags,
            0o600,
        )
    except OSError as error:
        raise BundleStagingError(
            f"cannot create staged file: {relative_path}"
        ) from error

    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def validate_stage_identity(
    project: str,
    environment: str,
    digest: str,
) -> None:
    if not PROJECT_PATTERN.fullmatch(project):
        raise BundleStagingError(f"invalid project for staging: {project}")

    if environment not in ALLOWED_ENVIRONMENTS:
        raise BundleStagingError(f"invalid environment for staging: {environment}")

    if not DIGEST_PATTERN.fullmatch(digest):
        raise BundleStagingError(f"invalid bundle digest for staging: {digest}")


def build_staged_files(
    verified: VerifiedBundle,
) -> dict[str, bytes]:
    metadata_content = (
        json.dumps(
            verified.metadata,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )

    staged_files = {
        METADATA_PATH: metadata_content,
    }

    for name, content in verified.files.items():
        relative_path = verified.metadata["files"][name]["path"]
        validate_archive_path(relative_path)

        if relative_path in staged_files:
            raise BundleStagingError(f"duplicate staged file path: {relative_path}")

        staged_files[relative_path] = content

    return staged_files


def validate_existing_stage(
    destination: Path,
    expected_files: dict[str, bytes],
) -> Path:
    if destination.is_symlink() or not destination.is_dir():
        raise BundleStagingError(f"existing staged bundle is invalid: {destination}")

    if stat.S_IMODE(destination.stat().st_mode) != 0o700:
        raise BundleStagingError(
            f"existing staged bundle has unsafe permissions: " f"{destination}"
        )

    discovered_files: set[str] = set()

    for path in destination.rglob("*"):
        relative_path = path.relative_to(destination).as_posix()

        if path.is_symlink():
            raise BundleStagingError(
                f"existing staged bundle contains a symbolic link: " f"{relative_path}"
            )

        if path.is_dir():
            if stat.S_IMODE(path.stat().st_mode) != 0o700:
                raise BundleStagingError(
                    f"staged directory has unsafe permissions: " f"{relative_path}"
                )

            continue

        if not path.is_file():
            raise BundleStagingError(
                f"existing staged bundle contains an invalid entry: " f"{relative_path}"
            )

        discovered_files.add(relative_path)

        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise BundleStagingError(
                f"staged file has unsafe permissions: " f"{relative_path}"
            )

        expected_content = expected_files.get(relative_path)

        if expected_content is None or path.read_bytes() != expected_content:
            raise BundleStagingError(
                f"existing staged bundle content mismatch: " f"{relative_path}"
            )

    if discovered_files != set(expected_files):
        raise BundleStagingError("existing staged bundle file set mismatch")

    return destination


def stage_verified_bundle(
    verified: VerifiedBundle,
    releases_root: Path,
) -> Path:
    project = verified.metadata["project"]
    environment = verified.metadata["environment"]
    digest = verified.digest
    staged_files = build_staged_files(verified)

    validate_stage_identity(
        project,
        environment,
        digest,
    )

    releases_root.mkdir(
        mode=0o750,
        parents=True,
        exist_ok=True,
    )
    releases_root = releases_root.resolve()

    bundle_parent = releases_root / project / environment / "bundles"
    bundle_parent.mkdir(
        mode=0o750,
        parents=True,
        exist_ok=True,
    )

    destination = bundle_parent / digest

    if destination.is_symlink():
        raise BundleStagingError(f"existing staged bundle is invalid: {destination}")

    if destination.exists():
        return validate_existing_stage(
            destination,
            staged_files,
        )

    temporary_directory = Path(
        tempfile.mkdtemp(
            prefix=f".{digest}.",
            dir=bundle_parent,
        )
    )
    temporary_directory.chmod(0o700)

    try:
        for relative_path, content in staged_files.items():
            write_private_file(
                temporary_directory,
                relative_path,
                content,
            )

        os.rename(
            temporary_directory,
            destination,
        )
    except Exception:
        if temporary_directory.exists():
            shutil.rmtree(temporary_directory)

        raise

    return destination
