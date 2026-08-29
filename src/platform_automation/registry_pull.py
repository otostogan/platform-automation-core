#!/usr/bin/env python3

import os
import re
import subprocess
import tempfile
from pathlib import Path


DEFAULT_DOCKER_EXECUTABLE = Path("/usr/bin/docker")
DEFAULT_REGISTRY_RUNTIME_ROOT = Path("/run/platform/registry")

REGISTRY_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_REGISTRY_TOKEN_BYTES = 64 * 1024


class RegistryPullError(ValueError):
    pass


def read_registry_token(stream) -> bytes:
    token = stream.read(MAX_REGISTRY_TOKEN_BYTES + 1)

    if len(token) > MAX_REGISTRY_TOKEN_BYTES:
        raise RegistryPullError("registry token exceeds the size limit")

    token = token.strip()

    if not token:
        raise RegistryPullError("registry token from stdin is empty")

    if b"\x00" in token or b"\n" in token or b"\r" in token:
        raise RegistryPullError("registry token has an invalid format")

    return token


def resolve_registry(image: str) -> str:
    repository = image.split("@", 1)[0]
    registry, separator, remainder = repository.partition("/")

    if not separator or not remainder or "." not in registry:
        raise RegistryPullError("image repository has no explicit registry")

    return registry


def create_private_registry_root(runtime_root: Path) -> Path:
    if runtime_root.is_symlink():
        raise RegistryPullError("registry runtime root is unsafe")

    runtime_root.mkdir(
        mode=0o700,
        parents=True,
        exist_ok=True,
    )

    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise RegistryPullError("registry runtime root is unsafe")

    runtime_root.chmod(0o700)
    return runtime_root.resolve()


def run_docker_command(
    command: list[str],
    runner,
    action: str,
    stdin: bytes = None,
) -> None:
    try:
        result = runner(
            command,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RegistryPullError(
            f"Docker registry {action} could not be executed"
        ) from error

    if result.returncode != 0:
        raise RegistryPullError(f"Docker registry {action} failed")


def image_is_present(
    image: str,
    docker_executable: Path,
    runner,
) -> bool:
    """Report whether the daemon already holds this exact digest.

    A reference carrying a sha256 digest names one immutable manifest, and
    Docker only records such a reference for content it actually pulled, so a
    local hit is as trustworthy as a fresh pull.
    """
    try:
        result = runner(
            [
                str(docker_executable),
                "image",
                "inspect",
                image,
                "--format",
                "{{.Id}}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    return result.returncode == 0


def pull_immutable_image(
    image: str,
    registry_username: str = None,
    registry_token: bytes = None,
    runtime_root: Path = DEFAULT_REGISTRY_RUNTIME_ROOT,
    docker_executable: Path = DEFAULT_DOCKER_EXECUTABLE,
    runner=subprocess.run,
) -> None:
    if (registry_username is None) != (registry_token is None):
        raise RegistryPullError("registry username and token must be provided together")

    # Rollback and reboot recovery must work when the registry is unreachable,
    # and retention keeps the images they need. Contacting the registry for a
    # digest already on the host would make that impossible.
    if image_is_present(image, docker_executable, runner):
        return

    if registry_username is not None and not REGISTRY_USERNAME_PATTERN.fullmatch(
        registry_username
    ):
        raise RegistryPullError("registry username has an invalid format")

    registry = resolve_registry(image)
    registry_root = create_private_registry_root(runtime_root)

    with tempfile.TemporaryDirectory(
        prefix=".docker-config-",
        dir=registry_root,
    ) as temporary_name:
        docker_config = Path(temporary_name)
        docker_config.chmod(0o700)
        base_command = [
            str(docker_executable),
            "--config",
            str(docker_config),
        ]

        if registry_token is not None:
            run_docker_command(
                base_command
                + [
                    "login",
                    registry,
                    "--username",
                    registry_username,
                    "--password-stdin",
                ],
                runner=runner,
                action="login",
                stdin=registry_token,
            )

        run_docker_command(
            base_command + ["pull", image],
            runner=runner,
            action="image pull",
        )
