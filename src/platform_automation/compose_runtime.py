#!/usr/bin/env python3

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

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
    tls_hosts = [domain["host"] for domain in manifest["domains"] if domain["tls"]]

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
            "PLATFORM_VIRTUAL_HOSTS": ",".join(
                domain["host"] for domain in manifest["domains"]
            ),
        }
    )

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
