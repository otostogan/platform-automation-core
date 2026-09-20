#!/usr/bin/env python3

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .domains import helper_host_variables, web_domains
from .validate_manifest import load_yaml
from .verify_bundle import METADATA_PATH


class ComposeRuntimeError(RuntimeError):
    pass


def resolve_staged_file(
    staged_bundle_path: Path,
    relative_path: str,
    label: str,
) -> Path:
    if staged_bundle_path.is_symlink() or not staged_bundle_path.is_dir():
        raise ComposeRuntimeError(
            f"staged bundle is not a safe directory: {staged_bundle_path}"
        )

    root = staged_bundle_path.resolve()
    candidate = root.joinpath(*relative_path.split("/"))

    if candidate.is_symlink():
        raise ComposeRuntimeError(f"{label} cannot be a symbolic link")

    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as error:
        raise ComposeRuntimeError(
            f"{label} is missing or escapes staged bundle"
        ) from error

    if not resolved.is_file():
        raise ComposeRuntimeError(f"{label} is not a regular file")

    return resolved


def build_compose_environment(
    manifest: dict[str, Any],
    image: str,
    runtime_secrets_path: Path,
    base_environment: dict[str, str] = None,
) -> dict[str, str]:
    if runtime_secrets_path.is_symlink() or not runtime_secrets_path.is_file():
        raise ComposeRuntimeError(
            f"runtime secrets file is missing or unsafe: {runtime_secrets_path}"
        )

    environment = dict(os.environ if base_environment is None else base_environment)
    own = web_domains(manifest)
    tls_hosts = [domain["host"] for domain in own if domain["tls"]]

    database_network = (
        f"platform-db-{manifest['project']}-{manifest['environment']}"
        if manifest["database"]["mode"] == "docker"
        else ""
    )

    environment.update(
        {
            "PLATFORM_COMPOSE_PROJECT_NAME": (
                f"{manifest['project']}-{manifest['environment']}"
            ),
            # Empty for an external database, so a compose file that
            # wrongly references it fails loudly at startup.
            "PLATFORM_DB_NETWORK": database_network,
            "PLATFORM_IMAGE": image,
            "PLATFORM_INTERNAL_PORT": str(manifest["service"]["internal_port"]),
            "PLATFORM_RUNTIME_ENV_FILE": str(runtime_secrets_path.resolve()),
            "PLATFORM_TLS_HOSTS": ",".join(tls_hosts),
            "PLATFORM_VIRTUAL_HOSTS": ",".join(domain["host"] for domain in own),
        }
    )
    # A helper's domains reach it through its own variables, so the Compose
    # file never carries a hostname as a literal.
    environment.update(helper_host_variables(manifest))

    return environment


def compose_context(
    manifest: dict[str, Any],
    staged_bundle_path: Path,
    image: str,
    runtime_secrets_path: Path,
    docker_executable: Path,
) -> tuple[list[str], dict[str, str]]:
    compose_file = resolve_staged_file(
        staged_bundle_path,
        manifest["compose_file"],
        "Compose file",
    )
    environment = build_compose_environment(
        manifest,
        image,
        runtime_secrets_path,
    )
    command = [
        str(docker_executable),
        "compose",
        "--project-name",
        environment["PLATFORM_COMPOSE_PROJECT_NAME"],
        "--file",
        str(compose_file),
    ]

    return command, environment


def run_compose_command(
    command: list[str],
    environment: dict[str, str],
    action: str,
    runner=subprocess.run,
) -> None:
    try:
        result = runner(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as error:
        raise ComposeRuntimeError(f"{action} could not start") from error

    if result.returncode != 0:
        raise ComposeRuntimeError(f"{action} failed with exit code {result.returncode}")


def parse_compose_ps(output: str) -> list[dict[str, Any]]:
    output = output.strip()

    if not output:
        return []

    try:
        document = json.loads(output)
    except json.JSONDecodeError:
        try:
            document = [json.loads(line) for line in output.splitlines() if line]
        except json.JSONDecodeError as error:
            raise ComposeRuntimeError(
                "Docker Compose status output is invalid"
            ) from error

    if isinstance(document, dict):
        document = [document]

    if not isinstance(document, list) or not all(
        isinstance(item, dict) for item in document
    ):
        raise ComposeRuntimeError("Docker Compose status output is invalid")

    return document


def inspect_release_services(
    command: list[str],
    environment: dict[str, str],
    expected_services: set[str],
    runner=subprocess.run,
) -> None:
    try:
        result = runner(
            [*command, "ps", "--all", "--format", "json"],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
    except OSError as error:
        raise ComposeRuntimeError(
            "Docker Compose service status could not be inspected"
        ) from error

    if result.returncode != 0:
        raise ComposeRuntimeError("Docker Compose service status inspection failed")

    services = parse_compose_ps(result.stdout)
    discovered = {
        service.get("Service"): service
        for service in services
        if isinstance(service.get("Service"), str)
    }

    if set(discovered) != expected_services:
        raise ComposeRuntimeError("Docker Compose service set is incomplete")

    for service_name, service in discovered.items():
        state = str(service.get("State", "")).lower()
        health = str(service.get("Health", "")).lower()

        if state != "running":
            raise ComposeRuntimeError(
                f"Docker Compose service is not running: {service_name}"
            )

        if health and health != "healthy":
            raise ComposeRuntimeError(
                f"Docker Compose service is unhealthy: {service_name}"
            )


def validate_release_compose(
    manifest: dict[str, Any],
    staged_bundle_path: Path,
    image: str,
    runtime_secrets_path: Path,
    docker_executable: Path,
    runner=subprocess.run,
) -> None:
    command, environment = compose_context(
        manifest,
        staged_bundle_path,
        image,
        runtime_secrets_path,
        docker_executable,
    )
    run_compose_command(
        [*command, "config", "--quiet"],
        environment,
        "Docker Compose validation",
        runner,
    )


def run_release_migration(
    manifest: dict[str, Any],
    staged_bundle_path: Path,
    image: str,
    runtime_secrets_path: Path,
    docker_executable: Path,
    runner=subprocess.run,
) -> None:
    migration_command = manifest["deployment"].get("migration_command")

    if not migration_command:
        return

    migration_service = manifest["deployment"]["migration_service"]
    command, environment = compose_context(
        manifest,
        staged_bundle_path,
        image,
        runtime_secrets_path,
        docker_executable,
    )
    run_compose_command(
        [
            *command,
            "run",
            "--rm",
            "--entrypoint",
            "",
            migration_service,
            *migration_command,
        ],
        environment,
        "application migration",
        runner,
    )


def start_release(
    manifest: dict[str, Any],
    staged_bundle_path: Path,
    image: str,
    runtime_secrets_path: Path,
    docker_executable: Path,
    runner=subprocess.run,
    sleeper=time.sleep,
    http_get=None,
    clock=time.monotonic,
) -> None:
    timeout = manifest["service"]["healthcheck"]["timeout_seconds"]
    command, environment = compose_context(
        manifest,
        staged_bundle_path,
        image,
        runtime_secrets_path,
        docker_executable,
    )
    run_compose_command(
        [
            *command,
            "up",
            "--detach",
            "--remove-orphans",
            "--wait",
            "--wait-timeout",
            str(timeout),
        ],
        environment,
        "application healthcheck",
        runner,
    )

    compose_file = resolve_staged_file(
        staged_bundle_path,
        manifest["compose_file"],
        "Compose file",
    )
    compose = load_yaml(compose_file)
    expected_services = set(compose["services"])

    for _ in range(3):
        sleeper(1)
        inspect_release_services(
            command,
            environment,
            expected_services,
            runner,
        )

    probe_release_http(
        manifest,
        command,
        environment,
        runner=runner,
        sleeper=sleeper,
        http_get=http_get,
        clock=clock,
    )


EDGE_NETWORK = "platform-edge"
PROBE_INTERVAL_SECONDS = 2
PROBE_REQUEST_TIMEOUT_SECONDS = 5


def default_http_get(url: str, host: str, timeout: float) -> int:
    """HTTP status of a GET; a connection failure is reported as 0."""
    request = urllib.request.Request(
        url, headers={"Host": host, "User-Agent": "platform-healthcheck"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def service_container_address(
    command: list[str],
    environment: dict[str, str],
    service: str,
    runner=subprocess.run,
) -> str:
    """The web service's address on the edge network — where nginx will send traffic."""
    try:
        listed = runner(
            [*command, "ps", "-q", service],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
    except OSError as error:
        raise ComposeRuntimeError(
            "application container could not be listed"
        ) from error
    container = (listed.stdout or "").strip().splitlines()
    if listed.returncode != 0 or not container:
        raise ComposeRuntimeError(f"application service has no container: {service}")

    try:
        inspected = runner(
            [
                command[0],
                "inspect",
                "--format",
                "{{json .NetworkSettings.Networks}}",
                container[0],
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
    except OSError as error:
        raise ComposeRuntimeError(
            "application container could not be inspected"
        ) from error
    try:
        networks = json.loads(inspected.stdout or "{}")
    except ValueError:
        networks = {}
    if inspected.returncode != 0 or not isinstance(networks, dict):
        raise ComposeRuntimeError("application container networks could not be read")

    addresses = {
        name: str(values.get("IPAddress") or "")
        for name, values in networks.items()
        if isinstance(values, dict) and values.get("IPAddress")
    }
    if not addresses:
        raise ComposeRuntimeError("application container has no network address")
    return addresses.get(EDGE_NETWORK) or next(iter(addresses.values()))


def probe_release_http(
    manifest: dict[str, Any],
    command: list[str],
    environment: dict[str, str],
    runner=subprocess.run,
    sleeper=time.sleep,
    http_get=None,
    clock=time.monotonic,
) -> None:
    """GET ``healthcheck.path`` on the new container until it answers, or the timeout.

    ``compose up --wait`` only proves the process is up. The manifest promises
    a path that answers, and that promise is checked here, on the host, before
    nginx is switched — so a release that starts but answers 404 is refused
    and rolled back like one that never started, instead of being recorded
    as deployed and discovered by the workflow's check afterwards.
    """
    service = manifest["service"]
    path = service["healthcheck"]["path"]
    timeout = float(service["healthcheck"]["timeout_seconds"])
    port = int(service["internal_port"])
    host = web_domains(manifest)[0]["host"]
    http_get = default_http_get if http_get is None else http_get

    address = service_container_address(command, environment, service["web"], runner)
    url = f"http://{address}:{port}{path}"
    deadline = clock() + timeout
    last = 0
    while True:
        last = http_get(url, host, PROBE_REQUEST_TIMEOUT_SECONDS)
        if 200 <= last < 400:  # what curl --fail accepts in the workflow's check
            return
        if clock() >= deadline:
            break
        sleeper(PROBE_INTERVAL_SECONDS)
    reason = f"HTTP {last}" if last else "no HTTP answer"
    raise ComposeRuntimeError(
        f"application healthcheck failed: GET {path} (Host: {host}) on the web"
        f" container answered {reason} within {int(timeout)}s; expected 2xx or 3xx"
    )


def stop_release(
    manifest: dict[str, Any],
    staged_bundle_path: Path,
    image: str,
    runtime_secrets_path: Path,
    docker_executable: Path,
    runner=subprocess.run,
) -> None:
    command, environment = compose_context(
        manifest,
        staged_bundle_path,
        image,
        runtime_secrets_path,
        docker_executable,
    )
    run_compose_command(
        [*command, "down", "--remove-orphans"],
        environment,
        "failed release cleanup",
        runner,
    )


def load_staged_manifest(staged_bundle_path: Path) -> dict[str, Any]:
    metadata_file = resolve_staged_file(
        staged_bundle_path,
        METADATA_PATH,
        "bundle metadata",
    )

    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
        manifest_relative = metadata["files"]["manifest"]["path"]
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ) as error:
        raise ComposeRuntimeError("staged bundle metadata is invalid") from error

    manifest_file = resolve_staged_file(
        staged_bundle_path,
        manifest_relative,
        "application manifest",
    )

    try:
        manifest = load_yaml(manifest_file)
    except (OSError, ValueError) as error:
        raise ComposeRuntimeError("staged application manifest is invalid") from error

    if not isinstance(manifest, dict):
        raise ComposeRuntimeError("staged application manifest is invalid")

    return manifest
