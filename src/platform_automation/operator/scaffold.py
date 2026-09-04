"""``platform new app``: the files an application needs, written once, never
copied by hand again.

The templates are the handbook's own — a test keeps them identical — and the
values that vary come from the operator once. Nothing here is guessed: what
can be read from the repository or the infrastructure is read; what cannot is
asked; and existing files are never overwritten.
"""

import re
import subprocess
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Optional

from .. import __version__
from ..validate_manifest import (
    DEFAULT_SCHEMA,
    load_json,
    load_yaml,
    resolve_compose_path,
    validate_compose,
    validate_manifest,
)

TOKEN = re.compile(r"\{\{(\w+)\}\}")
PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
SECRET_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENVIRONMENTS = ("lab", "staging", "production")
POSTGRES_MAJORS = (16, 17, 18)
PLACEHOLDER_VALUE = "замените-на-настоящее"

# template name in the package -> where it lands in the application
DESTINATIONS = {
    "sops.yaml": ".sops.yaml",
    "compose.yml": "deploy/compose.yml",
    "workflow_build.yml": ".github/workflows/build.yml",
    "workflow_release.yml": ".github/workflows/release.yml",
    "workflow_deploy.yml": ".github/workflows/deploy.yml",
    "hook_post-commit": ".githooks/post-commit",
    "hook_pre-push": ".githooks/pre-push",
}
EXECUTABLE = {".githooks/post-commit", ".githooks/pre-push"}


class ScaffoldError(RuntimeError):
    pass


@dataclass(frozen=True)
class AppAnswers:
    project: str
    owner: str  # the ghcr.io owner; named so a policy scan does not read it as a .org domain
    environments: tuple
    domains: dict  # environment -> host
    target_host: str
    recipient_host: str
    recipient_recovery: str
    internal_port: int = 3000
    healthcheck_path: str = "/"
    healthcheck_timeout: int = 120
    database_mode: str = "docker"
    postgres_major: int = 18
    backup_interval: int = 15
    backup_retain: int = 3
    restore_query: str = "SELECT 1"
    secret_names: tuple = ("API_TOKEN", "SESSION_SECRET")
    core_pin: str = field(default_factory=lambda: f"v{__version__}")


def validate_answers(answers: AppAnswers) -> list:
    """The same rules the schema and the runtime enforce, applied before writing."""
    errors = []
    if not PROJECT_PATTERN.match(answers.project):
        errors.append("project must match ^[a-z][a-z0-9-]{1,62}$")
    if not answers.owner:
        errors.append("owner (the ghcr.io owner) is required")
    if not answers.environments:
        errors.append("at least one environment is required")
    for environment in answers.environments:
        if environment not in ENVIRONMENTS:
            errors.append(
                f"environment must be one of {', '.join(ENVIRONMENTS)}: {environment}"
            )
        host = answers.domains.get(environment)
        if not host or not DOMAIN_PATTERN.match(host):
            errors.append(
                f"domain for {environment} must be lowercase with at least one dot"
            )
    for label, value in (
        ("host", answers.recipient_host),
        ("recovery", answers.recipient_recovery),
    ):
        if not value.startswith("age1"):
            errors.append(f"{label} recipient must be an age public key (age1…)")
    if answers.recipient_host == answers.recipient_recovery:
        errors.append("the two recipients must be different keys")
    if not 1 <= answers.internal_port <= 65535:
        errors.append("internal_port must be between 1 and 65535")
    if not answers.healthcheck_path.startswith("/"):
        errors.append("healthcheck path must start with /")
    if answers.healthcheck_timeout < 1:
        errors.append("healthcheck timeout must be positive")
    if answers.database_mode not in ("docker", "external"):
        errors.append("database mode must be docker or external")
    if answers.postgres_major not in POSTGRES_MAJORS:
        errors.append(f"postgres_major must be one of {POSTGRES_MAJORS}")
    if not 15 <= answers.backup_interval <= 1440:
        errors.append("backup interval must be between 15 and 1440 minutes")
    if not 1 <= answers.backup_retain <= 100:
        errors.append("backup retain must be between 1 and 100")
    if not answers.secret_names:
        errors.append("at least one secret name is required")
    for name in answers.secret_names:
        if not SECRET_NAME_PATTERN.match(name):
            errors.append(f"secret name is not a valid environment variable: {name}")
        if name == "DATABASE_URL" and answers.database_mode == "docker":
            errors.append(
                "DATABASE_URL is set by the platform when it runs the database"
            )
    return errors


def template(name: str) -> str:
    return (
        resources.files("platform_automation.templates")
        .joinpath("app", name)
        .read_text(encoding="utf-8")
    )


def render(text: str, values: dict) -> str:
    """Replace ``{{token}}`` for known tokens only.

    ``${{ github.ref }}`` and ``{{.Manifest.Digest}}`` never match: the token
    must be one word directly inside the braces — the same rule the handbook's
    context panel uses.
    """

    def replace(match):
        key = match.group(1)
        return str(values[key]) if key in values else match.group(0)

    return TOKEN.sub(replace, text)


def render_app(answers: AppAnswers) -> dict:
    """Every file ``new app`` writes, keyed by path relative to the repository."""
    errors = validate_answers(answers)
    if errors:
        raise ScaffoldError("; ".join(errors))

    common = {
        "project": answers.project,
        "org": answers.owner,
        "tailnet": answers.target_host,
        "core_pin": answers.core_pin,
        "internal_port": answers.internal_port,
        "healthcheck_path": answers.healthcheck_path,
        "healthcheck_timeout": answers.healthcheck_timeout,
        "postgres_major": answers.postgres_major,
        "backup_interval": answers.backup_interval,
        "backup_retain": answers.backup_retain,
        "restore_query": answers.restore_query,
        "recipient_host": answers.recipient_host,
        "recipient_recovery": answers.recipient_recovery,
    }
    files = {}

    for name, destination in DESTINATIONS.items():
        files[destination] = render(template(name), common)

    database = template(
        "database_docker.yml"
        if answers.database_mode == "docker"
        else "database_external.yml"
    )
    secrets_plaintext = "".join(
        f"{name}: {PLACEHOLDER_VALUE}\n" for name in answers.secret_names
    )

    for environment in answers.environments:
        values = {**common, "env": environment, "domain": answers.domains[environment]}
        manifest = (
            render(template("platform.yml"), values).rstrip("\n")
            + "\n\n"
            + render(database, values)
        )
        files[f"deploy/platform.{environment}.yml"] = manifest
        files[f"deploy/secrets.{environment}.sops.yaml"] = secrets_plaintext

    return files


def existing_targets(root: Path, files: dict) -> list:
    return sorted(path for path in files if (root / path).exists())


def write_files(root: Path, files: dict) -> list:
    """Write everything, or nothing: an existing file stops the whole scaffold."""
    clashes = existing_targets(root, files)
    if clashes:
        raise ScaffoldError(
            "refusing to overwrite existing files: "
            + ", ".join(clashes)
            + " — new app is for a new application; for an existing one use platform doctor"
        )
    written = []
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if relative in EXECUTABLE:
            path.chmod(0o755)
        written.append(relative)
    return written


def encrypt_secrets(root: Path, files: dict, runner=subprocess.run) -> list:
    """Encrypt every secrets file in place; a plaintext file must not survive."""
    encrypted = []
    for relative in files:
        if not relative.startswith("deploy/secrets."):
            continue
        path = root / relative
        try:
            result = runner(
                ["sops", "encrypt", "--in-place", str(path)],
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            path.unlink(missing_ok=True)
            raise ScaffoldError(
                f"sops is not available ({error}); {relative} was not left in plaintext"
            )
        if result.returncode != 0:
            path.unlink(missing_ok=True)
            detail = (
                (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            )
            raise ScaffoldError(
                f"sops could not encrypt {relative}: {detail[-1] if detail else 'no message'};"
                " the plaintext file was removed"
            )
        encrypted.append(relative)
    return encrypted


def validate_app(root: Path, files: dict) -> dict:
    """The same check the deployment runs first, per environment."""
    schema = load_json(DEFAULT_SCHEMA)
    report = {}
    for relative in files:
        if not (relative.startswith("deploy/platform.") and relative.endswith(".yml")):
            continue
        document = load_yaml(root / relative)
        errors = validate_manifest(document, schema)
        if not errors:
            compose = load_yaml(resolve_compose_path(root, document["compose_file"]))
            errors = validate_compose(document, compose)
        report[relative] = errors
    return report


def git_org(root: Path, runner=subprocess.run) -> Optional[str]:
    """The ghcr.io owner is the GitHub owner of origin, when there is one."""
    try:
        result = runner(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    url = (result.stdout or b"").decode("utf-8", "replace").strip()
    match = re.search(r"github\.com[:/]([^/]+)/", url)
    return match.group(1) if match else None


def next_steps(answers: AppAnswers, written: list) -> str:
    lines = [
        "Written (nothing committed):",
        *[f"  {path}" for path in written],
        "",
        "Next:",
        "  1. Real secret values: sops deploy/secrets.<env>.sops.yaml — the editor opens",
        "     the file decrypted and saves it encrypted.",
        "  2. Hooks: chmod +x .githooks/post-commit .githooks/pre-push &&",
        "     git config core.hooksPath .githooks && git config push.followTags true",
        f"  3. Tailnet policy: add tag:ci-{answers.project} to tagOwners, a grant to",
        "     tag:server-platform on tcp:22 and an ssh rule for user deploy — handbook #/flow-new-app.",
        "  4. Repository settings: the Tailscale OAuth client id and audience as secrets.",
        f"  5. Commit, tag v0.1.0, and deploy to {answers.environments[0]} — handbook #/flow-deploy.",
    ]
    return "\n".join(lines)
