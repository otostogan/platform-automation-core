"""Write what the platform knows about this host as Prometheus text.

A collector cannot read per-container figures without the container runtime's
own socket, which is root on the host. The platform already has everything a
dashboard needs — which containers belong to which project and environment,
what is deployed, when the last dump was taken, which domains a release
serves — so it writes that into one file a collector reads. Nothing here
opens a port, and nothing in the file is a secret: names, numbers, timestamps.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .backup_runtime import DEFAULT_BACKUPS_ROOT, list_backups
from .backup_schedule import backups_are_scheduled
from .compose_runtime import ComposeRuntimeError, load_staged_manifest
from .domains import domain_service
from .release_ledger import (
    ReleaseLedgerError,
    find_latest_deployed_release,
    list_project_scopes,
    list_release_records,
    resolve_release_bundle,
)
from .restore_runtime import VERIFICATION_SUCCEEDED, last_verification
from .retire import is_retired

DEFAULT_OUTPUT = Path("/var/lib/platform/observability/textfile/platform.prom")
DEFAULT_PROJECTS_ROOT = Path("/var/lib/platform/projects")
DEFAULT_RELEASES_ROOT = Path("/var/lib/platform/releases")
DEFAULT_DOCKER = Path("/usr/bin/docker")
ENVIRONMENTS = "lab|staging|production"
APPLICATION = re.compile(rf"^(.+)-({ENVIRONMENTS})$")
DATABASE = re.compile(rf"^platform-db-(.+)-({ENVIRONMENTS})$")
PLATFORM = re.compile(r"^platform-(proxy|observability|observability-collector)$")
SIZE = re.compile(r"^\s*([0-9.]+)\s*([kKMGTP]?i?B)\s*$")
UNITS = {
    "B": 1,
    "kB": 1000,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}
STAMP = re.compile(r"^(\d{8}T\d{6}Z)")
LABEL_ESCAPE = str.maketrans({"\\": "\\\\", '"': '\\"', "\n": "\\n"})


def scope_labels(compose_project: str, service: str) -> dict:
    """project / environment / service the way the platform names things."""
    match = DATABASE.match(compose_project or "")
    if match:
        return {
            "project": match.group(1),
            "environment": match.group(2),
            "service": "postgres",
        }
    match = APPLICATION.match(compose_project or "")
    if match:
        return {
            "project": match.group(1),
            "environment": match.group(2),
            "service": service,
        }
    if PLATFORM.match(compose_project or ""):
        return {"project": "platform", "environment": "", "service": service}
    return {"project": "", "environment": "", "service": service}


def parse_size(text: str) -> Optional[float]:
    match = SIZE.match(text or "")
    if not match or match.group(2) not in UNITS:
        return None
    return float(match.group(1)) * UNITS[match.group(2)]


def parse_percent(text: str) -> Optional[float]:
    try:
        return float((text or "").strip().rstrip("%"))
    except ValueError:
        return None


def parse_stamp(stamp: str) -> Optional[float]:
    match = STAMP.match(stamp or "")
    if not match:
        return None
    return (
        datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def parse_time(text: str) -> Optional[float]:
    try:
        return datetime.fromisoformat((text or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Samples:
    def __init__(self) -> None:
        self.families: dict = {}

    def add(
        self, name: str, kind: str, help_text: str, labels: dict, value: float
    ) -> None:
        family = self.families.setdefault(
            name, {"kind": kind, "help": help_text, "rows": []}
        )
        family["rows"].append((labels, value))

    def render(self) -> str:
        lines = []
        for name in sorted(self.families):
            family = self.families[name]
            lines.append(f"# HELP {name} {family['help']}")
            lines.append(f"# TYPE {name} {family['kind']}")
            for labels, value in family["rows"]:
                body = ",".join(
                    f'{key}="{str(val).translate(LABEL_ESCAPE)}"'
                    for key, val in sorted(labels.items())
                    if val not in (None, "")
                )
                lines.append(
                    f"{name}{{{body}}} {number(value)}"
                    if body
                    else f"{name} {number(value)}"
                )
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- containers


def number(value: float) -> str:
    """A sample value in full.

    Six significant digits are plenty for a percentage and ruin a Unix
    timestamp: 1790943672 came out as 1.79094e+09, up to 2.7 hours off, and
    every rule that subtracts it from the clock was wrong by that much.
    """
    value = float(value)
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def docker_lines(docker: Path, arguments: list, runner=subprocess.run) -> list:
    try:
        result = runner(
            [str(docker), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [
        line
        for line in result.stdout.decode("utf-8", "replace").splitlines()
        if line.strip()
    ]


def container_samples(samples: Samples, docker: Path, runner=subprocess.run) -> None:
    listing = docker_lines(
        docker,
        [
            "ps",
            "--all",
            "--no-trunc",
            "--format",
            '{{.ID}}|{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.service"}}|{{.State}}',
        ],
        runner,
    )
    known = {}
    for line in listing:
        parts = line.split("|")
        if len(parts) != 5:
            continue
        identifier, name, project, service, state = parts
        labels = {**scope_labels(project, service), "container": name}
        known[identifier] = labels
        samples.add(
            "platform_container_up",
            "gauge",
            "1 when the container is running.",
            labels,
            1.0 if state == "running" else 0.0,
        )

    if known:
        for line in docker_lines(
            docker, ["inspect", "--format", "{{.Id}}|{{.RestartCount}}", *known], runner
        ):
            identifier, _, count = line.partition("|")
            if identifier in known and count.isdigit():
                samples.add(
                    "platform_container_restarts_total",
                    "counter",
                    "Restarts by the runtime since creation.",
                    known[identifier],
                    float(count),
                )

    for line in docker_lines(
        docker, ["stats", "--no-stream", "--no-trunc", "--format", "{{json .}}"], runner
    ):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        labels = known.get(row.get("ID"))
        if labels is None:
            continue
        cpu = parse_percent(row.get("CPUPerc", ""))
        if cpu is not None:
            samples.add(
                "platform_container_cpu_percent",
                "gauge",
                "CPU use, percent of one core.",
                labels,
                cpu,
            )
        used, _, limit = (row.get("MemUsage") or "").partition("/")
        for name, text, help_text in (
            ("platform_container_memory_bytes", used, "Memory in use."),
            (
                "platform_container_memory_limit_bytes",
                limit,
                "Memory limit; the host's total when none is set.",
            ),
        ):
            value = parse_size(text)
            if value is not None:
                samples.add(name, "gauge", help_text, labels, value)
        received, _, sent = (row.get("NetIO") or "").partition("/")
        for name, text, help_text in (
            (
                "platform_container_network_receive_bytes_total",
                received,
                "Bytes received.",
            ),
            ("platform_container_network_transmit_bytes_total", sent, "Bytes sent."),
        ):
            value = parse_size(text)
            if value is not None:
                samples.add(name, "counter", help_text, labels, value)


# ------------------------------------------------------------------ releases


def release_samples(
    samples: Samples, projects_root: Path, releases_root: Path, backups_root: Path
) -> None:
    try:
        scopes = list_project_scopes(projects_root)
    except (ReleaseLedgerError, OSError):
        samples.add(
            "platform_ledger_readable",
            "gauge",
            "0 when the release ledger could not be walked.",
            {},
            0.0,
        )
        return
    samples.add(
        "platform_ledger_readable",
        "gauge",
        "0 when the release ledger could not be walked.",
        {},
        1.0,
    )

    for project, environment in scopes:
        scope = {"project": project, "environment": environment}
        try:
            records = list_release_records(projects_root, project, environment)
        except (ReleaseLedgerError, OSError):
            # One scope's damaged record must not hide behind a healthy walk.
            samples.add(
                "platform_ledger_readable",
                "gauge",
                "0 when the release ledger could not be walked.",
                scope,
                0.0,
            )
            continue
        samples.add(
            "platform_ledger_readable",
            "gauge",
            "0 when the release ledger could not be walked.",
            scope,
            1.0,
        )
        samples.add(
            "platform_release_records",
            "gauge",
            "Ledger records for this scope.",
            scope,
            float(len(records)),
        )
        samples.add(
            "platform_retired",
            "gauge",
            "1 when the application is retired on this host.",
            scope,
            1.0 if is_retired(projects_root, project, environment) else 0.0,
        )
        if records:
            latest = records[-1]
            samples.add(
                "platform_release_latest_info",
                "gauge",
                "The latest attempt: its tag and how it ended.",
                {**scope, "release": latest["release_tag"], "status": latest["status"]},
                1.0,
            )
        current = find_latest_deployed_release(records)
        if current is not None:
            samples.add(
                "platform_release_info",
                "gauge",
                "The release serving traffic.",
                {**scope, "release": current["release_tag"]},
                1.0,
            )
            deployed = parse_time(current.get("updated_at", ""))
            if deployed is not None:
                samples.add(
                    "platform_release_deployed_timestamp_seconds",
                    "gauge",
                    "When the serving release was deployed.",
                    scope,
                    deployed,
                )
            try:
                manifest = load_staged_manifest(
                    resolve_release_bundle(current, releases_root)
                )
                samples.add(
                    "platform_backup_scheduled",
                    "gauge",
                    "1 when the serving release asks for scheduled dumps.",
                    scope,
                    1.0 if backups_are_scheduled(manifest) else 0.0,
                )
                for domain in manifest["domains"]:
                    samples.add(
                        "platform_domain_info",
                        "gauge",
                        "A domain the serving release answers on.",
                        {
                            **scope,
                            "domain": domain["host"],
                            "service": domain_service(manifest, domain),
                        },
                        1.0,
                    )
            except (
                ReleaseLedgerError,
                ComposeRuntimeError,
                OSError,
                KeyError,
                TypeError,
            ):
                pass

        directory = backups_root / project / environment
        try:
            stamps = list_backups(directory)
        except OSError:
            stamps = []
        samples.add(
            "platform_backup_count",
            "gauge",
            "Local dumps kept.",
            scope,
            float(len(stamps)),
        )
        newest = parse_stamp(stamps[-1]) if stamps else None
        if newest is not None:
            samples.add(
                "platform_backup_latest_timestamp_seconds",
                "gauge",
                "When the newest dump was taken.",
                scope,
                newest,
            )
        try:
            verification = last_verification(directory)
        except OSError:
            verification = None
        proven = (
            parse_stamp((verification or {}).get("stamp", ""))
            if (verification or {}).get("outcome") == VERIFICATION_SUCCEEDED
            else None
        )
        samples.add(
            "platform_backup_restore_proven",
            "gauge",
            "1 when a restore of some dump has been proven.",
            scope,
            1.0 if proven is not None else 0.0,
        )
        if proven is not None:
            samples.add(
                "platform_backup_verified_dump_timestamp_seconds",
                "gauge",
                "The dump whose restore was last proven.",
                scope,
                proven,
            )


def write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".platform.prom.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def collect(
    docker: Path = DEFAULT_DOCKER,
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    releases_root: Path = DEFAULT_RELEASES_ROOT,
    backups_root: Path = DEFAULT_BACKUPS_ROOT,
    runner=subprocess.run,
    now: Optional[float] = None,
) -> str:
    samples = Samples()
    container_samples(samples, docker, runner)
    release_samples(samples, projects_root, releases_root, backups_root)
    stamp = datetime.now(timezone.utc).timestamp() if now is None else now
    samples.add(
        "platform_metrics_export_timestamp_seconds",
        "gauge",
        "When this file was written.",
        {},
        stamp,
    )
    return samples.render()


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write platform metrics for a collector to read."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--docker", type=Path, default=DEFAULT_DOCKER)
    parser.add_argument("--projects-root", type=Path, default=DEFAULT_PROJECTS_ROOT)
    parser.add_argument("--releases-root", type=Path, default=DEFAULT_RELEASES_ROOT)
    parser.add_argument("--backups-root", type=Path, default=DEFAULT_BACKUPS_ROOT)
    arguments = parser.parse_args(argv)
    try:
        write_atomically(
            arguments.output,
            collect(
                arguments.docker,
                arguments.projects_root,
                arguments.releases_root,
                arguments.backups_root,
            ),
        )
    except OSError as error:
        print(f"metrics export error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
