"""Start a company infrastructure repository from nothing.

The repository is small on purpose: a pin, two inventories, one group policy,
paths to private material, and the documents an operator needs. Everything
the console can make it makes — the operator's SSH key, both age keys — and
it asks only for what belongs to the company: its name, the ACME contact,
the first host. The result is a git repository with nothing committed and a
registry entry, so ``doctor`` and ``new app`` see it at once.
"""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import __version__
from .config import register_infra
from .hosts import (
    HostAnswers,
    HostError,
    generate_age_key,
    next_steps as host_next_steps,
    plan_host,
    template,
    validate_host_answers,
    write_host,
)
from .scaffold import render

COMPANY_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SSH_PUBLIC_PATTERN = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) [A-Za-z0-9+/=]+( .*)?$"
)
ACME_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
ACME_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
ANSIBLE_CORE = "2.15.13"

DESTINATIONS = {
    "ansible.cfg": "ansible.cfg",
    "requirements.yml": "requirements.yml",
    "gitignore": ".gitignore",
    "README.md": "README.md",
    "TAILNET.md": "docs/TAILNET.md",
    "inventory_hosts.yml": "inventory/hosts.yml",
    "inventory_bootstrap.yml": "inventory/bootstrap.yml",
    "group_vars.yml": "inventory/group_vars/platform_hosts.yml",
    "all_local_secrets.yml": "inventory/group_vars/all/local-secrets.yml",
    "all_local_secrets_example.yml": "inventory/group_vars/all/local-secrets.yml.example",
}


class CompanyError(RuntimeError):
    pass


@dataclass
class CompanyAnswers:
    company: str
    acme_email: str
    operator: str
    operator_key: str  # private key path; generated when missing
    host: HostAnswers
    acme_ca: str = ACME_STAGING
    extra_ops_keys: tuple = ()  # public key lines of other operators

    @property
    def core_pin(self) -> str:
        return f"v{__version__}"


def validate_company_answers(answers: CompanyAnswers) -> list:
    errors = []
    if not COMPANY_PATTERN.match(answers.company):
        errors.append("company: lowercase, digits and dashes")
    if not EMAIL_PATTERN.match(answers.acme_email):
        errors.append("ACME email: an address Let's Encrypt can write to")
    if answers.acme_ca not in (ACME_STAGING, ACME_PRODUCTION):
        errors.append("ACME CA: staging or production")
    if not answers.operator.strip():
        errors.append("operator: a name for the key comment")
    if not answers.operator_key:
        errors.append("operator key: a path is required")
    for line in answers.extra_ops_keys:
        if not SSH_PUBLIC_PATTERN.match(line.strip()):
            errors.append(
                f"operator public key is not an OpenSSH public key line: {line[:24]}…"
            )
    errors += validate_host_answers(answers.host)
    return errors


def refuse_existing(root: Path) -> None:
    present = [
        relative
        for relative in ("inventory", "requirements.yml", "ansible.cfg")
        if (root / relative).exists()
    ]
    if present:
        raise CompanyError(
            f"{root} already has {', '.join(present)} — new company-infra starts from an empty directory"
        )


# ---------------------------------------------------------------------- keys


def generate_ssh_key(path: Path, comment: str, runner=subprocess.run) -> str:
    """ed25519, no passphrase; returns the public key line. Existing keys are reused."""
    public = path.with_name(path.name + ".pub")
    if path.exists():
        if not public.is_file():
            raise CompanyError(
                f"{path} exists but {public.name} does not; cannot read its public half"
            )
        return public.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = runner(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                comment,
                "-f",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except FileNotFoundError as error:
        raise CompanyError("ssh-keygen is not installed on this workstation") from error
    if result.returncode != 0:
        raise CompanyError(f"ssh-keygen failed: {_text(result.stderr).strip()}")
    path.chmod(0o600)
    return public.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------- plan


def render_company(answers: CompanyAnswers, operator_public_key: str) -> dict:
    keys = [operator_public_key.strip()] + [
        line.strip() for line in answers.extra_ops_keys
    ]
    values = {
        "company": answers.company,
        "core_pin": answers.core_pin,
        "core_version": __version__,
        "acme_email": answers.acme_email,
        "acme_ca": answers.acme_ca,
        "keydir": answers.host.keys_dir,
        "ops_keys": "\n".join(f'    - "{line}"' for line in keys),
    }
    return {
        destination: render(template(name), values)
        for name, destination in DESTINATIONS.items()
    }


@dataclass
class CompanyResult:
    written: list = field(default_factory=list)
    recipients: dict = field(default_factory=dict)
    operator_public_key: str = ""
    git_initialised: bool = False


def write_company(
    root: Path,
    answers: CompanyAnswers,
    runner=subprocess.run,
    home: Optional[Path] = None,
) -> CompanyResult:
    errors = validate_company_answers(answers)
    if errors:
        raise CompanyError("; ".join(errors))
    root.mkdir(parents=True, exist_ok=True)
    refuse_existing(root)
    for label, path in (
        (answers.host.name, answers.host.age_key),
        ("recovery", answers.host.recovery_key),
    ):
        if Path(path).expanduser().exists():
            raise CompanyError(f"refusing to overwrite an existing key: {path}")

    result = CompanyResult()
    key_path = Path(answers.operator_key).expanduser()
    result.operator_public_key = generate_ssh_key(
        key_path, f"ops:{answers.operator}", runner
    )

    files = render_company(answers, result.operator_public_key)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        result.written.append(relative)

    # The first host goes in the same way every later one will.
    host = HostAnswers(**{**answers.host.__dict__, "recovery_needed": True})
    try:
        plan = plan_host(root, host)
        result.recipients = write_host(root, host, plan, runner)
    except HostError as error:
        raise CompanyError(str(error)) from error
    result.written += [
        relative for relative in plan.files if relative not in result.written
    ]

    if not (root / ".git").exists():
        init = runner(
            ["git", "init", "-q"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        result.git_initialised = init.returncode == 0
    register_infra(root, keys=Path(answers.host.keys_dir).expanduser(), home=home)
    return result


def venv_commands() -> list:
    """What the handbook's venv step runs; the console prints them and asks."""
    return [
        "python3 -m venv .venv",
        ".venv/bin/python -m pip install --upgrade pip",
        f".venv/bin/python -m pip install 'ansible-core=={ANSIBLE_CORE}'",
        ".venv/bin/ansible-galaxy collection install --force --requirement requirements.yml",
    ]


def next_steps(answers: CompanyAnswers, result: CompanyResult) -> str:
    lines = [
        "Next:",
        f"  1. Second operator: their public key line into inventory/group_vars/platform_hosts.yml"
        " (users_ops_ssh_keys) — one key recreates the dependency this platform removes.",
        "  2. Tailnet policy: tagOwners for tag:server-platform, the ssh rule for ops — docs/TAILNET.md,"
        " handbook #/ref-network.",
    ]
    for line in host_next_steps(answers.host, result.recipients).split("\n")[1:]:
        if "Commit both" in line:
            line = (
                line[:5]
                + " Commit everything git shows; local-secrets.yml files stay ignored."
            )
        number = int(line.strip()[0]) + 2 if line.strip()[:1].isdigit() else None
        lines.append(f"  {number}. {line.strip()[3:]}" if number else line)
    return "\n".join(lines)


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
