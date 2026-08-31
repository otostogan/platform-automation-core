#!/usr/bin/env python3

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

from . import compose_runtime
from .compose_runtime import ComposeRuntimeError
from .operation_lock import (
    OperationLockError,
    project_environment_lock,
)
from .nginx_transaction import NginxTransactionError, NginxTransactionManager
from .registry_pull import (
    RegistryPullError,
    pull_immutable_image,
    read_registry_token,
)
from .release_ledger import (
    ReleaseLedgerError,
    build_prepared_release,
    find_latest_deployed_release,
    list_release_records,
    replace_release_record,
    utc_timestamp,
    write_release_record,
    load_release_bundle,
    build_rollback_release,
    resolve_release_bundle,
)
from .release_retention import (
    ReleaseRetentionError,
    apply_retention,
    remove_image,
    resolve_retained_images,
)
from .runtime_secrets import (
    RuntimeSecretsError,
    materialize_env_secrets,
)
from .stage_bundle import (
    BundleStagingError,
    stage_verified_bundle,
)

from .backup_runtime import (
    DEFAULT_AGE_EXECUTABLE,
    DEFAULT_BACKUPS_ROOT,
    BackupRuntimeError,
    create_backup,
    list_backups,
)

from .backup_offsite import (
    DEFAULT_CREDENTIALS_FILE,
    DEFAULT_OFFSITE_CONFIG,
    OffsiteError,
    download_backup,
    offsite_status,
    read_operator_credentials,
    upload_backups,
)

from .backup_schedule import (
    DEFAULT_SYSTEMCTL_EXECUTABLE,
    DEFAULT_SYSTEMD_ROOT,
    BackupScheduleError,
    reconcile_backup_timer,
    split_instance,
)

from .restore_runtime import (
    RestoreRuntimeError,
    last_verification,
    restore_backup,
    verify_backup,
)

from .database_runtime import (
    DatabaseRuntimeError,
    ensure_project_database,
    inject_database_url,
)

from .deployment_request import (
    DeploymentRequest,
    DeploymentRequestError,
    load_deployment_request,
    parse_immutable_image,
)

DEFAULT_PROJECTS_ROOT = Path("/var/lib/platform/projects")
DEFAULT_DATABASES_ROOT = Path("/var/lib/platform/databases")
DEFAULT_RELEASES_ROOT = Path("/var/lib/platform/releases")
DEFAULT_LOCK_ROOT = Path("/run/platform/locks")
DEFAULT_RUNTIME_SECRETS_ROOT = Path("/run/platform/secrets")
DEFAULT_AGE_KEY_FILE = Path("/etc/platform/keys/age.key")
DEFAULT_SOPS_EXECUTABLE = Path("/usr/local/bin/sops")
DEFAULT_REGISTRY_RUNTIME_ROOT = Path("/run/platform/registry")
DEFAULT_DOCKER_EXECUTABLE = Path("/usr/bin/docker")
DEFAULT_NGINX_VHOST_ROOT = Path("/var/lib/platform/proxy/vhost.d")
DEFAULT_NGINX_OWNERSHIP_ROOT = Path("/var/lib/platform/proxy/managed-vhosts")
DEFAULT_NGINX_CONFIG = Path("/var/lib/platform/proxy/conf.d/default.conf")
DEFAULT_NGINX_RAW_ALLOWLIST = Path("/etc/platform/nginx-raw-projects.json")
DEFAULT_NGINX_CONTAINER = "platform-nginx"


class DeploymentExecutionError(RuntimeError):
    pass


def positive_integer(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"expected a positive integer, got {value!r}"
        ) from error

    if parsed < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")

    return parsed


class RaiseOnlyAction(argparse.Action):
    """Let repeated occurrences raise a policy floor but never lower it.

    The host wrapper prepends the configured policy before caller arguments,
    so a later caller-supplied value must not be able to weaken it.
    """

    def __call__(self, parser, namespace, values, option_string=None):
        current = getattr(namespace, self.dest, None)

        if current is None or values > current:
            setattr(namespace, self.dest, values)


def load_saved_release_request(
    record: dict[str, Any],
    releases_root: Path,
    minimum_age_recipients: int = 1,
) -> DeploymentRequest:
    bundle = load_release_bundle(
        record,
        releases_root,
        minimum_age_recipients=minimum_age_recipients,
    )

    repository, digest = parse_immutable_image(
        record["image"]["reference"],
        bundle.manifest["image"]["repository"],
    )

    if (
        repository != record["image"]["repository"]
        or digest != record["image"]["digest"]
    ):
        raise DeploymentRequestError("saved image fields do not match image reference")

    return DeploymentRequest(
        project=record["project"],
        environment=record["environment"],
        release_tag=record["release_tag"],
        image=record["image"]["reference"],
        image_repository=repository,
        image_digest=digest,
        bundle=bundle,
    )


def add_identity_arguments(
    parser: argparse.ArgumentParser,
) -> None:
    parser.add_argument(
        "--project",
        required=True,
        help="Application project name.",
    )
    parser.add_argument(
        "--environment",
        required=True,
        choices=("lab", "staging", "production"),
        help="Deployment environment.",
    )


def parse_arguments(
    argv: list[str] = None,
) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="platform",
        description="Reusable VPS platform CLI.",
    )
    parser.add_argument(
        "--minimum-age-recipients",
        type=positive_integer,
        action=RaiseOnlyAction,
        default=1,
        help=(
            "Host policy for unique SOPS age recipients. "
            "Configured by the platform automation role. "
            "Repeating the option can only raise the policy, never lower it."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    deploy_parser = subparsers.add_parser(
        "deploy",
        help="Deploy an immutable application release.",
    )
    add_identity_arguments(deploy_parser)
    deploy_parser.add_argument(
        "--bundle",
        required=True,
        type=Path,
        help="Verified deployment bundle archive.",
    )
    deploy_parser.add_argument(
        "--image",
        required=True,
        help="Immutable image reference with sha256 digest.",
    )
    deploy_parser.add_argument(
        "--release-tag",
        required=True,
        help="Human-readable release tag.",
    )
    deploy_parser.add_argument(
        "--registry-username",
        help="Temporary registry username.",
    )
    deploy_parser.add_argument(
        "--registry-token-stdin",
        action="store_true",
        help="Read a temporary registry token from stdin.",
    )
    deploy_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Show application deployment status.",
    )
    add_identity_arguments(status_parser)
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )

    backup_parser = subparsers.add_parser(
        "backup",
        help="Write an encrypted dump of a platform-owned database.",
    )
    # The scheduled unit knows one systemd instance name, not two arguments.
    # Splitting it here rather than in the unit keeps the parsing in tested
    # code and out of a shell that systemd expands before the shell sees it.
    backup_identity = backup_parser.add_mutually_exclusive_group(required=True)
    backup_identity.add_argument(
        "--instance",
        help="systemd instance name, as {project}-{environment}.",
    )
    backup_identity.add_argument(
        "--project",
        help="Application project name.",
    )
    backup_parser.add_argument(
        "--environment",
        choices=("lab", "staging", "production"),
        help="Deployment environment. Required with --project.",
    )
    backup_parser.add_argument(
        "--reason",
        choices=("operator", "schedule", "pre-migration"),
        default="operator",
        help="Why this backup ran. Recorded in the metadata card.",
    )
    backup_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )

    restore_parser = subparsers.add_parser(
        "restore",
        help="Restore a backup over the live database. Destructive.",
    )
    add_identity_arguments(restore_parser)
    restore_parser.add_argument(
        "--from",
        dest="stamp",
        help="Backup stamp to restore. Defaults to the newest.",
    )
    restore_parser.add_argument(
        "--from-offsite",
        action="store_true",
        help=(
            "Fetch the backup from object storage first. Reader credentials "
            "are read from stdin; the host holds none."
        ),
    )
    restore_parser.add_argument(
        "--confirm-destructive",
        action="store_true",
        help="Required. Restoring replaces the contents of the live database.",
    )
    restore_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )

    verify_parser = subparsers.add_parser(
        "verify-backup",
        help="Restore a backup into a throwaway container and query it.",
    )
    add_identity_arguments(verify_parser)
    verify_parser.add_argument(
        "--from",
        dest="stamp",
        help="Backup stamp to verify. Defaults to the newest.",
    )
    verify_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )

    rollback_parser = subparsers.add_parser(
        "rollback",
        help="Restore a previously successful release without migrations.",
    )
    add_identity_arguments(rollback_parser)
    rollback_parser.add_argument(
        "--to",
        dest="target_tag",
        required=True,
        help="Previously successful release tag to restore.",
    )
    rollback_parser.add_argument(
        "--registry-username",
        help="Temporary registry username.",
    )
    rollback_parser.add_argument(
        "--registry-token-stdin",
        action="store_true",
        help="Read a temporary registry token from stdin.",
    )
    rollback_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )

    return parser.parse_args(argv)


def build_status_document(
    project: str,
    environment: str,
    records: list[dict[str, Any]],
    backups: dict[str, Any] = None,
) -> dict[str, Any]:
    current = find_latest_deployed_release(records)
    latest = records[-1] if records else None

    return {
        "project": project,
        "environment": environment,
        "release_count": len(records),
        "current": current,
        "latest": latest,
        "backups": backups,
    }


def build_backup_status(
    backups_root: Path,
    project: str,
    environment: str,
    offsite: dict[str, Any] = None,
) -> dict[str, Any]:
    """Answer when a restore was last proven, not when a dump was last taken.

    The two are different questions, and only the first one matters at three
    in the morning.
    """
    directory = backups_root / project / environment

    try:
        stamps = list_backups(directory)
        verification = last_verification(directory)
    except OSError:
        return {"count": 0, "latest": None, "last_verified": None}

    return {
        "count": len(stamps),
        "latest": stamps[-1] if stamps else None,
        "last_verified": verification,
        "offsite": offsite,
    }


def print_release_summary(
    label: str,
    record: dict[str, Any],
) -> None:
    if record is None:
        print(f"{label}: none")
        return

    print(f"{label}:")
    print(f"  release_id: {record['release_id']}")
    print(f"  release_tag: {record['release_tag']}")
    print(f"  status: {record['status']}")
    print(f"  image: {record['image']['reference']}")
    print(f"  bundle: {record['bundle']['digest']}")
    print(f"  migration: {record['migration']['status']}")
    print(f"  healthcheck: {record['healthcheck']['status']}")
    print(f"  updated_at: {record['updated_at']}")


def print_backup_status(backups: dict[str, Any]) -> None:
    if backups is None:
        return

    print(f"Backups: {backups['count']}")
    print(f"  latest: {backups['latest'] or 'none'}")

    print_verification_status(backups["last_verified"])
    print_offsite_status(backups.get("offsite"))


def print_verification_status(verification: dict[str, Any]) -> None:
    if verification is None:
        # A backup nobody has restored is not yet a backup.
        print("  last proven restorable: never")
        return

    print(
        f"  last proven restorable: {verification.get('outcome')} "
        f"on {verification.get('stamp')} "
        f"at {verification.get('completed_at')}"
    )

    if verification.get("error"):
        print(f"  verification error: {verification['error']}")


def print_offsite_status(offsite: dict[str, Any]) -> None:
    if offsite is None:
        return

    state = offsite["state"]

    if state == "not-configured":
        print("  offsite: not configured; backups stay on this host")
    elif state == "error":
        print(f"  offsite: UNKNOWN — {offsite['error']}")
    elif state == "behind":
        print(
            f"  offsite: BEHIND — {len(offsite['not_uploaded'])} backup(s) "
            f"not in {offsite['bucket']}"
        )
    else:
        print(
            f"  offsite: current, {offsite['remote_objects']} object(s) in "
            f"{offsite['bucket']}"
        )


def print_human_status(document: dict[str, Any]) -> None:
    print(f"Platform status: " f"{document['project']}/{document['environment']}")
    print(f"Release records: {document['release_count']}")

    print_release_summary(
        "Current deployed release",
        document["current"],
    )
    print_release_summary(
        "Latest release attempt",
        document["latest"],
    )
    print_backup_status(document["backups"])


def run_status(
    arguments: argparse.Namespace,
    projects_root: Path,
    backups_root: Path = DEFAULT_BACKUPS_ROOT,
    offsite_config: Path = DEFAULT_OFFSITE_CONFIG,
    offsite_credentials: Path = DEFAULT_CREDENTIALS_FILE,
    offsite_reporter=offsite_status,
) -> int:
    try:
        records = list_release_records(
            projects_root,
            arguments.project,
            arguments.environment,
        )
    except ReleaseLedgerError as error:
        print(f"status error: {error}", file=sys.stderr)
        return 1

    document = build_status_document(
        arguments.project,
        arguments.environment,
        records,
        build_backup_status(
            backups_root,
            arguments.project,
            arguments.environment,
            offsite_reporter(
                project=arguments.project,
                environment=arguments.environment,
                directory=backups_root / arguments.project / arguments.environment,
                config_path=offsite_config,
                credentials_path=offsite_credentials,
            ),
        ),
    )

    if arguments.json:
        print(
            json.dumps(
                document,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print_human_status(document)

    return 0


def find_release_by_tag(
    records: list[dict[str, Any]],
    release_tag: str,
) -> dict[str, Any]:
    for record in reversed(records):
        if record["release_tag"] == release_tag:
            return record

    return None


def select_rollback_release(
    records: list[dict[str, Any]],
    release_tag: str,
) -> dict[str, Any]:
    matching = [record for record in records if record["release_tag"] == release_tag]

    if not matching:
        raise DeploymentExecutionError(f"rollback target tag not found: {release_tag}")

    for record in reversed(matching):
        if (
            record["status"] == "deployed"
            and record["healthcheck"]["status"] == "succeeded"
        ):
            return record

    raise DeploymentExecutionError(
        f"rollback target has no successful deployment: {release_tag}"
    )


def find_release_by_id(
    records: list[dict[str, Any]],
    release_id: str,
) -> dict[str, Any]:
    for record in records:
        if record["release_id"] == release_id:
            return record

    return None


def release_matches_request(
    record: dict[str, Any],
    image: str,
    bundle_digest: str,
) -> bool:
    return (
        record["image"]["reference"] == image
        and record["bundle"]["digest"] == bundle_digest
    )


def materialize_release_secrets(
    request,
    staged_bundle_path: Path,
    release_id: str,
    runtime_secrets_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    secrets_materializer,
) -> Path:
    delivery = request.bundle.manifest["secrets"]["delivery"]

    if delivery != "env":
        raise RuntimeSecretsError(f"unsupported secrets delivery mode: {delivery}")

    relative_path = request.bundle.metadata["files"]["secrets"]["path"]
    encrypted_file = staged_bundle_path.joinpath(*relative_path.split("/"))

    return secrets_materializer(
        encrypted_file=encrypted_file,
        project=request.project,
        environment=request.environment,
        release_id=release_id,
        runtime_root=runtime_secrets_root,
        age_key_file=age_key_file,
        sops_executable=sops_executable,
    )


def load_registry_credentials(
    arguments: argparse.Namespace,
    token_stream,
) -> tuple[str, bytes]:
    has_username = arguments.registry_username is not None
    reads_token = arguments.registry_token_stdin

    if has_username != reads_token:
        raise RegistryPullError(
            "--registry-username and --registry-token-stdin " "must be used together"
        )

    if not reads_token:
        return None, None

    return arguments.registry_username, read_registry_token(token_stream)


def build_deploy_result(
    record: dict[str, Any],
    staged_bundle_path: Path,
    runtime_secrets_path: Path,
    reused: bool,
    containers_started: bool,
    retention: dict[str, Any] = None,
    schedule: dict[str, Any] = None,
) -> dict[str, Any]:
    """Report what this invocation did, never what the ledger already said."""
    return {
        "project": record["project"],
        "environment": record["environment"],
        "release_id": record["release_id"],
        "release_tag": record["release_tag"],
        "status": record["status"],
        "image": record["image"]["reference"],
        "bundle_digest": record["bundle"]["digest"],
        "staged_bundle_path": str(staged_bundle_path),
        "runtime_secrets_path": str(runtime_secrets_path),
        "image_pulled": True,
        "reused": reused,
        "containers_started": containers_started,
        "retention": retention,
        "schedule": schedule,
    }


def print_deploy_result(document: dict[str, Any]) -> None:
    action = "Reused" if document["reused"] else "Completed"

    print(f"{action} deployment: " f"{document['project']}/{document['environment']}")
    print(f"Release ID: {document['release_id']}")
    print(f"Release tag: {document['release_tag']}")
    print(f"Status: {document['status']}")
    print(f"Image: {document['image']}")
    print(f"Bundle: {document['bundle_digest']}")
    print(f"Staged at: {document['staged_bundle_path']}")
    print(f"Runtime secrets: {document['runtime_secrets_path']}")
    print("Image pulled: yes")
    print("Containers started: " + ("yes" if document["containers_started"] else "no"))
    print_retention_summary(document["retention"])
    print_schedule_summary(document["schedule"])


def print_schedule_summary(schedule: dict[str, Any]) -> None:
    if schedule is None:
        return

    if schedule["state"] == "enabled":
        print(
            f"Backup schedule: every {schedule['interval_minutes']} minute(s) "
            f"via {schedule['unit']}"
        )
    else:
        print(f"Backup schedule: {schedule['state']}")

    if schedule.get("warning"):
        print(f"Schedule warning: {schedule['warning']}")


def print_retention_summary(retention: dict[str, Any]) -> None:
    if retention is None:
        print("Retention: not run")
        return

    print(f"Retention depth: {retention['retained_images']} image(s)")
    print(
        "Reclaimed: "
        f"{len(retention['removed_images'])} image(s), "
        f"{len(retention['removed_bundles'])} bundle(s), "
        f"{len(retention['removed_runtime_secrets'])} secret set(s)"
    )

    for warning in retention["warnings"]:
        print(f"Retention warning: {warning}")


def update_release_state(
    projects_root: Path,
    record: dict[str, Any],
    status: str = None,
    migration_status: str = None,
    migration_error: str = None,
    healthcheck_status: str = None,
    healthcheck_error: str = None,
) -> dict[str, Any]:
    updated = copy.deepcopy(record)
    timestamp = utc_timestamp()

    if status is not None:
        updated["status"] = status

    for result_name, result_status, result_error in (
        ("migration", migration_status, migration_error),
        ("healthcheck", healthcheck_status, healthcheck_error),
    ):
        if result_status is None:
            continue

        updated[result_name]["status"] = result_status
        updated[result_name]["error"] = (
            str(result_error)[:2048] if result_error is not None else None
        )
        updated[result_name]["completed_at"] = (
            timestamp if result_status in ("succeeded", "failed") else None
        )

    updated["updated_at"] = timestamp
    replace_release_record(projects_root, updated)

    return updated


def runtime_arguments(
    request,
    staged_bundle_path: Path,
    runtime_secrets_path: Path,
    docker_executable: Path,
) -> dict[str, Any]:
    return {
        "manifest": request.bundle.manifest,
        "staged_bundle_path": staged_bundle_path,
        "image": request.image,
        "runtime_secrets_path": runtime_secrets_path,
        "docker_executable": docker_executable,
    }


def restore_previous_release(
    previous: dict[str, Any],
    releases_root: Path,
    runtime_secrets_root: Path,
    docker_executable: Path,
    compose_runtime_module,
) -> None:
    staged_bundle_path = releases_root / previous["bundle"]["relative_path"]
    manifest = compose_runtime_module.load_staged_manifest(staged_bundle_path)
    runtime_secrets_path = (
        runtime_secrets_root
        / previous["project"]
        / previous["environment"]
        / previous["release_id"]
        / "app.env"
    )
    compose_runtime_module.start_release(
        manifest=manifest,
        staged_bundle_path=staged_bundle_path,
        image=previous["image"]["reference"],
        runtime_secrets_path=runtime_secrets_path,
        docker_executable=docker_executable,
    )


def pre_migration_backup(
    request,
    record: dict[str, Any],
    projects_root: Path,
    backup_runner,
) -> None:
    # Deliberately not gated on backup_enabled: that flag governs the
    # schedule, while this dump is a precondition for a destructive step.
    # Turning scheduling off must not silently remove the safety net.
    if request.bundle.manifest["database"]["mode"] != "docker":
        return

    backup_runner(
        manifest=request.bundle.manifest,
        project=request.project,
        environment=request.environment,
        record=record,
        secrets_document=request.bundle.secrets,
        reason="pre-migration",
    )


def execute_prepared_release(
    request,
    record: dict[str, Any],
    records: list[dict[str, Any]],
    staged_bundle_path: Path,
    runtime_secrets_path: Path,
    projects_root: Path,
    releases_root: Path,
    runtime_secrets_root: Path,
    docker_executable: Path,
    compose_runtime_module,
    nginx_manager,
    backup_runner=None,
) -> dict[str, Any]:
    arguments = runtime_arguments(
        request,
        staged_bundle_path,
        runtime_secrets_path,
        docker_executable,
    )

    try:
        compose_runtime_module.validate_release_compose(**arguments)
    except ComposeRuntimeError as error:
        update_release_state(
            projects_root,
            record,
            status="failed",
            healthcheck_status="failed",
            healthcheck_error=error,
        )
        raise DeploymentExecutionError(str(error)) from error

    plan = nginx_manager.build_plan(request.bundle.manifest, record["release_id"])

    with nginx_manager.prepare(plan) as nginx_transaction:
        record = update_release_state(
            projects_root,
            record,
            status="deploying",
        )

        if record["migration"]["status"] != "not_required":
            # The most valuable snapshot is the one taken immediately before a
            # migration, and unlike retention this is not housekeeping: it is
            # the safety net for a destructive step. If the net cannot be
            # strung, the step does not happen.
            try:
                pre_migration_backup(
                    request=request,
                    record=record,
                    projects_root=projects_root,
                    backup_runner=backup_runner,
                )
            except (BackupRuntimeError, DatabaseRuntimeError) as error:
                update_release_state(
                    projects_root,
                    record,
                    status="failed",
                    migration_status="failed",
                    migration_error=f"pre-migration backup failed: {error}",
                )
                raise DeploymentExecutionError(
                    f"pre-migration backup failed: {error}"
                ) from error

            record = update_release_state(
                projects_root,
                record,
                migration_status="running",
            )

            try:
                compose_runtime_module.run_release_migration(**arguments)
            except ComposeRuntimeError as error:
                update_release_state(
                    projects_root,
                    record,
                    status="failed",
                    migration_status="failed",
                    migration_error=error,
                )
                raise DeploymentExecutionError(str(error)) from error

            record = update_release_state(
                projects_root,
                record,
                migration_status="succeeded",
            )

        record = update_release_state(
            projects_root,
            record,
            healthcheck_status="running",
        )

        try:
            nginx_transaction.stage()
            compose_runtime_module.start_release(**arguments)
            nginx_transaction.activate()
        except (ComposeRuntimeError, NginxTransactionError) as error:
            previous = find_release_by_id(
                records,
                record["previous_release_id"],
            )
            cleanup_errors = []

            try:
                if previous is None:
                    compose_runtime_module.stop_release(**arguments)
                else:
                    restore_previous_release(
                        previous,
                        releases_root,
                        runtime_secrets_root,
                        docker_executable,
                        compose_runtime_module,
                    )
            except ComposeRuntimeError as cleanup_error:
                cleanup_errors.append(f"rollback failed: {cleanup_error}")

            try:
                nginx_transaction.rollback()
            except NginxTransactionError as cleanup_error:
                cleanup_errors.append(f"nginx rollback failed: {cleanup_error}")

            rollback_succeeded = not cleanup_errors
            final_status = (
                "rolled_back"
                if previous is not None and rollback_succeeded
                else "failed"
            )
            error_message = str(error)

            if cleanup_errors:
                error_message += "; " + "; ".join(cleanup_errors)
            elif previous is not None:
                error_message += "; previous release restored; nginx fragments restored"

            update_release_state(
                projects_root,
                record,
                status=final_status,
                healthcheck_status="failed",
                healthcheck_error=error_message,
            )
            raise DeploymentExecutionError(error_message) from error

        # Commit ledger before releasing the shared nginx activation lock.
        return update_release_state(
            projects_root,
            record,
            status="deployed",
            healthcheck_status="succeeded",
        )


def prepare_release_database(
    request,
    runtime_secrets_path: Path,
    databases_root: Path,
    runtime_secrets_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    docker_executable: Path,
    database_ensurer,
) -> None:
    """Bring the declared database up and hand its URL to the release."""
    password = database_ensurer(
        manifest=request.bundle.manifest,
        project=request.project,
        environment=request.environment,
        secrets_document=request.bundle.secrets,
        databases_root=databases_root,
        runtime_secrets_root=runtime_secrets_root,
        age_key_file=age_key_file,
        sops_executable=sops_executable,
        docker_executable=docker_executable,
    )
    inject_database_url(runtime_secrets_path, password)


def build_backup_runner(
    databases_root: Path,
    backups_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    age_executable: Path,
    docker_executable: Path,
    backup_creator,
):
    """Bind the host paths so the release path can ask for a backup plainly."""

    def take_backup(**arguments):
        return backup_creator(
            databases_root=databases_root,
            backups_root=backups_root,
            age_key_file=age_key_file,
            sops_executable=sops_executable,
            age_executable=age_executable,
            docker_executable=docker_executable,
            **arguments,
        )

    return take_backup


def run_backup_schedule(
    request,
    systemd_root: Path,
    systemctl_executable: Path,
    timer_reconciler,
) -> dict[str, Any]:
    """Match the host timer to the release, warning rather than failing.

    A deployment that is already serving traffic must not be reported as
    failed because a timer could not be written; the warning surfaces in the
    deploy output and in `platform status`.
    """
    try:
        return timer_reconciler(
            manifest=request.bundle.manifest,
            project=request.project,
            environment=request.environment,
            systemd_root=systemd_root,
            systemctl_executable=systemctl_executable,
        )
    except (BackupScheduleError, OSError) as error:
        return {
            "unit": None,
            "state": "unknown",
            "interval_minutes": None,
            "warning": f"backup schedule not applied: {error}",
        }


def run_release_retention(
    request,
    record: dict[str, Any],
    projects_root: Path,
    releases_root: Path,
    runtime_secrets_root: Path,
    docker_executable: Path,
    image_remover,
) -> dict[str, Any]:
    """Reclaim superseded artefacts after the ledger has been committed.

    A deployment that succeeded must never be reported as failed because
    housekeeping did not, so every outcome here is a warning.
    """
    try:
        retained_images = resolve_retained_images(request.bundle.manifest)
        records = list_release_records(
            projects_root,
            record["project"],
            record["environment"],
        )

        return apply_retention(
            records=records,
            project=record["project"],
            environment=record["environment"],
            current_release_id=record["release_id"],
            retained_images=retained_images,
            projects_root=projects_root,
            releases_root=releases_root,
            runtime_secrets_root=runtime_secrets_root,
            docker_executable=docker_executable,
            image_remover=image_remover,
        )
    except (ReleaseLedgerError, ReleaseRetentionError, OSError) as error:
        return {
            "retained_images": None,
            "retained_image_digests": [],
            "removed_bundles": [],
            "removed_runtime_secrets": [],
            "removed_images": [],
            "warnings": [f"retention skipped: {error}"],
        }


def run_deploy(
    arguments: argparse.Namespace,
    projects_root: Path,
    releases_root: Path,
    lock_root: Path,
    runtime_secrets_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    secrets_materializer,
    registry_runtime_root: Path,
    docker_executable: Path,
    image_puller,
    token_stream,
    compose_runtime_module,
    nginx_manager,
    image_remover=remove_image,
    databases_root: Path = DEFAULT_DATABASES_ROOT,
    database_ensurer=ensure_project_database,
    backups_root: Path = DEFAULT_BACKUPS_ROOT,
    age_executable: Path = DEFAULT_AGE_EXECUTABLE,
    backup_creator=create_backup,
    systemd_root: Path = DEFAULT_SYSTEMD_ROOT,
    systemctl_executable: Path = DEFAULT_SYSTEMCTL_EXECUTABLE,
    timer_reconciler=reconcile_backup_timer,
) -> int:
    try:
        registry_username, registry_token = load_registry_credentials(
            arguments,
            token_stream,
        )
        request = load_deployment_request(
            bundle_path=arguments.bundle,
            project=arguments.project,
            environment=arguments.environment,
            image=arguments.image,
            release_tag=arguments.release_tag,
            minimum_age_recipients=arguments.minimum_age_recipients,
        )

        with project_environment_lock(
            lock_root,
            request.project,
            request.environment,
            "deploy",
        ):
            records = list_release_records(
                projects_root,
                request.project,
                request.environment,
            )
            existing = find_release_by_tag(
                records,
                request.release_tag,
            )
            current = find_latest_deployed_release(records)

            if existing is not None:
                if not release_matches_request(
                    existing,
                    request.image,
                    request.bundle.digest,
                ):
                    raise DeploymentRequestError(
                        "release tag already points to a " "different image or bundle"
                    )

                if existing["status"] == "deploying":
                    raise DeploymentExecutionError(
                        "release is left in deploying state; "
                        "migration outcome requires operator review"
                    )

                is_current = (
                    existing["status"] == "deployed"
                    and current is not None
                    and current["release_id"] == existing["release_id"]
                )

                # Only the running release makes a redeploy a genuine no-op.
                # Any other match is an operator asking for this tag again,
                # which the ledger must record as a new deployment.
                if is_current:
                    staged_bundle_path = resolve_release_bundle(
                        existing,
                        releases_root,
                    )
                    image_puller(
                        image=request.image,
                        registry_username=registry_username,
                        registry_token=registry_token,
                        runtime_root=registry_runtime_root,
                        docker_executable=docker_executable,
                    )
                    runtime_secrets_path = materialize_release_secrets(
                        request=request,
                        staged_bundle_path=staged_bundle_path,
                        release_id=existing["release_id"],
                        runtime_secrets_root=runtime_secrets_root,
                        age_key_file=age_key_file,
                        sops_executable=sops_executable,
                        secrets_materializer=secrets_materializer,
                    )
                    prepare_release_database(
                        request=request,
                        runtime_secrets_path=runtime_secrets_path,
                        databases_root=databases_root,
                        runtime_secrets_root=runtime_secrets_root,
                        age_key_file=age_key_file,
                        sops_executable=sops_executable,
                        docker_executable=docker_executable,
                        database_ensurer=database_ensurer,
                    )

                    document = build_deploy_result(
                        existing,
                        staged_bundle_path,
                        runtime_secrets_path,
                        reused=True,
                        containers_started=False,
                        schedule=run_backup_schedule(
                            request,
                            systemd_root,
                            systemctl_executable,
                            timer_reconciler,
                        ),
                    )

                    if arguments.json:
                        print(
                            json.dumps(
                                document,
                                indent=2,
                                sort_keys=True,
                            )
                        )
                    else:
                        print_deploy_result(document)

                    return 0

            staged_bundle_path = stage_verified_bundle(
                request.bundle,
                releases_root,
            )
            previous_release_id = current["release_id"] if current is not None else None

            record = build_prepared_release(
                request=request,
                staged_bundle_path=staged_bundle_path,
                releases_root=releases_root,
                previous_release_id=previous_release_id,
            )

            image_puller(
                image=request.image,
                registry_username=registry_username,
                registry_token=registry_token,
                runtime_root=registry_runtime_root,
                docker_executable=docker_executable,
            )

            runtime_secrets_path = materialize_release_secrets(
                request=request,
                staged_bundle_path=staged_bundle_path,
                release_id=record["release_id"],
                runtime_secrets_root=runtime_secrets_root,
                age_key_file=age_key_file,
                sops_executable=sops_executable,
                secrets_materializer=secrets_materializer,
            )

            # An application whose database will not start has nothing
            # worth writing into the ledger.
            prepare_release_database(
                request=request,
                runtime_secrets_path=runtime_secrets_path,
                databases_root=databases_root,
                runtime_secrets_root=runtime_secrets_root,
                age_key_file=age_key_file,
                sops_executable=sops_executable,
                docker_executable=docker_executable,
                database_ensurer=database_ensurer,
            )

            write_release_record(
                projects_root,
                record,
            )

            record = execute_prepared_release(
                request=request,
                record=record,
                records=records,
                staged_bundle_path=staged_bundle_path,
                runtime_secrets_path=runtime_secrets_path,
                projects_root=projects_root,
                releases_root=releases_root,
                runtime_secrets_root=runtime_secrets_root,
                docker_executable=docker_executable,
                compose_runtime_module=compose_runtime_module,
                nginx_manager=nginx_manager,
                backup_runner=build_backup_runner(
                    databases_root,
                    backups_root,
                    age_key_file,
                    sops_executable,
                    age_executable,
                    docker_executable,
                    backup_creator,
                ),
            )

            schedule = run_backup_schedule(
                request,
                systemd_root,
                systemctl_executable,
                timer_reconciler,
            )

            retention = run_release_retention(
                request=request,
                record=record,
                projects_root=projects_root,
                releases_root=releases_root,
                runtime_secrets_root=runtime_secrets_root,
                docker_executable=docker_executable,
                image_remover=image_remover,
            )

            document = build_deploy_result(
                record,
                staged_bundle_path,
                runtime_secrets_path,
                reused=False,
                containers_started=True,
                retention=retention,
                schedule=schedule,
            )

            if arguments.json:
                print(
                    json.dumps(
                        document,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print_deploy_result(document)

            return 0
    except (
        BundleStagingError,
        ComposeRuntimeError,
        DatabaseRuntimeError,
        DeploymentExecutionError,
        DeploymentRequestError,
        OperationLockError,
        RegistryPullError,
        ReleaseLedgerError,
        RuntimeSecretsError,
        NginxTransactionError,
        OSError,
    ) as error:
        print(f"deploy error: {error}", file=sys.stderr)
        return 1


def print_backup_result(document: dict[str, Any]) -> None:
    print(f"Backup written: {document['path']}")
    print(f"Reason: {document['reason']}")
    print(f"Size: {document['bytes']} bytes")
    print(f"Recipients: {document['recipients']}")
    print(f"Release: {document['release_id']}")
    print(f"Removed by retention: {len(document['removed_backups'])}")

    for warning in document["warnings"]:
        print(f"Backup warning: {warning}")

    offsite = document.get("offsite")

    if offsite is None:
        return

    if offsite["state"] == "not-configured":
        print("Offsite: not configured; backups stay on this host")
    elif offsite["state"] == "failed":
        print(f"Offsite: FAILED — {offsite['error']}")
    else:
        print(
            f"Offsite: {len(offsite['uploaded'])} object(s) uploaded to "
            f"{offsite['bucket']}"
        )


def resolve_backup_identity(
    arguments: argparse.Namespace,
) -> tuple[str, str]:
    if arguments.instance is not None:
        return split_instance(arguments.instance)

    if arguments.environment is None:
        raise BackupScheduleError("--environment is required with --project")

    return arguments.project, arguments.environment


def run_backup(
    arguments: argparse.Namespace,
    projects_root: Path,
    releases_root: Path,
    lock_root: Path,
    databases_root: Path,
    backups_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    age_executable: Path,
    docker_executable: Path,
    minimum_age_recipients: int,
    backup_creator=create_backup,
    offsite_config: Path = DEFAULT_OFFSITE_CONFIG,
    offsite_credentials: Path = DEFAULT_CREDENTIALS_FILE,
    uploader=upload_backups,
) -> int:
    try:
        project, environment = resolve_backup_identity(arguments)

        with project_environment_lock(
            lock_root,
            project,
            environment,
            "backup",
        ):
            records = list_release_records(
                projects_root,
                project,
                environment,
            )
            record = find_latest_deployed_release(records)

            if record is None:
                raise BackupRuntimeError(
                    "a backup describes a deployed release; none exists"
                )

            bundle = load_release_bundle(
                record,
                releases_root,
                minimum_age_recipients=minimum_age_recipients,
            )

            document = backup_creator(
                manifest=bundle.manifest,
                project=project,
                environment=environment,
                record=record,
                secrets_document=bundle.secrets,
                reason=arguments.reason,
                databases_root=databases_root,
                backups_root=backups_root,
                age_key_file=age_key_file,
                sops_executable=sops_executable,
                age_executable=age_executable,
                docker_executable=docker_executable,
            )

        # This is the one place the warnings-only rule bends. A host set up
        # for offsite backups that has quietly stopped uploading is exactly
        # the failure discovered too late, so the command exits non-zero --
        # while still reporting the dump that did succeed and stays on disk
        # for the next run to carry up.
        offsite_error = None

        try:
            document["offsite"] = uploader(
                project=project,
                environment=environment,
                directory=backups_root / project / environment,
                config_path=offsite_config,
                credentials_path=offsite_credentials,
            )
        except OffsiteError as error:
            document["offsite"] = {"state": "failed", "error": str(error)}
            offsite_error = error

        if arguments.json:
            print(json.dumps(document, indent=2, sort_keys=True))
        else:
            print_backup_result(document)

        if offsite_error is not None:
            print(f"backup error: {offsite_error}", file=sys.stderr)
            return 1

        return 0
    except (
        BackupRuntimeError,
        BackupScheduleError,
        DatabaseRuntimeError,
        OperationLockError,
        ReleaseLedgerError,
        OSError,
    ) as error:
        print(f"backup error: {error}", file=sys.stderr)
        return 1


def load_current_manifest(
    projects_root: Path,
    releases_root: Path,
    project: str,
    environment: str,
    minimum_age_recipients: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    records = list_release_records(projects_root, project, environment)
    record = find_latest_deployed_release(records)

    if record is None:
        raise RestoreRuntimeError(
            "no deployed release; the platform does not know this database"
        )

    bundle = load_release_bundle(
        record,
        releases_root,
        minimum_age_recipients=minimum_age_recipients,
    )

    return bundle.manifest, record


def run_restore(
    arguments: argparse.Namespace,
    projects_root: Path,
    releases_root: Path,
    lock_root: Path,
    databases_root: Path,
    backups_root: Path,
    runtime_secrets_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    age_executable: Path,
    docker_executable: Path,
    minimum_age_recipients: int,
    restorer=restore_backup,
    offsite_config: Path = DEFAULT_OFFSITE_CONFIG,
    downloader=download_backup,
    token_stream=None,
) -> int:
    try:
        if not arguments.confirm_destructive:
            raise RestoreRuntimeError(
                "restoring replaces the contents of the live database; "
                "pass --confirm-destructive to proceed"
            )

        if arguments.from_offsite:
            if arguments.stamp is None:
                raise RestoreRuntimeError(
                    "--from-offsite needs --from: name the backup to fetch"
                )

            downloader(
                project=arguments.project,
                environment=arguments.environment,
                stamp=arguments.stamp,
                directory=backups_root / arguments.project / arguments.environment,
                credentials=read_operator_credentials(token_stream),
                config_path=offsite_config,
            )

        # The lock is the guard against restoring underneath a deployment
        # that is mid-migration.
        with project_environment_lock(
            lock_root,
            arguments.project,
            arguments.environment,
            "restore",
        ):
            _, record = load_current_manifest(
                projects_root,
                releases_root,
                arguments.project,
                arguments.environment,
                minimum_age_recipients,
            )

            document = restorer(
                project=arguments.project,
                environment=arguments.environment,
                stamp=arguments.stamp,
                current_record=record,
                databases_root=databases_root,
                backups_root=backups_root,
                runtime_secrets_root=runtime_secrets_root,
                age_key_file=age_key_file,
                sops_executable=sops_executable,
                age_executable=age_executable,
                docker_executable=docker_executable,
            )

        if arguments.json:
            print(json.dumps(document, indent=2, sort_keys=True))
        else:
            print(f"Restored: {document['stamp']}")
            print(f"Taken on release: {document['release_tag']}")

            if document["revision_gap"]:
                print(f"Note: {document['revision_gap']}")

        return 0
    except (
        OffsiteError,
        RestoreRuntimeError,
        DatabaseRuntimeError,
        OperationLockError,
        ReleaseLedgerError,
        OSError,
    ) as error:
        print(f"restore error: {error}", file=sys.stderr)
        return 1


def run_verify_backup(
    arguments: argparse.Namespace,
    projects_root: Path,
    releases_root: Path,
    lock_root: Path,
    databases_root: Path,
    backups_root: Path,
    runtime_secrets_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    age_executable: Path,
    docker_executable: Path,
    minimum_age_recipients: int,
    verifier=verify_backup,
) -> int:
    try:
        manifest, _ = load_current_manifest(
            projects_root,
            releases_root,
            arguments.project,
            arguments.environment,
            minimum_age_recipients,
        )

        document = verifier(
            manifest=manifest,
            project=arguments.project,
            environment=arguments.environment,
            stamp=arguments.stamp,
            databases_root=databases_root,
            backups_root=backups_root,
            runtime_secrets_root=runtime_secrets_root,
            age_key_file=age_key_file,
            sops_executable=sops_executable,
            age_executable=age_executable,
            docker_executable=docker_executable,
        )

        if arguments.json:
            print(json.dumps(document, indent=2, sort_keys=True))
        else:
            print(f"Verified: {document['stamp']}")
            print(f"Query: {document['query']}")
            print(f"Result: {document['result']}")

        return 0
    except (
        RestoreRuntimeError,
        DatabaseRuntimeError,
        ReleaseLedgerError,
        OSError,
    ) as error:
        print(f"verify-backup error: {error}", file=sys.stderr)
        return 1


def run_rollback(
    arguments: argparse.Namespace,
    projects_root: Path,
    releases_root: Path,
    lock_root: Path,
    runtime_secrets_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    secrets_materializer,
    registry_runtime_root: Path,
    docker_executable: Path,
    image_puller,
    token_stream,
    compose_runtime_module,
    nginx_manager,
    image_remover=remove_image,
    databases_root: Path = DEFAULT_DATABASES_ROOT,
    database_ensurer=ensure_project_database,
    backups_root: Path = DEFAULT_BACKUPS_ROOT,
    age_executable: Path = DEFAULT_AGE_EXECUTABLE,
    backup_creator=create_backup,
    systemd_root: Path = DEFAULT_SYSTEMD_ROOT,
    systemctl_executable: Path = DEFAULT_SYSTEMCTL_EXECUTABLE,
    timer_reconciler=reconcile_backup_timer,
) -> int:
    try:
        registry_username, registry_token = load_registry_credentials(
            arguments,
            token_stream,
        )

        with project_environment_lock(
            lock_root,
            arguments.project,
            arguments.environment,
            "rollback",
        ):
            records = list_release_records(
                projects_root,
                arguments.project,
                arguments.environment,
            )

            if any(item["status"] == "deploying" for item in records):
                raise DeploymentExecutionError(
                    "unfinished deployment requires operator review"
                )

            current = find_latest_deployed_release(records)
            if current is None:
                raise DeploymentExecutionError(
                    "rollback requires a current deployed release"
                )

            target = select_rollback_release(
                records,
                arguments.target_tag,
            )

            if target["release_id"] == current["release_id"]:
                raise DeploymentExecutionError(
                    "rollback target is already the current release"
                )

            record = build_rollback_release(target, current)

            request = load_saved_release_request(
                record,
                releases_root,
                minimum_age_recipients=arguments.minimum_age_recipients,
            )
            current_request = load_saved_release_request(
                current,
                releases_root,
                minimum_age_recipients=arguments.minimum_age_recipients,
            )

            staged_bundle_path = resolve_release_bundle(
                record,
                releases_root,
            )
            current_bundle_path = resolve_release_bundle(
                current,
                releases_root,
            )

            prepared_runtime_paths = {}
            pulled_images = set()

            for saved_record, saved_request, saved_path in (
                (current, current_request, current_bundle_path),
                (record, request, staged_bundle_path),
            ):
                if saved_request.image not in pulled_images:
                    image_puller(
                        image=saved_request.image,
                        registry_username=registry_username,
                        registry_token=registry_token,
                        runtime_root=registry_runtime_root,
                        docker_executable=docker_executable,
                    )
                    pulled_images.add(saved_request.image)

                prepared_runtime_paths[saved_record["release_id"]] = (
                    materialize_release_secrets(
                        request=saved_request,
                        staged_bundle_path=saved_path,
                        release_id=saved_record["release_id"],
                        runtime_secrets_root=runtime_secrets_root,
                        age_key_file=age_key_file,
                        sops_executable=sops_executable,
                        secrets_materializer=secrets_materializer,
                    )
                )

            for saved_request, saved_record in (
                (current_request, current),
                (request, record),
            ):
                prepare_release_database(
                    request=saved_request,
                    runtime_secrets_path=prepared_runtime_paths[
                        saved_record["release_id"]
                    ],
                    databases_root=databases_root,
                    runtime_secrets_root=runtime_secrets_root,
                    age_key_file=age_key_file,
                    sops_executable=sops_executable,
                    docker_executable=docker_executable,
                    database_ensurer=database_ensurer,
                )

            compose_runtime_module.validate_release_compose(
                **runtime_arguments(
                    current_request,
                    current_bundle_path,
                    prepared_runtime_paths[current["release_id"]],
                    docker_executable,
                )
            )

            runtime_secrets_path = prepared_runtime_paths[record["release_id"]]

            write_release_record(projects_root, record)

            record = execute_prepared_release(
                request=request,
                record=record,
                records=records,
                staged_bundle_path=staged_bundle_path,
                runtime_secrets_path=runtime_secrets_path,
                projects_root=projects_root,
                releases_root=releases_root,
                runtime_secrets_root=runtime_secrets_root,
                docker_executable=docker_executable,
                compose_runtime_module=compose_runtime_module,
                nginx_manager=nginx_manager,
            )

            retention = run_release_retention(
                request=request,
                record=record,
                projects_root=projects_root,
                releases_root=releases_root,
                runtime_secrets_root=runtime_secrets_root,
                docker_executable=docker_executable,
                image_remover=image_remover,
            )

            document = build_deploy_result(
                record,
                staged_bundle_path,
                runtime_secrets_path,
                reused=False,
                containers_started=True,
                retention=retention,
                schedule=run_backup_schedule(
                    request,
                    systemd_root,
                    systemctl_executable,
                    timer_reconciler,
                ),
            )
            document.update(
                {
                    "operation": "rollback",
                    "target_release_id": target["release_id"],
                    "target_release_tag": target["release_tag"],
                    "previous_release_id": current["release_id"],
                }
            )

            if arguments.json:
                print(json.dumps(document, indent=2, sort_keys=True))
            else:
                print(f"Rollback target: {target['release_tag']}")
                print_deploy_result(document)

            return 0

    except (
        ComposeRuntimeError,
        DatabaseRuntimeError,
        DeploymentExecutionError,
        DeploymentRequestError,
        OperationLockError,
        RegistryPullError,
        ReleaseLedgerError,
        RuntimeSecretsError,
        NginxTransactionError,
        OSError,
    ) as error:
        print(f"rollback error: {error}", file=sys.stderr)
        return 1


def main(
    argv: list[str] = None,
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    releases_root: Path = DEFAULT_RELEASES_ROOT,
    lock_root: Path = DEFAULT_LOCK_ROOT,
    runtime_secrets_root: Path = DEFAULT_RUNTIME_SECRETS_ROOT,
    age_key_file: Path = DEFAULT_AGE_KEY_FILE,
    sops_executable: Path = DEFAULT_SOPS_EXECUTABLE,
    secrets_materializer=materialize_env_secrets,
    registry_runtime_root: Path = DEFAULT_REGISTRY_RUNTIME_ROOT,
    docker_executable: Path = DEFAULT_DOCKER_EXECUTABLE,
    image_puller=pull_immutable_image,
    image_remover=remove_image,
    databases_root: Path = DEFAULT_DATABASES_ROOT,
    database_ensurer=ensure_project_database,
    backups_root: Path = DEFAULT_BACKUPS_ROOT,
    age_executable: Path = DEFAULT_AGE_EXECUTABLE,
    backup_creator=create_backup,
    systemd_root: Path = DEFAULT_SYSTEMD_ROOT,
    systemctl_executable: Path = DEFAULT_SYSTEMCTL_EXECUTABLE,
    timer_reconciler=reconcile_backup_timer,
    offsite_config: Path = DEFAULT_OFFSITE_CONFIG,
    offsite_credentials: Path = DEFAULT_CREDENTIALS_FILE,
    uploader=upload_backups,
    offsite_reporter=offsite_status,
    downloader=download_backup,
    token_stream=None,
    compose_runtime_module=compose_runtime,
    nginx_manager=None,
    nginx_vhost_root: Path = DEFAULT_NGINX_VHOST_ROOT,
    nginx_ownership_root: Path = DEFAULT_NGINX_OWNERSHIP_ROOT,
    nginx_default_config: Path = DEFAULT_NGINX_CONFIG,
    nginx_raw_allowlist: Path = DEFAULT_NGINX_RAW_ALLOWLIST,
    nginx_container: str = DEFAULT_NGINX_CONTAINER,
) -> int:
    if token_stream is None:
        token_stream = sys.stdin.buffer

    arguments = parse_arguments(argv)

    if arguments.command in ("deploy", "rollback") and nginx_manager is None:
        nginx_manager = NginxTransactionManager(
            vhost_root=nginx_vhost_root,
            ownership_root=nginx_ownership_root,
            default_config=nginx_default_config,
            lock_root=lock_root,
            raw_allowlist=nginx_raw_allowlist,
            docker_executable=docker_executable,
            nginx_container=nginx_container,
        )

    if arguments.command in ("deploy", "rollback"):
        handler = run_deploy if arguments.command == "deploy" else run_rollback
        return handler(
            arguments,
            projects_root,
            releases_root,
            lock_root,
            runtime_secrets_root,
            age_key_file,
            sops_executable,
            secrets_materializer,
            registry_runtime_root,
            docker_executable,
            image_puller,
            token_stream,
            compose_runtime_module,
            nginx_manager,
            image_remover,
            databases_root,
            database_ensurer,
            backups_root,
            age_executable,
            backup_creator,
            systemd_root,
            systemctl_executable,
            timer_reconciler,
        )

    if arguments.command in ("restore", "verify-backup"):
        handler = run_restore if arguments.command == "restore" else run_verify_backup
        return handler(
            arguments,
            projects_root,
            releases_root,
            lock_root,
            databases_root,
            backups_root,
            runtime_secrets_root,
            age_key_file,
            sops_executable,
            age_executable,
            docker_executable,
            arguments.minimum_age_recipients,
            *(
                (restore_backup, offsite_config, downloader, token_stream)
                if arguments.command == "restore"
                else ()
            ),
        )

    if arguments.command == "backup":
        return run_backup(
            arguments,
            projects_root,
            releases_root,
            lock_root,
            databases_root,
            backups_root,
            age_key_file,
            sops_executable,
            age_executable,
            docker_executable,
            arguments.minimum_age_recipients,
            backup_creator,
            offsite_config,
            offsite_credentials,
            uploader,
        )

    if arguments.command == "status":
        return run_status(
            arguments,
            projects_root,
            backups_root,
            offsite_config,
            offsite_credentials,
            offsite_reporter,
        )

    print(
        f"unsupported platform command: {arguments.command}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
