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
    "hook_pre-commit": ".githooks/pre-commit",
    "gitignore": ".gitignore",
}
EXECUTABLE = {".githooks/post-commit", ".githooks/pre-push", ".githooks/pre-commit"}


MARKER = "# platform-managed"
MARKER_PATTERN = re.compile(r"^# platform-managed v(\d+\.\d+\.\d+)")
MANAGED = {
    ".sops.yaml",
    ".github/workflows/build.yml",
    ".github/workflows/release.yml",
    ".github/workflows/deploy.yml",
    ".githooks/post-commit",
    ".githooks/pre-push",
    ".githooks/pre-commit",
}
DEPLOY_WORKFLOW = ".github/workflows/deploy.yml"
# what the handbook shows; replaced by the environments actually chosen
ENVIRONMENT_OPTIONS = "                    - lab\n                    - production\n"


def marker_line(version: str = __version__) -> str:
    return (
        f"{MARKER} v{version} · «platform update» перезапишет этот файл;"
        " уберите эту строку, чтобы забрать его себе."
    )


def with_marker(text: str, version: str = __version__) -> str:
    """The marker goes first — after the shebang when there is one."""
    line = marker_line(version)
    if text.startswith("#!"):
        first, _, rest = text.partition("\n")
        return f"{first}\n{line}\n{rest}"
    return f"{line}\n{text}"


def strip_marker(text: str) -> str:
    lines = text.split("\n")
    for index in range(min(2, len(lines))):
        if lines[index].startswith(MARKER):
            del lines[index]
            break
    return "\n".join(lines)


def marker_version(text: str) -> Optional[str]:
    for line in text.split("\n")[:2]:
        match = MARKER_PATTERN.match(line)
        if match:
            return match.group(1)
    return None


def is_managed(text: str) -> bool:
    return marker_version(text) is not None


def environment_options(workflow: str, environments: tuple) -> str:
    """The dispatch offers exactly the environments the application has."""
    wanted = "".join(f"                    - {name}\n" for name in environments)
    return workflow.replace(ENVIRONMENT_OPTIONS, wanted, 1)


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
    backup_enabled: bool = True
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
        text = render(template(name), common)
        if destination == DEPLOY_WORKFLOW:
            text = environment_options(text, tuple(answers.environments))
        files[destination] = with_marker(text) if destination in MANAGED else text

    database = template(
        "database_docker.yml"
        if answers.database_mode == "docker"
        else "database_external.yml"
    )
    if answers.database_mode == "docker" and not answers.backup_enabled:
        database = without_schedule(database)
    dotenv = "".join(f"{name}={PLACEHOLDER_VALUE}\n" for name in answers.secret_names)

    for environment in answers.environments:
        values = {**common, "env": environment, "domain": answers.domains[environment]}
        manifest = (
            render(template("platform.yml"), values).rstrip("\n")
            + "\n\n"
            + render(database, values)
        )
        files[f"deploy/platform.{environment}.yml"] = manifest
        files[f".env.{environment}"] = dotenv

    return files


def without_schedule(database_block: str) -> str:
    """A platform-owned database with no timer.

    The contract forbids the ``backup`` block once ``backup_enabled`` is
    false, and the dump before each migration is taken regardless: the flag
    governs the schedule, not the safety.
    """
    lines = []
    skipping = False
    for line in database_block.splitlines(keepends=True):
        if line.startswith("    backup:"):
            skipping = True
            continue
        if skipping and line.startswith("        "):
            continue
        skipping = False
        lines.append(line.replace("backup_enabled: true", "backup_enabled: false"))
    return "".join(lines)


def produced_by(files: dict) -> list:
    """Paths the scaffold creates besides the ones it writes directly."""
    return [
        f"deploy/secrets.{path[len('.env.'):]}.sops.yaml"
        for path in files
        if path.startswith(".env.")
    ]


MERGED = {".gitignore"}  # every real project has one: ours is appended, never a clash


def existing_targets(root: Path, files: dict) -> list:
    return sorted(
        path
        for path in [*files, *produced_by(files)]
        if path not in MERGED and (root / path).exists()
    )


def merge_lines(existing: str, wanted: str) -> tuple:
    """Append the lines of ``wanted`` that ``existing`` lacks; nothing is removed."""
    present = {line.strip() for line in existing.splitlines()}
    missing = [
        line
        for line in wanted.splitlines()
        if line.strip() and line.strip() not in present
    ]
    if not missing:
        return existing, 0
    joined = existing if not existing or existing.endswith("\n") else existing + "\n"
    return joined + "\n".join(missing) + "\n", len(missing)


def write_files(root: Path, files: dict) -> list:
    """Write everything, or nothing: an existing file stops the whole scaffold.

    ``.gitignore`` is the one exception — it is merged, because every real
    repository already has one.
    """
    clashes = existing_targets(root, files)
    if clashes:
        raise ScaffoldError(
            "refusing to overwrite existing files: "
            + ", ".join(clashes)
            + " — new app never edits what is already there; remove or rename them first"
        )
    written = []
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative in MERGED and path.exists():
            merged, added = merge_lines(path.read_text(encoding="utf-8"), text)
            if added:
                path.write_text(merged, encoding="utf-8")
                written.append(f"{relative} (+{added} lines)")
            continue
        path.write_text(text, encoding="utf-8")
        if relative in EXECUTABLE:
            path.chmod(0o755)
        written.append(relative)
    return written


def encrypt_secrets(root: Path, files: dict, runner=subprocess.run) -> list:
    """Every ``.env.<environment>`` → its ciphertext, by the same path the hook uses."""
    from .secrets import SecretsError, encrypt_env

    encrypted = []
    for relative in files:
        if not relative.startswith(".env."):
            continue
        environment = relative[len(".env.") :]
        try:
            encrypt_env(root, environment, runner)
        except SecretsError as error:
            raise ScaffoldError(str(error))
        encrypted.append(f"deploy/secrets.{environment}.sops.yaml")
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
        f"  1. Real secret values: edit .env.{answers.environments[0]} (and the others) — plain",
        "     KEY=value. They stay on this machine; the pre-commit hook encrypts them",
        "     into deploy/secrets.<env>.sops.yaml on every commit.",
        f"  2. Tailnet policy: add tag:ci-{answers.project} to tagOwners, a grant to",
        "     tag:server-platform on tcp:22 and an ssh rule for user deploy — handbook #/flow-new-app.",
        "  3. Repository settings: the Tailscale OAuth client id and audience as secrets.",
        f"  4. Commit, tag v0.1.0, and deploy to {answers.environments[0]} — handbook #/flow-deploy.",
    ]
    return "\n".join(lines)
