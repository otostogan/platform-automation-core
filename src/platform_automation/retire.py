"""Take an application off a host, in two deliberate steps.

``retire`` stops what runs and frees what the host was holding for it —
containers, domains, the backup schedule — and leaves every byte of data
where it is: the database volume, the dumps, the ledger. It is reversible by
an ordinary deploy. ``purge`` is the irreversible half, allowed only after
``retire`` and only with the destructive flag, and it never reaches offsite
copies: the host holds a write-only credential there on purpose.
"""

import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Optional

from .backup_schedule import (
    DEFAULT_SYSTEMCTL_EXECUTABLE,
    DEFAULT_SYSTEMD_ROOT,
    disable_backup_timer,
)
from .database_runtime import database_resource_name
from .nginx_transaction import NginxTransactionError, build_fragment_plan
from .operation_lock import project_environment_lock
from .release_ledger import (
    ReleaseLedgerError,
    find_latest_deployed_release,
    list_release_records,
    utc_timestamp,
    validate_ledger_identity,
)

MARKER_NAME = "retired.json"


class RetireError(RuntimeError):
    pass


def marker_path(projects_root: Path, project: str, environment: str) -> Path:
    return projects_root / project / environment / MARKER_NAME


def is_retired(projects_root: Path, project: str, environment: str) -> bool:
    path = marker_path(projects_root, project, environment)
    return path.is_file() and not path.is_symlink()


def read_marker(projects_root: Path, project: str, environment: str) -> Optional[dict]:
    path = marker_path(projects_root, project, environment)
    if not is_retired(projects_root, project, environment):
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def write_marker(
    projects_root: Path, project: str, environment: str, payload: dict
) -> Path:
    path = marker_path(projects_root, project, environment)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(".retired.json.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def clear_marker(projects_root: Path, project: str, environment: str) -> bool:
    path = marker_path(projects_root, project, environment)
    if is_retired(projects_root, project, environment):
        path.unlink()
        return True
    return False


def application_project_name(project: str, environment: str) -> str:
    return f"{project}-{environment}"


def compose_down(
    docker_executable: Path,
    project_name: str,
    volumes: bool,
    runner=subprocess.run,
) -> None:
    """``docker compose -p <name> down`` finds the resources by label; no file needed."""
    command = [
        str(docker_executable),
        "compose",
        "--project-name",
        project_name,
        "down",
        "--remove-orphans",
    ]
    if volumes:
        command.append("--volumes")
    try:
        result = runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RetireError(
            f"docker compose down could not run for {project_name}"
        ) from error
    if result.returncode != 0:
        raise RetireError(
            f"docker compose down failed for {project_name} (exit {result.returncode})"
        )


def release_domains(nginx_manager, project: str, environment: str) -> list:
    """Free every fragment and htpasswd file this scope owns, through the transaction."""
    plan = build_fragment_plan(project, environment, uuid.uuid4().hex, {}, {})
    with nginx_manager.prepare(plan) as transaction:
        transaction.stage()
        released = sorted(transaction.previous_hosts)
        transaction.activate()
    return released


def retire(
    project: str,
    environment: str,
    projects_root: Path,
    lock_root: Path,
    docker_executable: Path,
    nginx_manager,
    systemd_root: Path = DEFAULT_SYSTEMD_ROOT,
    systemctl_executable: Path = DEFAULT_SYSTEMCTL_EXECUTABLE,
    runner=None,
    timer_disabler=disable_backup_timer,
) -> dict[str, Any]:
    validate_ledger_identity(project, environment)
    runner = subprocess.run if runner is None else runner
    with project_environment_lock(lock_root, project, environment, "retire"):
        records = list_release_records(projects_root, project, environment)
        if not records:
            raise RetireError("nothing was ever deployed here; nothing to retire")
        if any(record["status"] == "deploying" for record in records):
            raise RetireError("unfinished deployment requires operator review")
        current = find_latest_deployed_release(records)

        # Containers first: docker-gen drops a host from its render only once
        # the container carrying it is gone, and the nginx transaction waits
        # for exactly that before it writes the new config.
        compose_down(
            docker_executable,
            application_project_name(project, environment),
            False,
            runner,
        )
        compose_down(
            docker_executable,
            database_resource_name(project, environment),
            False,
            runner,
        )
        timer = timer_disabler(
            project,
            environment,
            systemd_root=systemd_root,
            systemctl_executable=systemctl_executable,
            runner=runner,
        )
        try:
            released = release_domains(nginx_manager, project, environment)
        except NginxTransactionError as error:
            raise RetireError(f"could not release the domains: {error}") from error

        marker = {
            "api_version": "platform-retired/v1",
            "project": project,
            "environment": environment,
            "retired_at": utc_timestamp(),
            "last_release_id": current["release_id"] if current else None,
            "last_release_tag": current["release_tag"] if current else None,
        }
        write_marker(projects_root, project, environment, marker)

    return {
        "operation": "retire",
        "project": project,
        "environment": environment,
        "last_release": marker["last_release_tag"],
        "domains_released": released,
        "backup_timer": timer["unit"],
        "kept": [
            "database volume " + database_resource_name(project, environment),
            "local backups",
            "offsite copies",
            "release ledger and bundles",
        ],
        "revive_with": "platform deploy",
    }


def _remove_tree(path: Path, removed: list) -> None:
    if path.is_symlink():
        raise RetireError(f"refusing to purge through a symbolic link: {path}")
    if path.exists():
        shutil.rmtree(path)
        removed.append(str(path))


def purge(
    project: str,
    environment: str,
    projects_root: Path,
    releases_root: Path,
    backups_root: Path,
    databases_root: Path,
    runtime_secrets_root: Path,
    lock_root: Path,
    docker_executable: Path,
    nginx_ownership_root: Path,
    confirmed: bool,
    systemd_root: Path = DEFAULT_SYSTEMD_ROOT,
    systemctl_executable: Path = DEFAULT_SYSTEMCTL_EXECUTABLE,
    runner=None,
    timer_disabler=disable_backup_timer,
) -> dict[str, Any]:
    validate_ledger_identity(project, environment)
    runner = subprocess.run if runner is None else runner
    if not confirmed:
        raise RetireError(
            "purge deletes the database volume, the local backups and the release"
            " history; pass --confirm-destructive to proceed"
        )
    if not is_retired(projects_root, project, environment):
        raise RetireError("purge requires a retired application; run: platform retire")

    removed: list = []
    with project_environment_lock(lock_root, project, environment, "purge"):
        compose_down(
            docker_executable,
            application_project_name(project, environment),
            True,
            runner,
        )
        compose_down(
            docker_executable,
            database_resource_name(project, environment),
            True,
            runner,
        )
        timer_disabler(
            project,
            environment,
            systemd_root=systemd_root,
            systemctl_executable=systemctl_executable,
            runner=runner,
        )
        for root in (releases_root, backups_root, databases_root, runtime_secrets_root):
            _remove_tree(root / project / environment, removed)
            parent = root / project
            if (
                parent.is_dir()
                and not parent.is_symlink()
                and not any(parent.iterdir())
            ):
                parent.rmdir()
        ownership = nginx_ownership_root / f"{project}--{environment}.json"
        if ownership.is_file() and not ownership.is_symlink():
            ownership.unlink()
            removed.append(str(ownership))
        # The ledger goes last: until here a crash leaves a retired scope that
        # purge can be run against again.
        _remove_tree(projects_root / project / environment, removed)
        parent = projects_root / project
        if parent.is_dir() and not parent.is_symlink() and not any(parent.iterdir()):
            parent.rmdir()

    return {
        "operation": "purge",
        "project": project,
        "environment": environment,
        "removed": removed,
        "docker_volume_removed": database_resource_name(project, environment),
        "kept": ["offsite copies (the host cannot delete them)"],
    }
