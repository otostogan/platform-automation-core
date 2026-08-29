#!/usr/bin/env python3

import json
import os
import tempfile
import re
import uuid
import stat
import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contract_resources import contract_path
from .deployment_request import DeploymentRequest
from .validate_manifest import load_json, validate_manifest

from .verify_bundle import (
    EXPECTED_MEMBER_COUNT,
    MAX_MEMBER_BYTES,
    MAX_TOTAL_MEMBER_BYTES,
    BundleVerificationError,
    VerifiedBundle,
    validate_bundle_members,
)


DEFAULT_RELEASE_SCHEMA = contract_path("release-v1.schema.json")

PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
ALLOWED_ENVIRONMENTS = {"lab", "staging", "production"}
MAX_RELEASE_RECORD_BYTES = 64 * 1024


class ReleaseLedgerError(ValueError):
    pass


def utc_timestamp() -> str:
    """Stamp records with millisecond precision.

    Release order is derived from these timestamps, and two deployments can
    legitimately land inside the same second.
    """
    moment = datetime.now(timezone.utc)

    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def validate_release_record(record: Any) -> None:
    schema = load_json(DEFAULT_RELEASE_SCHEMA)
    errors = validate_manifest(record, schema)

    if errors:
        raise ReleaseLedgerError("invalid release record:\n" + "\n".join(errors))


def load_release_bundle(
    record: dict[str, Any],
    releases_root: Path,
    minimum_age_recipients: int = 1,
) -> VerifiedBundle:
    root = resolve_release_bundle(record, releases_root)
    members: dict[str, bytes] = {}
    total_bytes = 0

    try:
        root_info = root.stat()
        if root_info.st_uid != os.geteuid() or stat.S_IMODE(root_info.st_mode) != 0o700:
            raise ReleaseLedgerError("saved bundle has unsafe ownership or permissions")

        for index, path in enumerate(root.rglob("*"), start=1):
            if index > 512:
                raise ReleaseLedgerError(
                    "saved bundle contains too many filesystem entries"
                )

            info = path.lstat()

            if stat.S_ISLNK(info.st_mode):
                raise ReleaseLedgerError("saved bundle contains a symbolic link")

            if info.st_uid != os.geteuid():
                raise ReleaseLedgerError("saved bundle entry has unsafe ownership")

            if stat.S_ISDIR(info.st_mode):
                if stat.S_IMODE(info.st_mode) != 0o700:
                    raise ReleaseLedgerError(
                        "saved bundle directory has unsafe permissions"
                    )
                continue

            if not stat.S_ISREG(info.st_mode):
                raise ReleaseLedgerError("saved bundle contains a non-regular file")

            if stat.S_IMODE(info.st_mode) != 0o600:
                raise ReleaseLedgerError("saved bundle file has unsafe permissions")

            if len(members) >= EXPECTED_MEMBER_COUNT:
                raise ReleaseLedgerError("saved bundle contains too many files")

            if info.st_size > MAX_MEMBER_BYTES:
                raise ReleaseLedgerError("saved bundle file exceeds size limit")

            with path.open("rb") as file:
                content = file.read(MAX_MEMBER_BYTES + 1)

            if len(content) > MAX_MEMBER_BYTES:
                raise ReleaseLedgerError("saved bundle file exceeds size limit")

            total_bytes += len(content)
            if total_bytes > MAX_TOTAL_MEMBER_BYTES:
                raise ReleaseLedgerError("saved bundle exceeds total size limit")

            members[path.relative_to(root).as_posix()] = content

        metadata, files, manifest, compose, secrets = validate_bundle_members(
            members,
            minimum_age_recipients=minimum_age_recipients,
        )

        for field in ("project", "environment"):
            if metadata[field] != record[field]:
                raise ReleaseLedgerError(
                    "saved bundle identity does not match release record"
                )

    except (OSError, BundleVerificationError) as error:
        raise ReleaseLedgerError(
            f"cannot load saved release bundle: {error}"
        ) from error

    return VerifiedBundle(
        # Original archive digest comes from the trusted release ledger.
        digest=record["bundle"]["digest"],
        metadata=metadata,
        files=files,
        manifest=manifest,
        compose=compose,
        secrets=secrets,
    )


def build_prepared_release(
    request: DeploymentRequest,
    staged_bundle_path: Path,
    releases_root: Path,
    previous_release_id: str = None,
    release_id: str = None,
    timestamp: str = None,
) -> dict[str, Any]:
    if staged_bundle_path.is_symlink():
        raise ReleaseLedgerError("staged bundle path cannot be a symbolic link")

    if not staged_bundle_path.is_dir():
        raise ReleaseLedgerError(f"staged bundle does not exist: {staged_bundle_path}")

    resolved_releases_root = releases_root.resolve()
    resolved_bundle_path = staged_bundle_path.resolve()

    try:
        relative_bundle_path = resolved_bundle_path.relative_to(
            resolved_releases_root
        ).as_posix()
    except ValueError as error:
        raise ReleaseLedgerError("staged bundle escapes releases root") from error

    expected_bundle_path = (
        f"{request.project}/"
        f"{request.environment}/"
        f"bundles/{request.bundle.digest}"
    )

    if relative_bundle_path != expected_bundle_path:
        raise ReleaseLedgerError("staged bundle path does not match deployment request")

    if release_id is None:
        release_id = uuid.uuid4().hex

    if timestamp is None:
        timestamp = utc_timestamp()

    migration_required = bool(
        request.bundle.manifest["deployment"].get("migration_command")
    )

    record = {
        "api_version": "platform-release/v1",
        "release_id": release_id,
        "project": request.project,
        "environment": request.environment,
        "release_tag": request.release_tag,
        "image": {
            "reference": request.image,
            "repository": request.image_repository,
            "digest": request.image_digest,
        },
        "bundle": {
            "digest": request.bundle.digest,
            "relative_path": relative_bundle_path,
        },
        "status": "prepared",
        "created_at": timestamp,
        "updated_at": timestamp,
        "previous_release_id": previous_release_id,
        "rollback_of_release_id": None,
        "migration": {
            "status": ("pending" if migration_required else "not_required"),
            "completed_at": None,
            "error": None,
        },
        "healthcheck": {
            "status": "pending",
            "completed_at": None,
            "error": None,
        },
    }

    validate_release_record(record)

    return record


def build_rollback_release(
    target: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    validate_release_record(target)
    validate_release_record(current)

    for field in ("project", "environment"):
        if target[field] != current[field]:
            raise ReleaseLedgerError(
                "rollback releases must belong to the same project/environment"
            )

    for record in (target, current):
        if (
            record["status"] != "deployed"
            or record["healthcheck"]["status"] != "succeeded"
        ):
            raise ReleaseLedgerError(
                "rollback requires successful target and current releases"
            )

    release_id = uuid.uuid4().hex
    timestamp = utc_timestamp()

    record = copy.deepcopy(target)
    record.update(
        {
            "release_id": release_id,
            "release_tag": f"rollback-{release_id}",
            "status": "prepared",
            "created_at": timestamp,
            "updated_at": timestamp,
            "previous_release_id": current["release_id"],
            "rollback_of_release_id": target["release_id"],
            "migration": {
                "status": "not_required",
                "completed_at": None,
                "error": None,
            },
            "healthcheck": {
                "status": "pending",
                "completed_at": None,
                "error": None,
            },
        }
    )

    validate_release_record(record)
    return record


def resolve_release_bundle(
    record: dict[str, Any],
    releases_root: Path,
) -> Path:
    validate_release_record(record)

    expected_relative_path = (
        f"{record['project']}/"
        f"{record['environment']}/"
        f"bundles/{record['bundle']['digest']}"
    )

    if record["bundle"]["relative_path"] != expected_relative_path:
        raise ReleaseLedgerError("bundle path does not match release identity")

    if releases_root.is_symlink():
        raise ReleaseLedgerError("releases root cannot be a symbolic link")

    if not releases_root.is_dir():
        raise ReleaseLedgerError("releases root does not exist")

    current = releases_root.resolve()

    for part in expected_relative_path.split("/"):
        current = current / part

        if current.is_symlink():
            raise ReleaseLedgerError(
                "release bundle path cannot contain a symbolic link"
            )

        if not current.is_dir():
            raise ReleaseLedgerError("release bundle directory does not exist")

    return current


def create_private_ledger_directory(
    projects_root: Path,
    project: str,
    environment: str,
) -> Path:
    if projects_root.is_symlink():
        raise ReleaseLedgerError(
            f"projects root cannot be a symbolic link: {projects_root}"
        )

    projects_root.mkdir(
        mode=0o750,
        parents=True,
        exist_ok=True,
    )
    projects_root = projects_root.resolve()

    current = projects_root

    for part in (project, environment, "ledger"):
        current = current / part

        if current.is_symlink():
            raise ReleaseLedgerError(
                f"ledger path cannot contain a symbolic link: {current}"
            )

        current.mkdir(
            mode=0o700,
            exist_ok=True,
        )
        current.chmod(0o700)

    return current


def write_release_record(
    projects_root: Path,
    record: dict[str, Any],
) -> Path:
    validate_release_record(record)

    ledger_directory = create_private_ledger_directory(
        projects_root,
        record["project"],
        record["environment"],
    )
    destination = ledger_directory / f"{record['release_id']}.json"

    if destination.exists() or destination.is_symlink():
        raise ReleaseLedgerError(
            f"release record already exists: " f"{record['release_id']}"
        )

    content = (
        json.dumps(
            record,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )

    temporary_file = tempfile.NamedTemporaryFile(
        prefix=f".{record['release_id']}.",
        suffix=".tmp",
        dir=ledger_directory,
        delete=False,
    )
    temporary_path = Path(temporary_file.name)

    try:
        temporary_file.write(content)
        temporary_file.flush()
        os.fsync(temporary_file.fileno())
        temporary_file.close()

        temporary_path.chmod(0o600)
        os.rename(
            temporary_path,
            destination,
        )
    except Exception:
        temporary_file.close()

        if temporary_path.exists():
            temporary_path.unlink()

        raise

    return destination


def replace_release_record(
    projects_root: Path,
    record: dict[str, Any],
) -> Path:
    validate_release_record(record)

    ledger_directory = resolve_existing_ledger_directory(
        projects_root,
        record["project"],
        record["environment"],
    )

    if ledger_directory is None:
        raise ReleaseLedgerError("release ledger does not exist")

    destination = ledger_directory / f"{record['release_id']}.json"

    if destination.is_symlink() or not destination.is_file():
        raise ReleaseLedgerError(
            f"release record does not exist: {record['release_id']}"
        )

    existing = load_release_record(destination)

    for field in (
        "api_version",
        "release_id",
        "project",
        "environment",
        "release_tag",
        "created_at",
    ):
        if record[field] != existing[field]:
            raise ReleaseLedgerError(f"release record identity changed: {field}")

    content = (
        json.dumps(
            record,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    temporary_file = tempfile.NamedTemporaryFile(
        prefix=f".{record['release_id']}.",
        suffix=".tmp",
        dir=ledger_directory,
        delete=False,
    )
    temporary_path = Path(temporary_file.name)

    try:
        temporary_file.write(content)
        temporary_file.flush()
        os.fsync(temporary_file.fileno())
        temporary_file.close()
        temporary_path.chmod(0o600)
        os.replace(temporary_path, destination)
    except Exception:
        temporary_file.close()

        if temporary_path.exists():
            temporary_path.unlink()

        raise

    return destination


def validate_ledger_identity(
    project: str,
    environment: str,
) -> None:
    if not PROJECT_PATTERN.fullmatch(project):
        raise ReleaseLedgerError(f"invalid project for release ledger: {project}")

    if environment not in ALLOWED_ENVIRONMENTS:
        raise ReleaseLedgerError(
            f"invalid environment for release ledger: {environment}"
        )


def resolve_existing_ledger_directory(
    projects_root: Path,
    project: str,
    environment: str,
) -> Path:
    validate_ledger_identity(project, environment)

    if projects_root.is_symlink():
        raise ReleaseLedgerError(
            f"projects root cannot be a symbolic link: {projects_root}"
        )

    if not projects_root.exists():
        return None

    current = projects_root.resolve()

    for part in (project, environment, "ledger"):
        current = current / part

        if current.is_symlink():
            raise ReleaseLedgerError(
                f"ledger path cannot contain a symbolic link: {current}"
            )

        if not current.exists():
            return None

        if not current.is_dir():
            raise ReleaseLedgerError(f"ledger path is not a directory: {current}")

    return current


def load_release_record(record_path: Path) -> dict[str, Any]:
    if record_path.is_symlink():
        raise ReleaseLedgerError(
            f"release record cannot be a symbolic link: {record_path}"
        )

    if not record_path.is_file():
        raise ReleaseLedgerError(f"release record does not exist: {record_path}")

    if stat.S_IMODE(record_path.stat().st_mode) != 0o600:
        raise ReleaseLedgerError(
            f"release record has unsafe permissions: {record_path}"
        )

    if record_path.stat().st_size > MAX_RELEASE_RECORD_BYTES:
        raise ReleaseLedgerError(f"release record exceeds size limit: {record_path}")

    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseLedgerError(
            f"release record is not valid JSON: {record_path}"
        ) from error

    validate_release_record(record)

    expected_filename = f"{record['release_id']}.json"

    if record_path.name != expected_filename:
        raise ReleaseLedgerError(
            f"release record filename does not match release ID: " f"{record_path}"
        )

    return record


def list_release_records(
    projects_root: Path,
    project: str,
    environment: str,
) -> list[dict[str, Any]]:
    ledger_directory = resolve_existing_ledger_directory(
        projects_root,
        project,
        environment,
    )

    if ledger_directory is None:
        return []

    records = [
        load_release_record(record_path)
        for record_path in ledger_directory.glob("*.json")
    ]

    return sorted(
        records,
        key=lambda record: (
            record["created_at"],
            record["release_id"],
        ),
    )


def find_latest_deployed_release(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    deployed_records = [record for record in records if record["status"] == "deployed"]

    if not deployed_records:
        return None

    return max(
        deployed_records,
        key=lambda record: (
            record["updated_at"],
            record["created_at"],
            record["release_id"],
        ),
    )


def create_prepared_release(
    request: DeploymentRequest,
    staged_bundle_path: Path,
    releases_root: Path,
    projects_root: Path,
    previous_release_id: str = None,
) -> tuple[dict[str, Any], Path]:
    record = build_prepared_release(
        request=request,
        staged_bundle_path=staged_bundle_path,
        releases_root=releases_root,
        previous_release_id=previous_release_id,
    )
    record_path = write_release_record(
        projects_root,
        record,
    )

    return record, record_path
