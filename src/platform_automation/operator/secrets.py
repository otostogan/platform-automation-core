"""Plain ``.env.<environment>`` in, the committed ciphertext out.

The operator edits an ordinary dotenv file and never runs sops: a pre-commit
hook calls ``platform secrets push``, which encrypts the file into
``deploy/secrets.<environment>.sops.yaml`` — the artifact the deployment has
always carried. Encryption needs only the public recipients in ``.sops.yaml``;
reading the ciphertext back (``pull``) is the one step that needs a private
key. The rules applied here are the host's own, so a refusal happens in the
operator's terminal rather than during a deployment.
"""

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..validate_manifest import load_yaml

NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PLATFORM_OWNED_URL = "DATABASE_URL"
HOOKS_PATH = ".githooks"


class SecretsError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncResult:
    environment: str
    written: bool
    dropped: tuple
    reason: str


def env_path(root: Path, environment: str) -> Path:
    return root / f".env.{environment}"


def ciphertext_path(root: Path, environment: str) -> Path:
    return root / "deploy" / f"secrets.{environment}.sops.yaml"


def manifest_path(root: Path, environment: str) -> Path:
    return root / "deploy" / f"platform.{environment}.yml"


def environments(root: Path) -> list:
    """Every environment that has a manifest, in a stable order."""
    found = []
    for path in sorted((root / "deploy").glob("platform.*.yml")):
        document = load_yaml(path) if path.is_file() else None
        if isinstance(document, dict) and document.get("api_version") == "platform/v1":
            found.append(str(document.get("environment")))
    return found


def parse_dotenv(text: str) -> dict:
    """``KEY=value`` lines, quotes stripped, names checked as the host checks them."""
    values = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise SecretsError(f"line {number} is not KEY=value: {raw.strip()[:40]}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not NAME_PATTERN.match(key):
            raise SecretsError(
                f"line {number}: {key!r} is not a valid environment variable name"
            )
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key] = value
    return values


def filter_for_manifest(values: dict, manifest: dict) -> tuple:
    """Apply the host's own rule about who provides the database address."""
    database = (
        manifest.get("database") if isinstance(manifest.get("database"), dict) else {}
    )
    mode = database.get("mode")
    dropped = ()
    if mode == "docker" and PLATFORM_OWNED_URL in values:
        values = {k: v for k, v in values.items() if k != PLATFORM_OWNED_URL}
        dropped = (PLATFORM_OWNED_URL,)
    if mode == "external" and PLATFORM_OWNED_URL not in values:
        raise SecretsError(
            f"{PLATFORM_OWNED_URL} is required: the database is external, so the "
            "application has nowhere to connect without it"
        )
    return values, dropped


def render_dotenv(values: dict) -> str:
    return "".join(f"{key}={value}\n" for key, value in values.items())


def is_stale(root: Path, environment: str) -> Optional[bool]:
    """True when the plaintext is newer than the ciphertext; None when either is missing."""
    plain, cipher = env_path(root, environment), ciphertext_path(root, environment)
    if not plain.is_file() or not cipher.is_file():
        return None
    return plain.stat().st_mtime > cipher.stat().st_mtime


def run(command, root: Path, runner=subprocess.run, timeout=60, stdin=None):
    try:
        return runner(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL if stdin is None else stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SecretsError(f"{command[0]} could not run: {error}")


def encrypt_env(root: Path, environment: str, runner=subprocess.run) -> SyncResult:
    """``.env.<environment>`` → ``deploy/secrets.<environment>.sops.yaml``."""
    plain = env_path(root, environment)
    if not plain.is_file():
        raise SecretsError(f"{plain.name} does not exist")
    manifest = load_yaml(manifest_path(root, environment))
    if not isinstance(manifest, dict):
        raise SecretsError(
            f"deploy/platform.{environment}.yml is missing or unreadable"
        )

    values, dropped = filter_for_manifest(
        parse_dotenv(plain.read_text(encoding="utf-8")), manifest
    )
    if not values:
        raise SecretsError(f"{plain.name} holds no variables to encrypt")

    target = ciphertext_path(root, environment)
    target.parent.mkdir(parents=True, exist_ok=True)

    # The filtered plaintext lives only for the duration of one sops call.
    descriptor, temporary = tempfile.mkstemp(prefix=".env-", dir=str(root))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(render_dotenv(values))
        result = run(
            [
                "sops",
                "--input-type",
                "dotenv",
                "--output-type",
                "yaml",
                "--filename-override",
                str(target.relative_to(root)),
                "--encrypt",
                os.path.basename(temporary),
            ],
            root,
            runner,
        )
    finally:
        os.unlink(temporary)

    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise SecretsError(
            f"sops could not encrypt {plain.name}: {detail[-1] if detail else 'no message'}"
        )
    target.write_bytes(result.stdout)
    # The ciphertext carries its source's timestamp, so "newer than the
    # ciphertext" means exactly "edited since", whatever the clocks did.
    source_time = plain.stat().st_mtime
    os.utime(target, (source_time, source_time))
    return SyncResult(environment, True, dropped, "encrypted")


def pull_env(root: Path, environment: str, runner=subprocess.run) -> Path:
    """Ciphertext → ``.env.<environment>``; needs a private key sops can find."""
    cipher = ciphertext_path(root, environment)
    if not cipher.is_file():
        raise SecretsError(f"{cipher.relative_to(root)} does not exist")
    result = run(
        ["sops", "--output-type", "dotenv", "--decrypt", str(cipher.relative_to(root))],
        root,
        runner,
    )
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise SecretsError(
            f"sops could not decrypt {cipher.name}: {detail[-1] if detail else 'no message'} "
            "(the private half of a recipient must be reachable, e.g. SOPS_AGE_KEY_FILE)"
        )
    plain = env_path(root, environment)
    plain.write_bytes(result.stdout)
    plain.chmod(0o600)
    return plain


def sync(
    root: Path,
    only: Optional[str] = None,
    stale_only: bool = False,
    runner=subprocess.run,
) -> list:
    """Encrypt every environment (or one); with stale_only, skip fresh ciphertext."""
    results = []
    for environment in environments(root):
        if only and environment != only:
            continue
        if not env_path(root, environment).is_file():
            results.append(SyncResult(environment, False, (), "no .env file"))
            continue
        if stale_only and is_stale(root, environment) is False:
            results.append(
                SyncResult(environment, False, (), "ciphertext is up to date")
            )
            continue
        results.append(encrypt_env(root, environment, runner))
    return results


def staged_plaintext(root: Path, runner=subprocess.run) -> list:
    """``.env.*`` files that are staged for commit — they must never be."""
    result = run(["git", "diff", "--cached", "--name-only"], root, runner)
    if result.returncode != 0:
        return []
    names = (result.stdout or b"").decode("utf-8", "replace").split()
    return [n for n in names if os.path.basename(n).startswith(".env.")]


def stage(root: Path, paths: list, runner=subprocess.run) -> None:
    if paths:
        run(["git", "add", "--", *paths], root, runner)


def enable_hooks(root: Path, runner=subprocess.run) -> bool:
    """Local repository config only — nothing committed; False outside a repository."""
    if not (root / ".git").exists():
        return False
    run(["git", "config", "core.hooksPath", HOOKS_PATH], root, runner)
    run(["git", "config", "push.followTags", "true"], root, runner)
    return True


def hooks_enabled(root: Path, runner=subprocess.run) -> Optional[bool]:
    if not (root / ".git").exists():
        return None
    result = run(["git", "config", "--get", "core.hooksPath"], root, runner)
    return (result.stdout or b"").decode("utf-8", "replace").strip() == HOOKS_PATH


def ignored(root: Path, relative: str, runner=subprocess.run) -> Optional[bool]:
    if not (root / ".git").exists():
        return None
    result = run(["git", "check-ignore", "-q", relative], root, runner)
    return result.returncode == 0
