#!/usr/bin/env python3

import hashlib
import argparse
import gzip
import io
import json
import os
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from .contract_resources import contract_path
from .sops_validation import validate_sops_document

from .validate_manifest import (
    load_json,
    load_yaml,
    validate_compose,
    validate_manifest,
)


DEFAULT_MANIFEST_SCHEMA = contract_path("platform-v1.schema.json")
DEFAULT_BUNDLE_SCHEMA = contract_path("bundle-v1.schema.json")


class BundleError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def resolve_app_file(
    app_root: Path,
    relative_path: str,
    label: str,
) -> Path:
    root = app_root.resolve()
    candidate = root / relative_path

    if candidate.is_symlink():
        raise BundleError(f"{label} cannot be a symbolic link: {relative_path}")

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise BundleError(f"{label} does not exist: {relative_path}") from error

    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise BundleError(
            f"{label} escapes application root: {relative_path}"
        ) from error

    if not resolved.is_file():
        raise BundleError(f"{label} is not a regular file: {relative_path}")

    return resolved


def require_valid_sops_file(
    path: Path,
    relative_path: str,
    minimum_age_recipients: int = 1,
) -> None:
    document = load_yaml(path)
    errors = validate_sops_document(
        document,
        minimum_age_recipients=minimum_age_recipients,
    )

    if errors:
        raise BundleError(f"{errors[0]}: {relative_path}")


def collect_bundle(
    manifest_path: Path,
    app_root: Path = None,
    minimum_age_recipients: int = 1,
) -> tuple[dict[str, Any], dict[str, Path]]:
    if manifest_path.is_symlink():
        raise BundleError("manifest cannot be a symbolic link")

    resolved_manifest = manifest_path.resolve()

    if app_root is None:
        app_root = resolved_manifest.parent.parent

    resolved_root = app_root.resolve()

    try:
        manifest_relative = resolved_manifest.relative_to(resolved_root).as_posix()
    except ValueError as error:
        raise BundleError(
            f"manifest escapes application root: {manifest_path}"
        ) from error

    manifest_file = resolve_app_file(
        resolved_root,
        manifest_relative,
        "manifest",
    )

    manifest = load_yaml(manifest_file)
    manifest_schema = load_json(DEFAULT_MANIFEST_SCHEMA)
    manifest_errors = validate_manifest(manifest, manifest_schema)

    if manifest_errors:
        raise BundleError(
            "invalid application manifest:\n" + "\n".join(manifest_errors)
        )

    compose_relative = manifest["compose_file"]
    compose_file = resolve_app_file(
        resolved_root,
        compose_relative,
        "compose file",
    )
    compose = load_yaml(compose_file)
    compose_errors = validate_compose(manifest, compose)

    if compose_errors:
        raise BundleError(
            "invalid application Compose file:\n" + "\n".join(compose_errors)
        )

    secrets_relative = manifest["secrets"]["source"]
    secrets_file = resolve_app_file(
        resolved_root,
        secrets_relative,
        "secrets file",
    )
    require_valid_sops_file(
        secrets_file,
        secrets_relative,
        minimum_age_recipients=minimum_age_recipients,
    )

    files = {
        "manifest": manifest_file,
        "compose": compose_file,
        "secrets": secrets_file,
    }

    relative_paths = {
        "manifest": manifest_relative,
        "compose": compose_relative,
        "secrets": secrets_relative,
    }

    metadata = {
        "api_version": "platform-bundle/v1",
        "project": manifest["project"],
        "environment": manifest["environment"],
        "files": {
            name: {
                "path": relative_paths[name],
                "sha256": sha256_file(path),
            }
            for name, path in files.items()
        },
    }

    bundle_schema = load_json(DEFAULT_BUNDLE_SCHEMA)
    metadata_errors = validate_manifest(metadata, bundle_schema)

    if metadata_errors:
        raise BundleError(
            "invalid deployment bundle metadata:\n" + "\n".join(metadata_errors)
        )

    return metadata, files


def add_archive_file(
    archive: tarfile.TarFile,
    archive_path: str,
    content: bytes,
) -> None:
    info = tarfile.TarInfo(name=archive_path)
    info.size = len(content)
    info.mode = 0o600
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""

    archive.addfile(info, io.BytesIO(content))


def create_bundle(
    manifest_path: Path,
    output_path: Path,
    app_root: Path = None,
    minimum_age_recipients: int = 1,
) -> str:
    metadata, files = collect_bundle(
        manifest_path,
        app_root=app_root,
        minimum_age_recipients=minimum_age_recipients,
    )

    if output_path.is_symlink():
        raise BundleError(f"bundle output cannot be a symbolic link: {output_path}")

    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    metadata_content = (
        json.dumps(
            metadata,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )

    temporary_file = tempfile.NamedTemporaryFile(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
        delete=False,
    )
    temporary_path = Path(temporary_file.name)
    temporary_file.close()

    try:
        with temporary_path.open("wb") as raw_file:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_file,
                mtime=0,
            ) as gzip_file:
                with tarfile.open(
                    mode="w",
                    fileobj=gzip_file,
                    format=tarfile.USTAR_FORMAT,
                ) as archive:
                    add_archive_file(
                        archive,
                        "platform-bundle.json",
                        metadata_content,
                    )

                    for name in sorted(
                        files,
                        key=lambda item: metadata["files"][item]["path"],
                    ):
                        add_archive_file(
                            archive,
                            metadata["files"][name]["path"],
                            files[name].read_bytes(),
                        )

        temporary_path.chmod(0o600)
        os.replace(temporary_path, output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return sha256_file(output_path)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a platform/v1 deployment bundle.",
    )
    parser.add_argument(
        "manifest",
        type=Path,
        help="Path to deploy/platform.yml.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output .tar.gz bundle path.",
    )
    parser.add_argument(
        "--app-root",
        type=Path,
        help=(
            "Application repository root. "
            "Default: parent of the manifest deploy directory."
        ),
    )
    parser.add_argument(
        "--minimum-age-recipients",
        type=int,
        default=1,
        help="Minimum number of unique SOPS age recipients. Default: 1.",
    )

    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    try:
        digest = create_bundle(
            arguments.manifest,
            arguments.output,
            app_root=arguments.app_root,
            minimum_age_recipients=arguments.minimum_age_recipients,
        )
    except (BundleError, OSError, ValueError) as error:
        print(f"bundle error: {error}")
        return 1

    print(f"created deployment bundle: {arguments.output}")
    print(f"bundle sha256: {digest}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
