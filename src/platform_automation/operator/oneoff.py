"""A one-off command in the serving release, from the workstation.

The console does not run the command itself: it asks the host's ``platform
exec`` over SSH with a terminal attached, so the operator sees the output as
it comes and the host applies its own rules — the release's image, its
secrets, a container of its own.
"""

import shlex
from typing import Any, Optional

from ..validate_manifest import load_yaml, resolve_compose_path


class OneOffError(RuntimeError):
    pass


def split_command(text: str) -> list:
    """What the operator typed, as the argument list the host gets.

    Shell words, nothing more: no expansion, no pipes, no redirects — those
    would run on the host, not in the container, and the host refuses them
    by never seeing a shell.
    """
    try:
        parts = shlex.split(text)
    except ValueError as error:
        raise OneOffError(f"cannot read the command: {error}") from error
    if not parts:
        raise OneOffError("the command is empty")
    for part in parts:
        if part in ("|", "||", "&&", ";", ">", ">>", "<"):
            raise OneOffError(
                f"{part!r} would be a shell operator; the command runs without a shell"
            )
    return parts


def remote_command(project: str, environment: str, service: str, argv: list) -> str:
    """The line the host runs; shown before it is, and reproducible by hand."""
    return shlex.join(
        [
            "sudo",
            "-n",
            "platform",
            "exec",
            "--project",
            project,
            "--environment",
            environment,
            "--service",
            service,
            "--",
            *argv,
        ]
    )


def compose_services(root, manifest_path) -> list:
    """The services the manifest's Compose file declares, web first."""
    manifest: Any = load_yaml(root / manifest_path)
    compose = load_yaml(resolve_compose_path(root, manifest["compose_file"]))
    services = (
        list((compose.get("services") or {}).keys())
        if isinstance(compose, dict)
        else []
    )
    web: Optional[str] = (manifest.get("service") or {}).get("web")
    if web in services:
        services.remove(web)
        services.insert(0, web)
    return services
