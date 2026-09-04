"""Answer "why won't this work" before anything runs.

Every check here mirrors a refusal that would otherwise arrive later and
worse: convergence's five assertions, a missing key file, a collection that
does not match its pin. Each finding names the handbook page that explains
the refusal, so the fix is one click away rather than one incident away.
"""

import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..sops_validation import valid_age_recipients, validate_sops_document
from ..validate_manifest import (
    DEFAULT_SCHEMA,
    load_json,
    load_yaml,
    resolve_compose_path,
    validate_compose,
    validate_manifest,
)
from .config import infra_for_host, infras
from .recipients import host_recipient, read_recipients, recovery_recipient
from .context import Context, Host, read_collection_pin
from .tailnet import Tailnet, find_peer

EXPECTED_USER = "ops"
TAG_PATTERN = re.compile(r"^tag:[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
ANSIBLE_VERSION_PATTERN = re.compile(r"core\s+(\d+\.\d+\.\d+)")
REQUIREMENT_PATTERN = re.compile(r"^\s*(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)\s*$")
COLLECTION_RELATIVE = Path("ansible_collections/otostogan/platform")
CONFIG_HINT = "register it with: platform infra add <path> (or run platform doctor inside it once)"


@dataclass(frozen=True)
class Finding:
    status: str  # "ok" | "fail" | "skip"
    title: str
    detail: str
    anchor: str

    @property
    def failed(self) -> bool:
        return self.status == "fail"


def ok(title, detail, anchor="") -> Finding:
    return Finding("ok", title, detail, anchor)


def fail(title, detail, anchor) -> Finding:
    return Finding("fail", title, detail, anchor)


def skip(title, detail, anchor="") -> Finding:
    return Finding("skip", title, detail, anchor)


# --------------------------------------------------------------------- shared


def tailnet_finding(tailnet: Tailnet) -> Finding:
    if not tailnet.available:
        return fail("Tailscale", tailnet.error or "not available", "#/flow-incidents")
    if tailnet.backend_state != "Running":
        return fail(
            "Tailscale",
            f"backend is {tailnet.backend_state}; convergence and SSH both need Running",
            "#/flow-incidents",
        )
    return ok("Tailscale", f"connected as {tailnet.self_dns or 'this machine'}")


def peer_finding(tailnet: Tailnet, name: str, label: str) -> Finding:
    if not tailnet.running:
        return skip(
            label, "cannot check while Tailscale is not running", "#/ref-network"
        )
    peer = find_peer(tailnet, name)
    if peer is None:
        return fail(label, f"{name} is not in this tailnet", "#/ref-network")
    if not peer.online:
        return fail(label, f"{peer.dns_name} is offline", "#/flow-reboot")
    tags = ", ".join(peer.tags) if peer.tags else "no tags"
    return ok(label, f"{peer.dns_name} online · {tags}")


def private_file_finding(title: str, path: Optional[Path], anchor: str) -> Finding:
    if path is None:
        return fail(title, "no path configured", anchor)
    if path.is_symlink():
        return fail(title, f"{path} is a symbolic link", anchor)
    if not path.is_file():
        return fail(title, f"{path} does not exist", anchor)
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return fail(title, f"{path} is mode {mode:04o}; expected 0600", anchor)
    return ok(title, str(path))


def parse_version(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


def version_satisfies(version: str, requirement: str) -> Optional[bool]:
    """Evaluate a ``>=a,<b`` style requirement; None when it cannot be parsed."""
    try:
        have = parse_version(version)
    except ValueError:
        return None

    for clause in requirement.split(","):
        match = REQUIREMENT_PATTERN.match(clause)
        if match is None:
            return None
        operator, wanted = match.group(1), parse_version(match.group(2))
        verdict = {
            ">=": have >= wanted,
            "<=": have <= wanted,
            "==": have == wanted,
            "!=": have != wanted,
            ">": have > wanted,
            "<": have < wanted,
        }[operator]
        if not verdict:
            return False

    return True


# ---------------------------------------------------------------- infra side


def default_collections_root(home: Path) -> Path:
    configured = os.environ.get("ANSIBLE_COLLECTIONS_PATH") or os.environ.get(
        "ANSIBLE_COLLECTIONS_PATHS"
    )
    if configured:
        return Path(configured.split(os.pathsep)[0]).expanduser()
    return home / ".ansible/collections"


def installed_collection(collections_root: Path) -> Optional[dict[str, Any]]:
    """Version and Ansible requirement of the collection the venv would load."""
    directory = collections_root / COLLECTION_RELATIVE
    manifest = directory / "MANIFEST.json"
    runtime = directory / "meta/runtime.yml"

    try:
        info = json.loads(manifest.read_text(encoding="utf-8"))["collection_info"]
        version = str(info["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return None

    requires = None
    document = load_yaml(runtime) if runtime.is_file() else None
    if isinstance(document, dict) and isinstance(document.get("requires_ansible"), str):
        requires = document["requires_ansible"]

    return {"version": version, "requires_ansible": requires, "path": directory}


def ansible_core_version(root: Path, runner) -> Optional[str]:
    executable = root / ".venv/bin/ansible"
    if not executable.is_file():
        return None
    try:
        result = runner(
            [str(executable), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = ANSIBLE_VERSION_PATTERN.search(_text(result.stdout))
    return match.group(1) if match else None


def collection_findings(context: Context, runner, collections_root: Path) -> list:
    findings = []
    installed = installed_collection(collections_root)
    pin = context.core_pin

    if installed is None:
        findings.append(
            fail(
                "Core collection",
                f"otostogan.platform is not installed under {collections_root}",
                "#/flow-new-host",
            )
        )
        return findings

    have = f"v{installed['version']}"
    if pin is None:
        findings.append(
            skip(
                "Core collection",
                f"{have} installed; requirements.yml has no pin",
                "#/flow-core-update",
            )
        )
    elif have == pin:
        findings.append(
            ok("Core collection", f"{have} installed, matches requirements.yml")
        )
    else:
        findings.append(
            fail(
                "Core collection",
                f"{have} installed but requirements.yml pins {pin}; reinstall with --force",
                "#/flow-core-update",
            )
        )

    core = ansible_core_version(context.root, runner)
    requires = installed["requires_ansible"]
    if core is None:
        findings.append(
            fail(
                "ansible-core",
                ".venv/bin/ansible is missing or does not report a version",
                "#/flow-new-host",
            )
        )
    elif requires is None:
        findings.append(ok("ansible-core", f"{core} in .venv"))
    else:
        verdict = version_satisfies(core, requires)
        if verdict is None:
            findings.append(
                skip("ansible-core", f"{core}; cannot read requirement {requires!r}")
            )
        elif verdict:
            findings.append(ok("ansible-core", f"{core} satisfies {requires}"))
        else:
            findings.append(
                fail(
                    "ansible-core",
                    f"{core} is outside {requires}",
                    "#/flow-core-update",
                )
            )

    return findings


def host_secret_path(root: Path, host: Host, key: str) -> Optional[Path]:
    document = load_yaml(root / "inventory/host_vars" / host.name / "local-secrets.yml")
    value = document.get(key) if isinstance(document, dict) else None
    return Path(str(value)).expanduser() if isinstance(value, str) and value else None


def age_recipient(path: Path, runner) -> Optional[str]:
    try:
        result = runner(
            ["age-keygen", "-y", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = _text(result.stdout).strip()
    return text if result.returncode == 0 and text.startswith("age1") else None


def host_findings(context: Context, host: Host, tailnet: Tailnet, runner) -> list:
    label = f"{host.name}"
    findings = [peer_finding(tailnet, host.address or host.name, f"{label}: tailnet")]

    if host.user == EXPECTED_USER:
        findings.append(ok(f"{label}: ansible_user", EXPECTED_USER))
    else:
        findings.append(
            fail(
                f"{label}: ansible_user",
                f"{host.user or 'unset'}; convergence requires {EXPECTED_USER}",
                "#/flow-incidents",
            )
        )

    key_file = Path(host.key_file).expanduser() if host.key_file else None
    findings.append(
        private_file_finding(f"{label}: SSH key", key_file, "#/flow-new-host")
    )

    age_path = host_secret_path(context.root, host, "secrets_age_key_source")
    age_check = private_file_finding(f"{label}: age key", age_path, "#/ref-keys")
    if age_check.status == "ok":
        recipient = age_recipient(age_path, runner)
        if recipient is None:
            age_check = fail(
                f"{label}: age key",
                f"{age_path} is not a private age key",
                "#/ref-keys",
            )
        else:
            age_check = ok(f"{label}: age key", f"{age_path} → {recipient[:12]}…")
    findings.append(age_check)

    return findings


def diagnose_infra(
    context: Context,
    tailnet: Tailnet,
    runner,
    home: Path,
    collections_root: Optional[Path],
) -> list:
    findings = [tailnet_finding(tailnet)]
    findings += collection_findings(
        context, runner, collections_root or default_collections_root(home)
    )
    if not context.hosts:
        findings.append(
            fail("Hosts", "inventory/hosts.yml lists no hosts", "#/flow-new-host")
        )
    for host in context.hosts:
        findings += host_findings(context, host, tailnet, runner)
    return findings


# ------------------------------------------------------------------ app side


def manifest_findings(root: Path, manifest_path: Path, schema: dict) -> list:
    label = str(manifest_path)
    document = load_yaml(root / manifest_path)

    if not isinstance(document, dict):
        return [fail(label, "manifest is not a YAML object", "#/ref-manifest")]

    errors = validate_manifest(document, schema)
    if errors:
        return [
            fail(
                label,
                "; ".join(errors[:3]) + (" …" if len(errors) > 3 else ""),
                "#/ref-manifest",
            )
        ]

    findings = [ok(label, "valid platform/v1 manifest")]

    try:
        compose_path = resolve_compose_path(root, document["compose_file"])
        compose = load_yaml(compose_path)
    except (OSError, ValueError, KeyError) as error:
        return findings + [fail(f"{label}: compose", str(error), "#/ref-compose")]

    compose_errors = validate_compose(document, compose)
    if compose_errors:
        findings.append(
            fail(f"{label}: compose", "; ".join(compose_errors[:3]), "#/ref-compose")
        )
    else:
        findings.append(
            ok(f"{label}: compose", f"{document['compose_file']} honours the contract")
        )

    secrets = (
        document.get("secrets") if isinstance(document.get("secrets"), dict) else {}
    )
    source = secrets.get("source")
    if not isinstance(source, str):
        findings.append(
            fail(f"{label}: secrets", "secrets.source is missing", "#/flow-new-app")
        )
        return findings

    secrets_path = root / source
    if not secrets_path.is_file():
        findings.append(
            fail(f"{label}: secrets", f"{source} does not exist", "#/flow-new-app")
        )
        return findings

    secrets_document = load_yaml(secrets_path)
    sops_errors = validate_sops_document(secrets_document, 1)
    if sops_errors:
        findings.append(
            fail(f"{label}: secrets", "; ".join(sops_errors), "#/flow-new-app")
        )
    else:
        count = len(valid_age_recipients(secrets_document))
        findings.append(
            ok(f"{label}: secrets", f"{source} encrypted to {count} recipient(s)")
        )

    return findings


def secrets_findings(context: Context) -> list:
    """The hook only helps when it runs, and only if the plaintext never leaves."""
    from .secrets import env_path, hooks_enabled, ignored, is_stale

    findings = []
    active = hooks_enabled(context.root)
    if active is None:
        findings.append(skip("Git hooks", "not a git repository", "#/flow-new-app"))
    elif active:
        findings.append(
            ok(
                "Git hooks",
                "core.hooksPath=.githooks — secrets are encrypted on commit",
            )
        )
    else:
        findings.append(
            fail(
                "Git hooks",
                "core.hooksPath is not .githooks; run: git config core.hooksPath .githooks",
                "#/flow-new-app",
            )
        )

    for environment in context.environments:
        plain = env_path(context.root, environment.environment)
        if not plain.is_file():
            continue
        if ignored(context.root, plain.name) is False:
            findings.append(
                fail(
                    plain.name,
                    "is not ignored by git — add .env.* to .gitignore",
                    "#/flow-new-app",
                )
            )
        stale = is_stale(context.root, environment.environment)
        if stale:
            findings.append(
                fail(
                    plain.name,
                    "is newer than its ciphertext; run: platform secrets push",
                    "#/flow-new-app",
                )
            )
        elif stale is False:
            findings.append(ok(plain.name, "ciphertext is up to date"))
    return findings


def diagnose_app(context: Context, tailnet: Tailnet, home: Path) -> list:
    findings = [tailnet_finding(tailnet)]
    schema = load_json(DEFAULT_SCHEMA)

    for environment in context.environments:
        findings += manifest_findings(context.root, environment.manifest, schema)

    findings += secrets_findings(context)

    if context.tailscale_tag is None:
        findings.append(
            fail("CI tailnet tag", "deploy.yml sets no tailscale_tag", "#/flow-new-app")
        )
    elif TAG_PATTERN.match(context.tailscale_tag):
        findings.append(ok("CI tailnet tag", context.tailscale_tag))
    else:
        findings.append(
            fail(
                "CI tailnet tag",
                f"{context.tailscale_tag} is not tag:<name>",
                "#/flow-new-app",
            )
        )

    if context.target_host is None:
        findings.append(
            fail("Target host", "deploy.yml sets no target_host", "#/flow-new-app")
        )
    else:
        findings.append(peer_finding(tailnet, context.target_host, "Target host"))

    infra = infra_for_host(context.target_host, home) if context.target_host else None
    if infra is None:
        known = len(infras(home))
        why = (
            "no registered infrastructure lists this host"
            if known
            else "no infrastructure is registered"
        )
        findings.append(
            skip("Core pin", f"{why} — {CONFIG_HINT}", "#/flow-core-update")
        )
        findings.append(skip("Recipients", f"{why}", "#/ref-keys"))
        return findings

    infra_pin = read_collection_pin(infra.path)
    if infra_pin is None:
        findings.append(
            skip(
                "Core pin",
                f"{infra.path} has no readable requirements.yml pin",
                "#/flow-core-update",
            )
        )
    elif context.core_pin == infra_pin:
        findings.append(ok("Core pin", f"{context.core_pin} matches {infra.name}"))
    else:
        findings.append(
            fail(
                "Core pin",
                f"application uses {context.core_pin or 'no pin'}, {infra.name} pins {infra_pin}",
                "#/flow-core-update",
            )
        )

    findings.append(recipients_finding(context, infra))
    findings.append(templates_finding(context, home))
    return findings


def templates_finding(context: Context, home: Path) -> Finding:
    """Workflows and hooks are the core's files; they age with it."""
    from .. import __version__
    from .update import UpdateError, behind, gather_facts, owned_by_app, render_managed

    try:
        facts = gather_facts(context, home)
    except UpdateError as error:
        return skip("Templates", str(error), "#/flow-core-update")
    files = render_managed(facts)
    stale = behind(context.root, files)
    owned = owned_by_app(context.root, files)
    note = (
        f" · {len(owned)} owned by the application: {', '.join(owned)}" if owned else ""
    )
    if not stale:
        return ok(
            "Templates", f"workflows, hooks and .sops.yaml match v{__version__}{note}"
        )
    return fail(
        "Templates",
        f"{len(stale)} file(s) behind v{__version__} ({', '.join(stale[:3])}"
        + (" …" if len(stale) > 3 else "")
        + "); run: platform update",
        "#/flow-core-update",
    )


def recipients_finding(context: Context, infra) -> Finding:
    """Every application encrypts to the recipients the infrastructure publishes."""
    published = read_recipients(infra.path)
    if not published:
        return skip(
            "Recipients",
            f"{infra.name} has no docs/RECIPIENTS.md to compare with",
            "#/ref-keys",
        )
    sops_path = context.root / ".sops.yaml"
    if not sops_path.is_file():
        return fail(
            "Recipients",
            ".sops.yaml is missing; secrets cannot be encrypted for any host",
            "#/flow-new-app",
        )
    document = load_yaml(sops_path)
    declared = set()
    try:
        for rule in document.get("creation_rules") or []:
            for group in rule.get("key_groups") or []:
                declared.update(str(item) for item in group.get("age") or [])
    except AttributeError:
        pass
    if not declared:
        return fail(
            "Recipients", ".sops.yaml declares no age recipients", "#/flow-new-app"
        )
    host_name = (context.target_host or "").split(".")[0]
    expected = {host_recipient(published, host_name), recovery_recipient(published)} - {
        None
    }
    missing = expected - declared
    if missing:
        return fail(
            "Recipients",
            f".sops.yaml lacks {len(missing)} recipient(s) that {infra.name} publishes in RECIPIENTS.md",
            "#/ref-keys",
        )
    return ok(
        "Recipients", f".sops.yaml carries both recipients {infra.name} publishes"
    )


# ---------------------------------------------------------------- entrypoint


def diagnose(
    context: Context,
    tailnet: Tailnet,
    runner=subprocess.run,
    home: Optional[Path] = None,
    collections_root: Optional[Path] = None,
) -> list:
    home = Path.home() if home is None else home

    if context.kind == "infra":
        return diagnose_infra(context, tailnet, runner, home, collections_root)
    if context.kind == "app":
        return diagnose_app(context, tailnet, home)
    return [skip("Repository", "no infrastructure or application repository here")]


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
