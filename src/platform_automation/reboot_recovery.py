import os
import stat

from pathlib import Path
from typing import Any, Optional

from .operation_lock import project_environment_lock

from .release_ledger import (
    ReleaseLedgerError,
    find_latest_deployed_release,
    list_release_records,
    load_release_bundle,
    resolve_release_bundle,
    validate_ledger_identity,
)
from .runtime_secrets import materialize_env_secrets


class RebootRecoveryError(ValueError):
    pass


def validate_recovery_directory(
    path: Path,
    expected_mode: int,
) -> None:
    try:
        information = path.lstat()
    except OSError as error:
        raise RebootRecoveryError(
            f"cannot inspect recovery directory: {path}"
        ) from error

    if stat.S_ISLNK(information.st_mode):
        raise RebootRecoveryError(
            f"recovery directory cannot be a symbolic link: {path}"
        )

    if not stat.S_ISDIR(information.st_mode):
        raise RebootRecoveryError(f"recovery path is not a directory: {path}")

    if information.st_uid != os.geteuid() or information.st_gid != os.getegid():
        raise RebootRecoveryError(f"recovery directory has unsafe ownership: {path}")

    if stat.S_IMODE(information.st_mode) != expected_mode:
        raise RebootRecoveryError(f"recovery directory has unsafe permissions: {path}")


def discover_recovery_scopes(
    projects_root: Path,
) -> list[tuple[str, str]]:
    validate_recovery_directory(
        projects_root,
        0o750,
    )

    scopes: list[tuple[str, str]] = []

    try:
        project_paths = sorted(
            projects_root.iterdir(),
            key=lambda item: item.name,
        )
    except OSError as error:
        raise RebootRecoveryError(
            f"cannot list projects directory: {projects_root}"
        ) from error

    for project_path in project_paths:
        project = project_path.name

        try:
            validate_ledger_identity(project, "lab")
        except ReleaseLedgerError as error:
            raise RebootRecoveryError(
                f"invalid recovery project directory: {project}"
            ) from error

        validate_recovery_directory(
            project_path,
            0o700,
        )

        try:
            environment_paths = sorted(
                project_path.iterdir(),
                key=lambda item: item.name,
            )
        except OSError as error:
            raise RebootRecoveryError(
                f"cannot list recovery project directory: {project_path}"
            ) from error

        for environment_path in environment_paths:
            environment = environment_path.name

            try:
                validate_ledger_identity(
                    project,
                    environment,
                )
            except ReleaseLedgerError as error:
                raise RebootRecoveryError(
                    f"invalid recovery environment directory: "
                    f"{project}/{environment}"
                ) from error

            validate_recovery_directory(
                environment_path,
                0o700,
            )
            validate_recovery_directory(
                environment_path / "ledger",
                0o700,
            )

            scopes.append(
                (
                    project,
                    environment,
                )
            )

    return scopes


def select_recovery_release(
    records: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Accept validated ledger records for one project/environment."""
    if any(record["status"] == "deploying" for record in records):
        raise RebootRecoveryError("unfinished deployment requires operator review")

    current = find_latest_deployed_release(records)

    if current is None:
        if any(record["status"] in {"failed", "rolled_back"} for record in records):
            raise RebootRecoveryError("no successful release; operator review required")
        return None

    if current["healthcheck"]["status"] != "succeeded":
        raise RebootRecoveryError("selected release has no successful healthcheck")

    matching_time = [
        record
        for record in records
        if record["status"] == "deployed"
        and record["updated_at"] == current["updated_at"]
    ]
    if len(matching_time) != 1:
        raise RebootRecoveryError("ambiguous release order requires operator review")

    for record in records:
        if record["updated_at"] < current["updated_at"]:
            continue

        if record["status"] == "failed":
            raise RebootRecoveryError("unresolved failure requires operator review")

        if (
            record["status"] == "rolled_back"
            and record["previous_release_id"] != current["release_id"]
        ):
            raise RebootRecoveryError("rollback target does not match selected release")

    return current


def restore_release_secrets(
    record: dict[str, Any],
    releases_root: Path,
    runtime_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    minimum_age_recipients: int = 1,
) -> Path:
    bundle = load_release_bundle(
        record,
        releases_root,
        minimum_age_recipients=minimum_age_recipients,
    )

    if record["status"] != "deployed" or record["healthcheck"]["status"] != "succeeded":
        raise RebootRecoveryError(
            "secret recovery requires a successfully deployed release"
        )

    if bundle.manifest["secrets"]["delivery"] != "env":
        raise RebootRecoveryError(
            "secret recovery currently supports env delivery only"
        )

    staged_path = resolve_release_bundle(record, releases_root)
    secrets_relative_path = bundle.metadata["files"]["secrets"]["path"]

    return materialize_env_secrets(
        encrypted_file=staged_path / secrets_relative_path,
        project=record["project"],
        environment=record["environment"],
        release_id=record["release_id"],
        runtime_root=runtime_root,
        age_key_file=age_key_file,
        sops_executable=sops_executable,
    )


def recover_project_environment_secrets(
    project: str,
    environment: str,
    projects_root: Path,
    releases_root: Path,
    lock_root: Path,
    runtime_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    minimum_age_recipients: int = 1,
) -> Optional[Path]:
    with project_environment_lock(
        lock_root,
        project,
        environment,
        "recovery",
    ):
        records = list_release_records(
            projects_root,
            project,
            environment,
        )
        record = select_recovery_release(records)

        if record is None:
            return None

        return restore_release_secrets(
            record=record,
            releases_root=releases_root,
            runtime_root=runtime_root,
            age_key_file=age_key_file,
            sops_executable=sops_executable,
            minimum_age_recipients=minimum_age_recipients,
        )
