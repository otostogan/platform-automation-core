"""A deployment without GitHub, from the workstation — the break-glass path.

The normal path is the application's Deploy workflow: it builds the image
from a reviewed commit, enters the tailnet as ``deploy`` with rights to
three commands, and leaves a run anyone can read later. This module does the
same three steps by hand — image, bundle, ``platform deploy`` — for the one
case the normal path cannot serve: GitHub is down and the fix cannot wait.

Nothing here is a shortcut. The working tree must be clean and on a release
tag, the registry login is the operator's own and is never printed, the
release is labelled ``manual`` so the registry of releases says who did it,
and the console refuses the path while GitHub answers unless the operator
spells out that this is a break-glass action.
"""

import base64
import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REGISTRY = "ghcr.io"
DEFAULT_PLATFORMS = "linux/amd64"
BREAK_GLASS = "break-glass"
TAG_PATTERN = re.compile(r"^v\d+\.\d+\.\d+$")
PLATFORMS_PATTERN = re.compile(r"^\s*PLATFORMS:\s*(\S+)\s*$", re.M)
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class ManualError(RuntimeError):
    pass


@dataclass(frozen=True)
class Workspace:
    """What the repository is at, as the deployment will record it."""

    commit: str
    tag: Optional[str]
    dirty: bool


def _run(command: list, root: Optional[Path], runner, timeout: int = 30):
    return runner(
        command,
        cwd=None if root is None else str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def workspace(root: Path, runner=subprocess.run) -> Workspace:
    """Commit, exact release tag and cleanliness of the checkout.

    A manual deployment has no run log; the tag and the commit it records
    are the only link back to what was deployed, so both must be exact.
    """
    head = _run(["git", "rev-parse", "HEAD"], root, runner)
    if head.returncode != 0:
        raise ManualError("not a git repository, or no commit yet")
    commit = _text(head.stdout).strip()
    status = _run(["git", "status", "--porcelain"], root, runner)
    dirty = bool(_text(status.stdout).strip())
    described = _run(
        ["git", "describe", "--exact-match", "--tags", "HEAD"], root, runner
    )
    tag = _text(described.stdout).strip() if described.returncode == 0 else None
    if tag and not TAG_PATTERN.fullmatch(tag):
        tag = None
    return Workspace(commit=commit, tag=tag, dirty=dirty)


def require_deployable(state: Workspace) -> None:
    if state.dirty:
        raise ManualError(
            "the working tree has uncommitted changes; a manual deployment "
            "records a commit, and it must be exactly what the image is built from"
        )
    if state.tag is None:
        raise ManualError(
            "HEAD is not at a release tag (vX.Y.Z); tag the commit first — "
            "the post-commit hook does it on a version bump"
        )


def build_platforms(root: Path) -> str:
    """The processor list the Build workflow uses, so the image matches the host."""
    workflow = root / ".github" / "workflows" / "build.yml"
    try:
        match = PLATFORMS_PATTERN.search(workflow.read_text(encoding="utf-8"))
    except OSError:
        return DEFAULT_PLATFORMS
    return match.group(1) if match else DEFAULT_PLATFORMS


RECIPIENTS_PATTERN = re.compile(r"^\s*minimum_age_recipients:\s*(\d+)\s*$", re.M)


def bundle_minimum_recipients(root: Path) -> int:
    """The recipient count the Deploy workflow demands of the bundle, so the hand demands the same."""
    workflow = root / ".github" / "workflows" / "deploy.yml"
    try:
        match = RECIPIENTS_PATTERN.search(workflow.read_text(encoding="utf-8"))
    except OSError:
        return 1
    return int(match.group(1)) if match else 1


def image_repository(manifest: dict) -> str:
    try:
        repository = str(manifest["image"]["repository"])
    except (KeyError, TypeError) as error:
        raise ManualError("the manifest names no image.repository") from error
    if not repository.startswith(f"{REGISTRY}/"):
        raise ManualError(
            f"the image repository is not on {REGISTRY}; the manual path knows that registry only"
        )
    return repository


def build_command(
    repository: str, state: Workspace, platforms: str, metadata: Path
) -> list:
    """``docker buildx build`` exactly as the Build workflow tags it: by commit and by tag."""
    return [
        "docker",
        "buildx",
        "build",
        "--platform",
        platforms,
        "--push",
        "--provenance=false",
        "--tag",
        f"{repository}:{state.commit}",
        "--tag",
        f"{repository}:{state.tag}",
        "--metadata-file",
        str(metadata),
        ".",
    ]


def pushed_digest(metadata: Path) -> str:
    """The immutable reference the push produced, from buildx's metadata file."""
    try:
        document = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ManualError(f"cannot read the build metadata: {error}") from error
    digest = str(document.get("containerimage.digest") or "")
    if not DIGEST_PATTERN.fullmatch(digest):
        raise ManualError("the build metadata carries no image digest")
    return digest


def registry_credentials(
    docker_config: Path, registry: str = REGISTRY, runner=subprocess.run
) -> tuple:
    """The operator's own login for the registry, read the way docker reads it.

    The pair is handed to the host on standard input and never shown. If
    there is no login, the operator is told to ``docker login`` — the
    console does not ask for a token and does not keep one.
    """
    try:
        config = json.loads(docker_config.read_text(encoding="utf-8"))
    except OSError as error:
        raise ManualError(
            f"no docker configuration at {docker_config}; run: docker login {registry}"
        ) from error
    except ValueError as error:
        raise ManualError(f"cannot read {docker_config}: {error}") from error

    helpers = config.get("credHelpers") or {}
    helper = helpers.get(registry) or config.get("credsStore")
    if helper:
        # docker stores the login under the address as it was typed:
        # ``docker login ghcr.io`` keeps ``ghcr.io``; an older login may sit
        # under the https form. Ask for both before giving up.
        found = None
        for key in (registry, f"https://{registry}"):
            result = runner(
                [f"docker-credential-{helper}", "get"],
                input=f"{key}\n".encode(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
            if result.returncode == 0:
                try:
                    found = json.loads(_text(result.stdout))
                except ValueError as error:
                    raise ManualError(
                        f"docker-credential-{helper} answered with something that is not a login"
                    ) from error
                break
        if found is None:
            raise ManualError(
                f"docker-credential-{helper} holds no login for {registry}; run: docker login {registry}"
            )
        try:
            username, secret = str(found["Username"]), str(found["Secret"])
        except (KeyError, TypeError) as error:
            raise ManualError(
                f"docker-credential-{helper} answered with something that is not a login"
            ) from error
        if username and secret:
            return username, secret
        raise ManualError(
            f"the login for {registry} is empty; run: docker login {registry}"
        )

    auths = config.get("auths") or {}
    entry = auths.get(registry) or auths.get(f"https://{registry}") or {}
    encoded = entry.get("auth") if isinstance(entry, dict) else None
    if not encoded:
        raise ManualError(f"not logged in to {registry}; run: docker login {registry}")
    try:
        username, secret = base64.b64decode(encoded).decode("utf-8").split(":", 1)
    except (ValueError, UnicodeDecodeError) as error:
        raise ManualError(f"the stored login for {registry} is unreadable") from error
    return username, secret


def manual_label(environment: str, tag: str, now: Optional[datetime] = None) -> str:
    """A release label that says, in the registry of releases, that a hand did this."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%MZ")
    return f"{environment}-{tag}-manual-{stamp}"


def remote_bundle_path(project: str, environment: str, label: str) -> str:
    return f"/tmp/{project}-{environment}-{label}.bundle.tar.gz"


def transfer_command(remote_path: str) -> str:
    """Receives the bundle on the host; the operator's file is its standard input."""
    return f"umask 077 && cat > {shlex.quote(remote_path)}"


def digest_command(remote_path: str) -> str:
    return f"sha256sum {shlex.quote(remote_path)} | cut -d ' ' -f 1"


def remove_command(remote_path: str) -> str:
    return f"rm -f {shlex.quote(remote_path)}"


def deploy_command(
    project: str,
    environment: str,
    remote_path: str,
    image: str,
    label: str,
    registry_username: str,
) -> str:
    """The host's deploy line — the token follows on standard input, never here."""
    return shlex.join(
        [
            "sudo",
            "-n",
            "platform",
            "deploy",
            "--project",
            project,
            "--environment",
            environment,
            "--bundle",
            remote_path,
            "--image",
            image,
            "--release-tag",
            label,
            "--registry-username",
            registry_username,
            "--registry-token-stdin",
            "--json",
        ]
    )


def github_reachable(root: Path, runner=subprocess.run) -> bool:
    """Whether the normal path is open: ``gh`` is logged in and GitHub answers."""
    try:
        result = _run(["gh", "auth", "status"], root, runner, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
