#!/usr/bin/env python3

"""Keep the host's export timers equal to what the release declares.

An export job is a timer per project, environment and job name. Like the
backup timer, the units are installed by convergence and a deploy writes only
the per-instance calendar, so what runs and as whom is fixed by the operator.
Unlike the backup timer there may be several, and a job that leaves the
manifest must take its timer with it: the deploy lists what the host has for
this scope and removes whatever the release no longer asks for.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .backup_schedule import (
    ALLOWED_ENVIRONMENTS,
    DEFAULT_SYSTEMCTL_EXECUTABLE,
    DEFAULT_SYSTEMD_ROOT,
    run_systemctl,
    split_instance,
    timer_instance,
)

DEFAULT_SYSTEMD_ANALYZE_EXECUTABLE = Path("/usr/bin/systemd-analyze")

TIMER_TEMPLATE = "platform-export@"
# A job name has no hyphen, so the instance splits unambiguously on its last
# one even though project names may contain them.
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
CALENDAR_PATTERN = re.compile(r"^[A-Za-z0-9 *,.:/~-]{1,64}$")


class ExportScheduleError(RuntimeError):
    pass


def export_instance(project: str, environment: str, name: str) -> str:
    if not NAME_PATTERN.fullmatch(name):
        raise ExportScheduleError(f"invalid export name: {name}")
    return f"{timer_instance(project, environment)}-{name}"


def split_export_instance(instance: str) -> tuple[str, str, str]:
    """Recover project, environment and job name from ``%i``."""
    scope, _, name = instance.rpartition("-")
    if not NAME_PATTERN.fullmatch(name):
        raise ExportScheduleError(f"invalid export timer instance: {instance}")
    try:
        project, environment = split_instance(scope)
    except Exception as error:
        raise ExportScheduleError(
            f"invalid export timer instance: {instance}"
        ) from error
    return project, environment, name


def declared_jobs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The jobs the manifest asks for; none unless the database is the platform's."""
    exports = manifest.get("exports")
    if not isinstance(exports, dict):
        return []
    if manifest.get("database", {}).get("mode") != "docker":
        return []
    jobs = exports.get("jobs")
    return (
        [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []
    )


def find_job(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    for job in declared_jobs(manifest):
        if job.get("name") == name:
            return job
    raise ExportScheduleError(f"the release declares no export named {name}")


def check_calendar(
    expression: str,
    systemd_analyze_executable: Path = DEFAULT_SYSTEMD_ANALYZE_EXECUTABLE,
    runner=subprocess.run,
) -> None:
    """Ask systemd itself whether the calendar expression means anything.

    The manifest schema only bounds the characters; ``Mon 25:00`` passes it.
    A timer with an expression systemd refuses would be enabled and never
    fire, so the question is put to the host before anything is written.
    """
    if not CALENDAR_PATTERN.fullmatch(expression):
        raise ExportScheduleError(f"invalid schedule: {expression!r}")
    try:
        result = runner(
            [
                str(systemd_analyze_executable),
                "calendar",
                "--iterations=1",
                expression,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ExportScheduleError(
            "systemd-analyze could not be executed to check the schedule"
        ) from error
    if result.returncode != 0:
        raise ExportScheduleError(
            f"systemd does not accept the schedule {expression!r}"
            " (a systemd calendar expression, e.g. 'Mon 06:00' or 'daily')"
        )


def render_schedule_override(expression: str) -> bytes:
    """Pin the calendar; an empty assignment first clears the template's own.

    Persistent lets a run the host slept through happen at the next boot,
    which is what a weekly report wants. A few minutes of random delay keep
    several jobs of several applications from hitting the database at once.
    """
    return (
        "# Written by platform deploy from exports.jobs[].schedule.\n"
        "[Timer]\n"
        "OnCalendar=\n"
        f"OnCalendar={expression}\n"
        "Persistent=true\n"
        "RandomizedDelaySec=5min\n"
    ).encode("utf-8")


def override_directory(systemd_root: Path, instance: str) -> Path:
    return systemd_root / f"{TIMER_TEMPLATE}{instance}.timer.d"


def write_schedule_override(
    systemd_root: Path, instance: str, expression: str
) -> tuple[Path, bool]:
    directory = override_directory(systemd_root, instance)
    if directory.is_symlink():
        raise ExportScheduleError("timer override path cannot be a symbolic link")
    directory.mkdir(mode=0o755, parents=True, exist_ok=True)
    destination = directory / "schedule.conf"
    content = render_schedule_override(expression)
    if destination.is_file() and destination.read_bytes() == content:
        return destination, False
    destination.write_bytes(content)
    destination.chmod(0o644)
    return destination, True


def existing_instances(systemd_root: Path, project: str, environment: str) -> list:
    """The export timers this scope already has on the host, by their drop-ins."""
    prefix = f"{TIMER_TEMPLATE}{timer_instance(project, environment)}-"
    found = []
    try:
        entries = sorted(systemd_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.startswith(prefix) or not entry.name.endswith(".timer.d"):
            continue
        name = entry.name[len(prefix) : -len(".timer.d")]
        if NAME_PATTERN.fullmatch(name):
            found.append(name)
    return found


def enable_export_timer(
    project: str,
    environment: str,
    name: str,
    expression: str,
    systemd_root: Path = DEFAULT_SYSTEMD_ROOT,
    systemctl_executable: Path = DEFAULT_SYSTEMCTL_EXECUTABLE,
    systemd_analyze_executable: Path = DEFAULT_SYSTEMD_ANALYZE_EXECUTABLE,
    runner=subprocess.run,
) -> dict[str, Any]:
    instance = export_instance(project, environment, name)
    unit = f"{TIMER_TEMPLATE}{instance}.timer"
    check_calendar(expression, systemd_analyze_executable, runner)
    _, changed = write_schedule_override(systemd_root, instance, expression)
    if changed:
        _systemctl(["daemon-reload"], systemctl_executable, runner)
    _systemctl(["enable", "--now", unit], systemctl_executable, runner)
    return {"name": name, "unit": unit, "schedule": expression, "state": "enabled"}


def disable_export_timer(
    project: str,
    environment: str,
    name: str,
    systemd_root: Path = DEFAULT_SYSTEMD_ROOT,
    systemctl_executable: Path = DEFAULT_SYSTEMCTL_EXECUTABLE,
    runner=subprocess.run,
) -> dict[str, Any]:
    instance = export_instance(project, environment, name)
    unit = f"{TIMER_TEMPLATE}{instance}.timer"
    directory = override_directory(systemd_root, instance)
    existed = directory.is_dir() and not directory.is_symlink()
    _systemctl(["disable", "--now", unit], systemctl_executable, runner)
    if existed:
        shutil.rmtree(directory)
        _systemctl(["daemon-reload"], systemctl_executable, runner)
    return {"name": name, "unit": unit, "schedule": None, "state": "disabled"}


def disable_export_timers(
    project: str,
    environment: str,
    systemd_root: Path = DEFAULT_SYSTEMD_ROOT,
    systemctl_executable: Path = DEFAULT_SYSTEMCTL_EXECUTABLE,
    runner=subprocess.run,
) -> list:
    """Every export timer of the scope, gone; nothing to do when there are none."""
    return [
        disable_export_timer(
            project,
            environment,
            name,
            systemd_root=systemd_root,
            systemctl_executable=systemctl_executable,
            runner=runner,
        )
        for name in existing_instances(systemd_root, project, environment)
    ]


def reconcile_export_timers(
    manifest: dict[str, Any],
    project: str,
    environment: str,
    systemd_root: Path = DEFAULT_SYSTEMD_ROOT,
    systemctl_executable: Path = DEFAULT_SYSTEMCTL_EXECUTABLE,
    systemd_analyze_executable: Path = DEFAULT_SYSTEMD_ANALYZE_EXECUTABLE,
    runner=subprocess.run,
) -> dict[str, Any]:
    """Make the host's export timers match the release.

    One job's bad schedule costs that job, not the others: the rest are still
    enabled and the refusal is reported beside them.
    """
    wanted = {job["name"]: job["schedule"] for job in declared_jobs(manifest)}
    jobs = []
    warnings = []
    for name, expression in wanted.items():
        try:
            jobs.append(
                enable_export_timer(
                    project,
                    environment,
                    name,
                    expression,
                    systemd_root=systemd_root,
                    systemctl_executable=systemctl_executable,
                    systemd_analyze_executable=systemd_analyze_executable,
                    runner=runner,
                )
            )
        except (ExportScheduleError, OSError) as error:
            jobs.append(
                {"name": name, "unit": None, "schedule": expression, "state": "unknown"}
            )
            warnings.append(f"export {name} not scheduled: {error}")
    removed = []
    for name in existing_instances(systemd_root, project, environment):
        if name in wanted:
            continue
        try:
            removed.append(
                disable_export_timer(
                    project,
                    environment,
                    name,
                    systemd_root=systemd_root,
                    systemctl_executable=systemctl_executable,
                    runner=runner,
                )["unit"]
            )
        except (ExportScheduleError, OSError) as error:
            warnings.append(f"export {name} timer not removed: {error}")
    return {"jobs": jobs, "removed": removed, "warnings": warnings}


def _systemctl(arguments: list, systemctl_executable: Path, runner) -> None:
    try:
        run_systemctl(arguments, systemctl_executable, runner)
    except Exception as error:
        raise ExportScheduleError(str(error)) from error


__all__ = [
    "ALLOWED_ENVIRONMENTS",
    "ExportScheduleError",
    "check_calendar",
    "declared_jobs",
    "disable_export_timer",
    "disable_export_timers",
    "enable_export_timer",
    "existing_instances",
    "export_instance",
    "find_job",
    "reconcile_export_timers",
    "render_schedule_override",
    "split_export_instance",
]
