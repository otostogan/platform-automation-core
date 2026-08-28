#!/usr/bin/env python3

import hashlib
import io
import json
import tarfile
import yaml
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .contract_resources import contract_path
from .sops_validation import validate_sops_document
from .validate_manifest import (
    load_json,
    validate_compose,
    validate_manifest,
)


DEFAULT_BUNDLE_SCHEMA = contract_path("bundle-v1.schema.json")
DEFAULT_MANIFEST_SCHEMA = contract_path("platform-v1.schema.json")

METADATA_PATH = "platform-bundle.json"
EXPECTED_MEMBER_COUNT = 4

MAX_BUNDLE_BYTES = 10 * 1024 * 1024
MAX_MEMBER_BYTES = 5 * 1024 * 1024
MAX_TOTAL_MEMBER_BYTES = 10 * 1024 * 1024


class BundleVerificationError(ValueError):
    pass


@dataclass(frozen=True)
class VerifiedBundle:
    digest: str
    metadata: dict[str, Any]
    files: dict[str, bytes]
    manifest: dict[str, Any]
    compose: dict[str, Any]
    secrets: dict[str, Any]


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def validate_archive_path(name: str) -> None:
    if not name or "\\" in name:
        raise BundleVerificationError(f"unsafe archive member path: {name!r}")

    path = PurePosixPath(name)

    if path.is_absolute() or ".." in path.parts:
        raise BundleVerificationError(f"unsafe archive member path: {name!r}")

    if path.as_posix() != name:
        raise BundleVerificationError(f"non-canonical archive member path: {name!r}")


def read_archive_members(bundle_content: bytes) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    total_size = 0

    try:
        with tarfile.open(
            mode="r:gz",
            fileobj=io.BytesIO(bundle_content),
        ) as archive:
            archive_members = archive.getmembers()

            if len(archive_members) != EXPECTED_MEMBER_COUNT:
                raise BundleVerificationError(
                    "deployment bundle must contain exactly "
                    f"{EXPECTED_MEMBER_COUNT} files"
                )

            for member in archive_members:
                validate_archive_path(member.name)

                if member.name in members:
                    raise BundleVerificationError(
                        f"duplicate archive member: {member.name}"
                    )

                if not member.isreg():
                    raise BundleVerificationError(
                        f"archive member is not a regular file: " f"{member.name}"
                    )

                if member.size > MAX_MEMBER_BYTES:
                    raise BundleVerificationError(
                        f"archive member is too large: {member.name}"
                    )

                total_size += member.size

                if total_size > MAX_TOTAL_MEMBER_BYTES:
                    raise BundleVerificationError(
                        "deployment bundle expands beyond the size limit"
                    )

                extracted_file = archive.extractfile(member)

                if extracted_file is None:
                    raise BundleVerificationError(
                        f"cannot read archive member: {member.name}"
                    )

                content = extracted_file.read(MAX_MEMBER_BYTES + 1)

                if len(content) != member.size:
                    raise BundleVerificationError(
                        f"archive member size mismatch: {member.name}"
                    )

                members[member.name] = content
    except BundleVerificationError:
        raise
    except (tarfile.TarError, EOFError, OSError) as error:
        raise BundleVerificationError("invalid deployment bundle archive") from error

    return members


def load_yaml_member(
    content: bytes,
    path: str,
) -> Any:
    try:
        return yaml.safe_load(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise BundleVerificationError(
            f"bundle file is not valid YAML: {path}"
        ) from error


def validate_embedded_contract(
    metadata: dict[str, Any],
    files: dict[str, bytes],
    minimum_age_recipients: int = 1,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    declared_files = metadata["files"]

    manifest_path = declared_files["manifest"]["path"]
    compose_path = declared_files["compose"]["path"]
    secrets_path = declared_files["secrets"]["path"]

    manifest = load_yaml_member(
        files["manifest"],
        manifest_path,
    )
    manifest_schema = load_json(DEFAULT_MANIFEST_SCHEMA)
    manifest_errors = validate_manifest(
        manifest,
        manifest_schema,
    )

    if manifest_errors:
        raise BundleVerificationError(
            "invalid embedded application manifest:\n" + "\n".join(manifest_errors)
        )

    if metadata["project"] != manifest["project"]:
        raise BundleVerificationError("bundle project does not match embedded manifest")

    if metadata["environment"] != manifest["environment"]:
        raise BundleVerificationError(
            "bundle environment does not match embedded manifest"
        )

    if compose_path != manifest["compose_file"]:
        raise BundleVerificationError(
            "bundle Compose path does not match embedded manifest"
        )

    if secrets_path != manifest["secrets"]["source"]:
        raise BundleVerificationError(
            "bundle secrets path does not match embedded manifest"
        )

    compose = load_yaml_member(
        files["compose"],
        compose_path,
    )
    compose_errors = validate_compose(
        manifest,
        compose,
    )

    if compose_errors:
        raise BundleVerificationError(
            "invalid embedded application Compose file:\n" + "\n".join(compose_errors)
        )

    secrets = load_yaml_member(
        files["secrets"],
        secrets_path,
    )
    sops_errors = validate_sops_document(
        secrets,
        minimum_age_recipients=minimum_age_recipients,
    )

    if sops_errors:
        raise BundleVerificationError(
            "invalid embedded SOPS secrets:\n" + "\n".join(sops_errors)
        )

    return manifest, compose, secrets


def validate_bundle_members(
    members: dict[str, bytes],
    minimum_age_recipients: int = 1,
) -> tuple:
    if len(members) != EXPECTED_MEMBER_COUNT:
        raise BundleVerificationError(
            "deployment bundle must contain exactly " f"{EXPECTED_MEMBER_COUNT} files"
        )

    for path, content in members.items():
        validate_archive_path(path)

        if len(content) > MAX_MEMBER_BYTES:
            raise BundleVerificationError("bundle file exceeds size limit")

    if sum(len(content) for content in members.values()) > MAX_TOTAL_MEMBER_BYTES:
        raise BundleVerificationError("bundle contents exceed total size limit")

    metadata_content = members.get(METADATA_PATH)

    if metadata_content is None:
        raise BundleVerificationError(f"deployment bundle has no {METADATA_PATH}")

    try:
        metadata = json.loads(metadata_content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleVerificationError(f"{METADATA_PATH} is not valid JSON") from error

    bundle_schema = load_json(DEFAULT_BUNDLE_SCHEMA)
    metadata_errors = validate_manifest(
        metadata,
        bundle_schema,
    )

    if metadata_errors:
        raise BundleVerificationError(
            "invalid deployment bundle metadata:\n" + "\n".join(metadata_errors)
        )

    declared_files = metadata["files"]
    declared_paths = [descriptor["path"] for descriptor in declared_files.values()]

    if len(set(declared_paths)) != len(declared_paths):
        raise BundleVerificationError("deployment bundle declares duplicate file paths")

    expected_paths = {
        METADATA_PATH,
        *declared_paths,
    }

    if set(members) != expected_paths:
        raise BundleVerificationError(
            "archive members do not match deployment bundle metadata"
        )

    verified_files: dict[str, bytes] = {}

    for name, descriptor in declared_files.items():
        path = descriptor["path"]
        content = members[path]
        actual_digest = sha256_bytes(content)

        if actual_digest != descriptor["sha256"]:
            raise BundleVerificationError(f"SHA-256 mismatch for bundle file: {path}")

        verified_files[name] = content

    manifest, compose, secrets = validate_embedded_contract(
        metadata,
        verified_files,
        minimum_age_recipients=minimum_age_recipients,
    )

    return metadata, verified_files, manifest, compose, secrets


def verify_bundle(
    bundle_path: Path,
    minimum_age_recipients: int = 1,
) -> VerifiedBundle:
    if bundle_path.is_symlink():
        raise BundleVerificationError(
            f"deployment bundle cannot be a symbolic link: {bundle_path}"
        )

    try:
        bundle_size = bundle_path.stat().st_size
    except FileNotFoundError as error:
        raise BundleVerificationError(
            f"deployment bundle does not exist: {bundle_path}"
        ) from error

    if bundle_size > MAX_BUNDLE_BYTES:
        raise BundleVerificationError(
            "compressed deployment bundle exceeds the size limit"
        )

    with bundle_path.open("rb") as file:
        bundle_content = file.read(MAX_BUNDLE_BYTES + 1)

    if len(bundle_content) > MAX_BUNDLE_BYTES:
        raise BundleVerificationError(
            "compressed deployment bundle exceeds the size limit"
        )

    members = read_archive_members(bundle_content)
    metadata, files, manifest, compose, secrets = validate_bundle_members(
        members,
        minimum_age_recipients=minimum_age_recipients,
    )

    return VerifiedBundle(
        digest=sha256_bytes(bundle_content),
        metadata=metadata,
        files=files,
        manifest=manifest,
        compose=compose,
        secrets=secrets,
    )
