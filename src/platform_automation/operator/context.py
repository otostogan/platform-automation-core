"""Work out where the operator is standing before offering anything.

The same ``platform`` binary is installed on hosts and on workstations. What it
should offer depends entirely on the directory it is started from, so the
console asks that question first and answers it from files on disk, never from
a flag someone has to remember.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

HOST_MARKER = Path("/var/lib/platform/projects")
INFRA_INVENTORY = Path("inventory/hosts.yml")
INFRA_REQUIREMENTS = Path("requirements.yml")
COLLECTION_ARTIFACT = "otostogan-platform"
APP_MANIFEST_GLOB = "deploy/platform*.yml"
APP_API_VERSION = "platform/v1"
APP_DEPLOY_WORKFLOW = Path(".github/workflows/deploy.yml")

TARGET_HOST_PATTERN = re.compile(r"^\s*target_host:\s*(\S+)\s*$", re.M)
TAILSCALE_TAG_PATTERN = re.compile(r"^\s*tailscale_tag:\s*(\S+)\s*$", re.M)
CORE_PIN_PATTERN = re.compile(r"reusable-deploy\.yml@(v\d+\.\d+\.\d+)")
COLLECTION_PIN_PATTERN = re.compile(rf"{COLLECTION_ARTIFACT}-(\d+\.\d+\.\d+)\.tar\.gz")


@dataclass(frozen=True)
class Host:
    name: str
    address: Optional[str]
    user: Optional[str]
    key_file: Optional[str] = None


@dataclass(frozen=True)
class Environment:
    project: str
    environment: str
    manifest: Path


@dataclass(frozen=True)
class Context:
    """What the console knows about the place it was started in."""

    kind: str  # "host" | "infra" | "app" | "nowhere"
    root: Optional[Path] = None
    hosts: tuple = field(default_factory=tuple)
    environments: tuple = field(default_factory=tuple)
    target_host: Optional[str] = None
    tailscale_tag: Optional[str] = None
    core_pin: Optional[str] = None


def load_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def is_infra_root(directory: Path) -> bool:
    requirements = directory / INFRA_REQUIREMENTS

    if not (directory / INFRA_INVENTORY).is_file() or not requirements.is_file():
        return False

    try:
        return COLLECTION_ARTIFACT in requirements.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def application_manifests(directory: Path) -> list[Path]:
    """Manifests that declare the contract, in a stable order."""
    found = []

    for path in sorted(directory.glob(APP_MANIFEST_GLOB)):
        document = load_yaml(path)

        if (
            isinstance(document, dict)
            and document.get("api_version") == APP_API_VERSION
        ):
            found.append(path)

    return found


def is_app_root(directory: Path) -> bool:
    return bool(application_manifests(directory))


def find_root(start: Path, predicate) -> Optional[Path]:
    """Walk upwards the way git does, stopping at the first directory that fits."""
    current = start.resolve()

    for candidate in (current, *current.parents):
        if predicate(candidate):
            return candidate

    return None


def read_hosts(root: Path) -> tuple:
    document = load_yaml(root / INFRA_INVENTORY)

    try:
        entries = document["all"]["children"]["platform_hosts"]["hosts"]
    except (KeyError, TypeError):
        return ()

    if not isinstance(entries, dict):
        return ()

    hosts = []

    for name, values in entries.items():
        values = values if isinstance(values, dict) else {}
        hosts.append(
            Host(
                name=str(name),
                address=_optional_str(values.get("ansible_host")),
                user=_optional_str(values.get("ansible_user")),
                key_file=_optional_str(values.get("ansible_ssh_private_key_file")),
            )
        )

    return tuple(hosts)


def read_collection_pin(root: Path) -> Optional[str]:
    try:
        text = (root / INFRA_REQUIREMENTS).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    match = COLLECTION_PIN_PATTERN.search(text)
    return f"v{match.group(1)}" if match else None


def read_environments(root: Path) -> tuple:
    environments = []

    for path in application_manifests(root):
        document = load_yaml(path)
        project = _optional_str(document.get("project"))
        environment = _optional_str(document.get("environment"))

        if project and environment:
            environments.append(
                Environment(
                    project=project,
                    environment=environment,
                    manifest=path.relative_to(root),
                )
            )

    return tuple(environments)


def read_deploy_workflow(root: Path) -> tuple:
    """Target host, tailnet tag and core pin, straight from the workflow file.

    The workflow is the one place an application says where it goes; reading
    it means the console never keeps a second copy that could drift.
    """
    try:
        text = (root / APP_DEPLOY_WORKFLOW).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, None, None

    host = TARGET_HOST_PATTERN.search(text)
    tag = TAILSCALE_TAG_PATTERN.search(text)
    pin = CORE_PIN_PATTERN.search(text)

    return (
        host.group(1) if host else None,
        tag.group(1) if tag else None,
        pin.group(1) if pin else None,
    )


def detect(start: Optional[Path] = None, host_marker: Path = HOST_MARKER) -> Context:
    """Answer "where am I" from the filesystem alone."""
    if host_marker.is_dir():
        return Context(kind="host", root=host_marker)

    start = Path.cwd() if start is None else start

    infra_root = find_root(start, is_infra_root)

    if infra_root is not None:
        return Context(
            kind="infra",
            root=infra_root,
            hosts=read_hosts(infra_root),
            core_pin=read_collection_pin(infra_root),
        )

    app_root = find_root(start, is_app_root)

    if app_root is not None:
        target_host, tailscale_tag, core_pin = read_deploy_workflow(app_root)
        return Context(
            kind="app",
            root=app_root,
            environments=read_environments(app_root),
            target_host=target_host,
            tailscale_tag=tailscale_tag,
            core_pin=core_pin,
        )

    return Context(kind="nowhere", root=start.resolve())


def _optional_str(value: Any) -> Optional[str]:
    return str(value) if isinstance(value, (str, int)) and str(value) else None
