#!/usr/bin/env python3

"""Turn a declared backup cadence into host state, and keep it honest.

The cadence lives in the manifest and arrives with each release; the timer
lives on the host and outlives the release that created it. Anything that
outlives its creator needs an owner and a removal path, so a deploy both
writes the timer and takes it away when the application stops asking for one.

The units themselves are installed by the `platform_cli` role as templates.
A deploy only writes the per-instance interval and enables the timer, so the
privileged part of this — what actually runs, and as whom — is fixed by
convergence rather than by whatever arrived in the last bundle.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .backup_runtime import DEFAULT_INTERVAL_MINUTES

DEFAULT_SYSTEMD_ROOT = Path("/etc/systemd/system")
DEFAULT_SYSTEMCTL_EXECUTABLE = Path("/usr/bin/systemctl")

TIMER_TEMPLATE = "platform-backup@"
ALLOWED_ENVIRONMENTS = ("lab", "staging", "production")
# A project may be 63 characters on its own, so the combined instance has
# room for the longest project plus the longest environment. systemd
# allows far more than this.
INSTANCE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,126}$")

MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 1440


class BackupScheduleError(RuntimeError):
    pass


def timer_instance(project: str, environment: str) -> str:
    """Name one timer per project and environment.

    Environment never contains a hyphen and comes from a closed set, so the
    instance splits back apart unambiguously on its last hyphen even though
    project names may contain them.
    """
    if environment not in ALLOWED_ENVIRONMENTS:
        raise BackupScheduleError(f"invalid environment: {environment}")

    instance = f"{project}-{environment}"

    if not INSTANCE_PATTERN.fullmatch(instance):
        raise BackupScheduleError(f"invalid timer instance: {instance}")

    return instance


PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


def split_instance(instance: str) -> tuple[str, str]:
    """Recover project and environment from a systemd instance name.

    The name reaches this from `%i` and goes straight back out as `--project`,
    so the recovered halves are validated as strictly as they were built.
    """
    project, _, environment = instance.rpartition("-")

    if environment not in ALLOWED_ENVIRONMENTS:
        raise BackupScheduleError(f"invalid timer instance: {instance}")

    if not PROJECT_PATTERN.fullmatch(project):
        raise BackupScheduleError(f"invalid timer instance: {instance}")

    return project, environment


def resolve_interval(manifest: dict[str, Any]) -> int:
    backup = manifest["database"].get("backup") or {}
    interval = backup.get("interval_minutes", DEFAULT_INTERVAL_MINUTES)

    if (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or interval < MIN_INTERVAL_MINUTES
        or interval > MAX_INTERVAL_MINUTES
    ):
        raise BackupScheduleError(
            f"backup interval must be between {MIN_INTERVAL_MINUTES} and "
            f"{MAX_INTERVAL_MINUTES} minutes"
        )

    return interval


def backups_are_scheduled(manifest: dict[str, Any]) -> bool:
    database = manifest["database"]

    return database["mode"] == "docker" and bool(database.get("backup_enabled"))


def render_interval_override(interval_minutes: int) -> bytes:
    """Spread the load, and never let a missed window pile up.

    RandomizedDelaySec keeps every project on a host from dumping at the same
    instant; Persistent catches up one run after downtime rather than one per
    window missed.
    """
    return (
        "# Written by platform deploy from database.backup.interval_minutes.\n"
        "[Timer]\n"
        "OnUnitActiveSec=\n"
        f"OnUnitActiveSec={interval_minutes}min\n"
        "OnBootSec=\n"
        f"OnBootSec={min(interval_minutes, 30)}min\n"
        "RandomizedDelaySec="
        f"{max(interval_minutes // 10, 1)}min\n"
    ).encode("utf-8")


def run_systemctl(
    arguments: list[str],
    systemctl_executable: Path,
    runner=subprocess.run,
) -> None:
    try:
        result = runner(
            [str(systemctl_executable), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackupScheduleError(
            f"systemctl {arguments[0]} could not be executed"
        ) from error

    if result.returncode != 0:
        raise BackupScheduleError(f"systemctl {arguments[0]} failed")


def override_directory(systemd_root: Path, instance: str) -> Path:
    return systemd_root / f"{TIMER_TEMPLATE}{instance}.timer.d"


def write_interval_override(
    systemd_root: Path,
    instance: str,
    interval_minutes: int,
) -> tuple[Path, bool]:
    """Write the drop-in, reporting whether anything actually changed."""
    directory = override_directory(systemd_root, instance)

    if directory.is_symlink():
        raise BackupScheduleError("timer override path cannot be a symbolic link")

    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    destination = directory / "interval.conf"
    content = render_interval_override(interval_minutes)

    if destination.is_file() and destination.read_bytes() == content:
        return destination, False

    destination.write_bytes(content)
    destination.chmod(0o644)

    return destination, True


def enable_backup_timer(
    project: str,
    environment: str,
    interval_minutes: int,
    systemd_root: Path = DEFAULT_SYSTEMD_ROOT,
    systemctl_executable: Path = DEFAULT_SYSTEMCTL_EXECUTABLE,
    runner=subprocess.run,
) -> dict[str, Any]:
    instance = timer_instance(project, environment)
    unit = f"{TIMER_TEMPLATE}{instance}.timer"
    _, changed = write_interval_override(systemd_root, instance, interval_minutes)

    if changed:
        run_systemctl(["daemon-reload"], systemctl_executable, runner)

    run_systemctl(["enable", "--now", unit], systemctl_executable, runner)

    return {
        "unit": unit,
        "state": "enabled",
        "interval_minutes": interval_minutes,
    }


def disable_backup_timer(
    project: str,
    environment: str,
    systemd_root: Path = DEFAULT_SYSTEMD_ROOT,
    systemctl_executable: Path = DEFAULT_SYSTEMCTL_EXECUTABLE,
    runner=subprocess.run,
) -> dict[str, Any]:
    """Stop scheduling backups an application no longer asks for."""
    instance = timer_instance(project, environment)
    unit = f"{TIMER_TEMPLATE}{instance}.timer"
    directory = override_directory(systemd_root, instance)
    existed = directory.is_dir() and not directory.is_symlink()

    run_systemctl(["disable", "--now", unit], systemctl_executable, runner)

    if existed:
        shutil.rmtree(directory)
        run_systemctl(["daemon-reload"], systemctl_executable, runner)

    return {
        "unit": unit,
        "state": "disabled",
        "interval_minutes": None,
    }


def reconcile_backup_timer(
    manifest: dict[str, Any],
    project: str,
    environment: str,
    systemd_root: Path = DEFAULT_SYSTEMD_ROOT,
    systemctl_executable: Path = DEFAULT_SYSTEMCTL_EXECUTABLE,
    runner=subprocess.run,
) -> dict[str, Any]:
    """Make the host's timer match what the release declares."""
    if not backups_are_scheduled(manifest):
        return disable_backup_timer(
            project,
            environment,
            systemd_root=systemd_root,
            systemctl_executable=systemctl_executable,
            runner=runner,
        )

    return enable_backup_timer(
        project,
        environment,
        resolve_interval(manifest),
        systemd_root=systemd_root,
        systemctl_executable=systemctl_executable,
        runner=runner,
    )
