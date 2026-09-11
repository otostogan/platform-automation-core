"""Add a host to an infrastructure repository the way the handbook does.

Two inventories, one ``host_vars`` file with a path in it, one age key per
host, one line in ``docs/RECIPIENTS.md``. The inventories are edited as text,
not re-serialised: their comments are the documentation an operator reads
first, and a YAML library would drop them. The result is parsed back before
anything is written, so a wrong indentation cannot reach the disk.
"""

import os
import re
import subprocess
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Optional

import yaml

from .context import INFRA_INVENTORY, load_yaml, read_hosts
from .recipients import RECIPIENTS_RELATIVE, read_recipients
from .scaffold import DOMAIN_PATTERN, render

BOOTSTRAP_INVENTORY = "inventory/bootstrap.yml"
GROUP_VARS = "inventory/group_vars/platform_hosts.yml"
HOST_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
IPV4_PATTERN = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)$"
)
INTERFACE_PATTERN = re.compile(r"^[a-z][a-z0-9@._-]{0,15}$")
RECIPIENT_PATTERN = re.compile(r"^age1[0-9a-z]+$")


class HostError(RuntimeError):
    pass


@dataclass
class HostAnswers:
    name: str
    public_address: str
    tailnet: str
    ssh_key: str
    keys_dir: str
    interface: str = "eth0"
    offsite: bool = False
    recovery_needed: bool = False

    @property
    def age_key(self) -> str:
        return f"{self.keys_dir}/{self.name}.agekey"

    @property
    def recovery_key(self) -> str:
        return f"{self.keys_dir}/recovery.agekey"


def validate_host_answers(answers: HostAnswers) -> list:
    errors = []
    if not HOST_PATTERN.match(answers.name):
        errors.append("host name: lowercase, digits and dashes, up to 63 chars")
    if not (
        IPV4_PATTERN.match(answers.public_address)
        or DOMAIN_PATTERN.match(answers.public_address)
    ):
        errors.append("public address: an IPv4 address or a hostname")
    if not DOMAIN_PATTERN.match(answers.tailnet):
        errors.append("tailnet address: the MagicDNS name, lowercase with dots")
    if not INTERFACE_PATTERN.match(answers.interface):
        errors.append("interface: a Linux interface name such as eth0")
    if not answers.ssh_key:
        errors.append("ssh key: a path is required")
    if not answers.keys_dir:
        errors.append("keys dir: a path is required")
    return errors


def template(name: str) -> str:
    return (
        resources.files("platform_automation.templates")
        .joinpath("infra", name)
        .read_text(encoding="utf-8")
    )


# ---------------------------------------------------------- inventory as text


def hosts_block(lines: list) -> tuple:
    """(index of the ``hosts:`` line under platform_hosts, its indent, child indent, end index).

    ``end`` is the index of the first line after the mapping — the place a
    new entry goes — and the child indent is taken from an existing entry, or
    derived from the file's own step when the mapping is empty.
    """
    under_group = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped == "platform_hosts:":
            under_group, group_indent = True, indent
            continue
        if under_group and stripped == "hosts:" and indent > group_indent:
            hosts_indent = indent
            child = None
            end = len(lines)
            for later in range(index + 1, len(lines)):
                text = lines[later]
                if not text.strip():
                    continue
                later_indent = len(text) - len(text.lstrip(" "))
                if later_indent <= hosts_indent:
                    # a comment at the outer level belongs to what follows it
                    end = later
                    break
                if text.strip().startswith("#"):
                    continue
                if child is None:
                    child = later_indent
            if child is None:
                child = hosts_indent + (hosts_indent - group_indent)
            # do not swallow the blank lines that separate the next section
            while end > index + 1 and not lines[end - 1].strip():
                end -= 1
            return index, hosts_indent, child, end
        if under_group and indent <= group_indent:
            under_group = False
    raise HostError("inventory has no all → children → platform_hosts → hosts mapping")


def reindent(entry: str, child_indent: int, unit: int) -> list:
    """Template entries are written with 4-space steps; the file may use 2."""
    out = []
    for line in entry.rstrip("\n").split("\n"):
        leading = len(line) - len(line.lstrip(" "))
        out.append(" " * (child_indent + (leading // 4) * unit) + line.strip())
    return out


def insert_host(text: str, entry: str) -> str:
    lines = text.split("\n")
    index, hosts_indent, child_indent, end = hosts_block(lines)
    unit = child_indent - hosts_indent
    block = reindent(entry, child_indent, unit)
    return "\n".join(lines[:end] + block + lines[end:])


def entries_of(text: str) -> dict:
    document = yaml.safe_load(text)
    try:
        entries = document["all"]["children"]["platform_hosts"]["hosts"]
    except (KeyError, TypeError):
        return {}
    return entries if isinstance(entries, dict) else {}


def checked_insert(text: str, entry: str, name: str) -> str:
    """Insert, then prove the file still means what it meant, plus one host."""
    before = entries_of(text)
    if name in before:
        raise HostError(f"{name} is already in the inventory")
    after_text = insert_host(text, entry)
    try:
        after = entries_of(after_text)
    except yaml.YAMLError as error:
        raise HostError(f"inserting {name} produced invalid YAML: {error}") from error
    if name not in after:
        raise HostError(f"inserting {name} did not add it to platform_hosts")
    if {k: v for k, v in after.items() if k != name} != before:
        raise HostError(f"inserting {name} would change another host's entry")
    return after_text


# ------------------------------------------------------------ recipients file


def add_recipient(text: str, label: str, recipient: str) -> str:
    """Append ``label  recipient`` to the first fenced block, aligned with it."""
    match = re.search(r"(```[^\n]*\n)(.*?)(```)", text, re.S)
    if match is None:
        raise HostError(f"{RECIPIENTS_RELATIVE} has no fenced block of recipients")
    rows = [line.split() for line in match.group(2).split("\n") if line.strip()]
    rows.append([label, recipient])
    width = max(len(row[0]) for row in rows)
    body = "".join(f"{row[0]:<{width}}  {' '.join(row[1:])}\n" for row in rows)
    return text[: match.start(2)] + body + text[match.end(2) :]


# ---------------------------------------------------------------------- keys


def generate_age_key(path: Path, runner=subprocess.run) -> str:
    """``age-keygen`` under umask 077; returns the public recipient."""
    if path.exists():
        raise HostError(f"refusing to overwrite an existing key: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    previous = os.umask(0o077)
    try:
        result = runner(
            ["age-keygen", "--output", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except FileNotFoundError as error:
        raise HostError("age-keygen is not installed on this workstation") from error
    finally:
        os.umask(previous)
    if result.returncode != 0:
        raise HostError(f"age-keygen failed: {_text(result.stderr).strip()}")
    match = re.search(r"(age1[0-9a-z]+)", _text(result.stderr) + _text(result.stdout))
    if match is None:
        raise HostError("age-keygen printed no public key")
    path.chmod(0o600)
    return match.group(1)


# ---------------------------------------------------------------------- plan


@dataclass
class HostPlan:
    files: dict = field(default_factory=dict)  # relative path → full new text
    keys: list = field(default_factory=list)  # (label, Path) to generate
    recipients_missing: bool = False


def offsite_enabled(root: Path) -> bool:
    document = load_yaml(root / GROUP_VARS)
    return bool(
        isinstance(document, dict) and document.get("platform_cli_offsite_enabled")
    )


def defaults_from(root: Path) -> dict:
    """What an existing host already answers: key dir, ssh key style, tailnet suffix."""
    found = {"keys_dir": None, "ssh_key": None, "suffix": None, "interface": None}
    hosts = read_hosts(root)
    for host in hosts:
        if host.address and "." in host.address:
            found["suffix"] = host.address.split(".", 1)[1]
        if host.key_file:
            found["ssh_key"] = host.key_file
        secrets = load_yaml(
            root / "inventory/host_vars" / host.name / "local-secrets.yml"
        )
        source = (
            secrets.get("secrets_age_key_source") if isinstance(secrets, dict) else None
        )
        if isinstance(source, str) and "/" in source:
            found["keys_dir"] = source.rsplit("/", 1)[0]
    document = load_yaml(root / INFRA_INVENTORY)
    try:
        for values in document["all"]["children"]["platform_hosts"]["hosts"].values():
            if isinstance(values, dict) and values.get("platform_public_interface"):
                found["interface"] = str(values["platform_public_interface"])
    except (KeyError, TypeError, AttributeError):
        pass
    return found


def plan_host(root: Path, answers: HostAnswers) -> HostPlan:
    errors = validate_host_answers(answers)
    if errors:
        raise HostError("; ".join(errors))

    values = {
        "host": answers.name,
        "public_address": answers.public_address,
        "tailnet": answers.tailnet,
        "ssh_key": answers.ssh_key,
        "interface": answers.interface,
        "keydir": answers.keys_dir,
    }
    plan = HostPlan()

    steady = render(template("host_steady.yml"), values)
    if answers.offsite:
        steady += f"    platform_cli_offsite_prefix: {answers.name}\n"
    for relative, entry in (
        (str(INFRA_INVENTORY), steady),
        (BOOTSTRAP_INVENTORY, render(template("host_bootstrap.yml"), values)),
    ):
        path = root / relative
        if not path.is_file():
            raise HostError(
                f"{relative} does not exist; this is not an infrastructure repository"
            )
        plan.files[relative] = checked_insert(
            path.read_text(encoding="utf-8"), entry, answers.name
        )

    host_vars = f"inventory/host_vars/{answers.name}"
    for relative in (
        f"{host_vars}/local-secrets.yml",
        f"{host_vars}/local-secrets.yml.example",
    ):
        if (root / relative).exists():
            raise HostError(f"refusing to overwrite: {relative}")
    secrets = render(template("host_local_secrets.yml"), values)
    if answers.offsite:
        secrets += (
            "\n# Object-storage credentials for this host's offsite copies.\n"
            f"platform_cli_offsite_credentials_source: {answers.keys_dir}/{answers.name}-s3.env\n"
        )
    plan.files[f"{host_vars}/local-secrets.yml"] = secrets
    plan.files[f"{host_vars}/local-secrets.yml.example"] = template(
        "host_local_secrets_example.yml"
    )

    plan.keys.append((answers.name, Path(answers.age_key).expanduser()))
    published = read_recipients(root)
    plan.recipients_missing = not published
    if answers.recovery_needed:
        if published.get("recovery"):
            raise HostError("docs/RECIPIENTS.md already publishes a recovery recipient")
        plan.keys.append(("recovery", Path(answers.recovery_key).expanduser()))
    elif not published.get("recovery"):
        raise HostError(
            "docs/RECIPIENTS.md publishes no recovery recipient — answer yes to generating one"
        )
    for label, path in plan.keys:
        if path.exists():
            raise HostError(f"refusing to overwrite an existing key: {path}")
    return plan


def write_host(
    root: Path, answers: HostAnswers, plan: HostPlan, runner=subprocess.run
) -> dict:
    """Keys first (they feed RECIPIENTS.md), then every file. Returns recipients."""
    recipients = {}
    for label, path in plan.keys:
        recipients[label] = generate_age_key(path, runner)

    recipients_path = root / RECIPIENTS_RELATIVE
    if plan.recipients_missing:
        rows = [(answers.name, recipients[answers.name])]
        rows.append(
            ("recovery", recipients.get("recovery", "age1<recovery recipient>"))
        )
        width = max(len(label) for label, _ in rows)
        text = render(
            template("recipients.md"),
            {
                "recipient_rows": "".join(
                    f"{label:<{width}}  {value}\n" for label, value in rows
                )
            },
        )
    else:
        text = recipients_path.read_text(encoding="utf-8")
        text = add_recipient(text, answers.name, recipients[answers.name])
        if "recovery" in recipients:
            text = add_recipient(text, "recovery", recipients["recovery"])
    plan.files[str(RECIPIENTS_RELATIVE)] = text

    for relative, content in plan.files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return recipients


def next_steps(answers: HostAnswers, recipients: dict) -> str:
    lines = [
        "Next:",
        f"  1. Provider: install the ops public key for root — ssh-copy-id -i {answers.ssh_key}.pub root@{answers.public_address}",
        "  2. Tailnet: a one-off auth key for tag:server-platform (Reusable off, Ephemeral off,",
        f"     Pre-approved on) → {answers.keys_dir}/tailscale-auth.key, mode 0600 — handbook #/flow-new-host step 3",
        "  3. Bootstrap, then converge twice and readiness — handbook #/flow-new-host steps 6–11.",
        f"  4. Commit both inventories and docs/RECIPIENTS.md; local-secrets.yml stays ignored.",
    ]
    if "recovery" in recipients:
        lines.append(
            f"  5. Put {answers.recovery_key} in the company secret store; it never goes to a server."
        )
    return "\n".join(lines)


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
