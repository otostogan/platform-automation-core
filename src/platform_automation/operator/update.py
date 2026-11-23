"""Bring an application's platform-owned files up to the console's templates.

An application carries files it did not write: the three workflows, the
three hooks, ``.sops.yaml``. They were copied from the handbook by ``new app``
and drift the moment the core changes them. ``platform update`` renders the
same templates again — from what the repository already says about itself —
and shows the difference, so an operator reviews and commits a change instead
of remembering what moved.

Ownership is explicit. A file the platform manages carries a first-line
marker; a file without it belongs to the application, and ``update`` only
shows what it would have changed. Deleting the marker is how a file is taken
over. The manifests, the Compose file and ``.env.*`` are never touched.
"""

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .. import __version__
from .config import infra_for_host
from .context import Context, load_yaml, read_collection_pin
from .recipients import host_recipient, read_recipients, recovery_recipient
from .scaffold import (
    DEFAULT_PLATFORMS,
    DEPLOY_WORKFLOW,
    DESTINATIONS,
    ENVIRONMENTS,
    EXECUTABLE,
    MANAGED,
    MERGED,
    environment_options,
    is_managed,
    valid_platforms,
    merge_lines,
    render,
    sops_rules,
    strip_marker,
    target_host_value,
    template,
    with_marker,
)


class UpdateError(RuntimeError):
    pass


# ------------------------------------------------------------------ the facts


@dataclass(frozen=True)
class Facts:
    """Everything the templates need, read back from the repository."""

    project: str
    owner: str
    target_host: str
    core_pin: str
    environments: tuple
    recipient_host: str
    recipient_recovery: str
    pin_source: str  # "infra:<name>" | "deploy.yml"
    recipients_source: str  # "infra:<name>" | ".sops.yaml"
    tailscale_tag: str = (
        ""  # what deploy.yml already uses; the template's tag:ci-<project> otherwise
    )
    build_platforms: str = DEFAULT_PLATFORMS  # what build.yml already builds for
    # Per environment, when they differ: (environment, host) and
    # (environment, host key, recovery key). Empty means the single values above.
    target_hosts: tuple = ()
    recipients: tuple = ()


def find_repository(document) -> Optional[str]:
    """The first ``REPOSITORY: ghcr.io/<owner>/<project>`` in any env mapping, job or step."""
    if isinstance(document, dict):
        env = document.get("env")
        value = env.get("REPOSITORY") if isinstance(env, dict) else None
        if (
            isinstance(value, str)
            and value.startswith("ghcr.io/")
            and value.count("/") == 2
        ):
            return value
        for child in document.values():
            found = find_repository(child)
            if found:
                return found
    elif isinstance(document, list):
        for child in document:
            found = find_repository(child)
            if found:
                return found
    return None


def find_platforms(document) -> Optional[str]:
    """``PLATFORMS: linux/…`` in any env mapping of the build workflow."""
    if isinstance(document, dict):
        env = document.get("env")
        value = env.get("PLATFORMS") if isinstance(env, dict) else None
        if valid_platforms(value):
            return value
        for child in document.values():
            found = find_platforms(child)
            if found:
                return found
    elif isinstance(document, list):
        for child in document:
            found = find_platforms(child)
            if found:
                return found
    return None


def read_repository(root: Path) -> dict:
    """Owner and project from the workflows' image name, recipients from .sops.yaml."""
    from .scaffold import git_org

    facts = {"owner": None, "project": None, "recipients": ()}
    for relative in (DESTINATIONS["workflow_build.yml"], DEPLOY_WORKFLOW):
        repository = find_repository(load_yaml(root / relative))
        if repository:
            _, facts["owner"], facts["project"] = repository.split("/")
            break
    if facts["owner"] is None:
        facts["owner"] = git_org(root)

    facts["sops"] = load_yaml(root / ".sops.yaml")
    facts["recipients"] = rule_recipients(facts["sops"], None)
    return facts


def rule_recipients(sops, environment: Optional[str]) -> tuple:
    """The age recipients ``.sops.yaml`` gives an environment's secrets file.

    sops takes the first rule whose ``path_regex`` matches, and so does this.
    Without an environment: every recipient of every rule, in order.
    """
    path = f"deploy/secrets.{environment}.sops.yaml"
    declared: list = []
    try:
        for rule in sops.get("creation_rules") or []:
            keys = [
                str(item)
                for group in rule.get("key_groups") or []
                for item in group.get("age") or []
            ]
            if environment is None:
                declared += keys
                continue
            pattern = rule.get("path_regex")
            try:
                matches = pattern is None or re.search(str(pattern), path)
            except re.error:
                matches = False
            if matches:
                return tuple(keys)
    except AttributeError:
        pass
    return tuple(declared)


def pin_key(pin: str) -> tuple:
    try:
        return tuple(int(part) for part in pin.lstrip("v").split("."))
    except ValueError:
        return ()


def gather_facts(context: Context, home: Path, hosts: Optional[dict] = None) -> Facts:
    """Read the repository back; ``hosts`` moves environments to other hosts."""
    if context.kind != "app" or context.root is None:
        raise UpdateError("not inside an application repository")
    hosts = hosts or {}
    if context.target_host is None and not hosts:
        raise UpdateError(
            f"{DEPLOY_WORKFLOW} names no target_host; nothing to update from"
        )

    repository = read_repository(context.root)
    project = repository["project"] or (
        context.environments[0].project if context.environments else None
    )
    if not project or not repository["owner"]:
        raise UpdateError(
            "no ghcr.io/<owner>/<project> image in the workflows and no GitHub remote to read the owner from"
        )

    environments = tuple(
        name
        for name in ENVIRONMENTS
        if any(item.environment == name for item in context.environments)
    )
    if not environments:
        raise UpdateError("no deploy/platform.<environment>.yml found")
    unknown = sorted(set(hosts) - set(environments))
    if unknown:
        raise UpdateError(
            f"--host names {', '.join(unknown)}, which has no deploy/platform.<environment>.yml"
        )

    target_hosts, recipients = [], []
    infra_pins, sources = {}, []
    for environment in environments:
        host = hosts.get(environment) or context.host_for(environment)
        if not host:
            raise UpdateError(
                f"{DEPLOY_WORKFLOW} names no host for {environment};"
                f" say where it goes: --host {environment}=<host>"
            )
        target_hosts.append((environment, host))

        pair, source = rule_recipients(repository["sops"], environment), ".sops.yaml"
        infra = infra_for_host(host, home)
        if infra is not None:
            pin = read_collection_pin(infra.path)
            if pin:
                # two infrastructures may share a directory name, never a path
                infra_pins[str(infra.path)] = (pin, infra.name)
            published = read_recipients(infra.path)
            wanted = (
                host_recipient(published, host.split(".")[0]),
                recovery_recipient(published),
            )
            if all(wanted):
                pair, source = wanted, f"infra:{infra.name}"
        moved = environment in hosts and host != context.host_for(environment)
        if moved and source == ".sops.yaml":
            # the keys in .sops.yaml belong to the host the environment leaves
            raise UpdateError(
                f"no registered infrastructure publishes age recipients for {host},"
                f" so {environment} cannot be encrypted for it"
            )
        if len(pair) < 2:
            raise UpdateError(
                f".sops.yaml declares fewer than two age recipients for {environment}"
            )
        recipients.append((environment, pair[0], pair[1]))
        if source not in sources:
            sources.append(source)

    core_pin, pin_source = context.core_pin, "deploy.yml"
    if infra_pins:
        # the workflow must not ask any of its hosts for more than it has
        core_pin, name = min(infra_pins.values(), key=lambda item: pin_key(item[0]))
        pin_source = f"infra:{name}"
    if not core_pin:
        raise UpdateError(
            "no core pin: neither deploy.yml nor a registered infrastructure has one"
        )

    platforms = find_platforms(
        load_yaml(context.root / DESTINATIONS["workflow_build.yml"])
    )

    return Facts(
        project=project,
        owner=repository["owner"],
        build_platforms=platforms or DEFAULT_PLATFORMS,
        target_host=target_hosts[0][1],
        core_pin=core_pin,
        environments=environments,
        recipient_host=recipients[0][1],
        recipient_recovery=recipients[0][2],
        pin_source=pin_source,
        recipients_source=", ".join(sources),
        tailscale_tag=context.tailscale_tag or f"tag:ci-{project}",
        target_hosts=tuple(target_hosts),
        recipients=tuple(recipients),
    )


def console_ahead_of_hosts(core_pin: str, version: str = __version__) -> bool:
    """A workflow from a newer console may ask a host for what it lacks."""
    try:
        have = tuple(int(part) for part in version.split("."))
        pinned = tuple(int(part) for part in core_pin.lstrip("v").split("."))
    except ValueError:
        return False
    return have > pinned


# ------------------------------------------------------------------- the plan


def render_managed(facts: Facts, version: str = __version__) -> dict:
    values = {
        "project": facts.project,
        "org": facts.owner,
        "tailnet": target_host_value(facts.target_hosts or (("", facts.target_host),)),
        "core_pin": facts.core_pin,
        "recipient_host": facts.recipient_host,
        "recipient_recovery": facts.recipient_recovery,
        "platforms": facts.build_platforms,
    }
    files = {}
    for name, destination in DESTINATIONS.items():
        if destination in MANAGED:
            text = render(template(name), values)
            if destination == ".sops.yaml":
                text = sops_rules(facts.recipients) or text
            if destination == DEPLOY_WORKFLOW:
                text = environment_options(text, facts.environments)
                # the tag is the tailnet policy's business, not the template's
                text = text.replace(
                    f"tailscale_tag: tag:ci-{facts.project}",
                    f"tailscale_tag: {facts.tailscale_tag}",
                    1,
                )
            files[destination] = with_marker(text, version)
        elif destination in MERGED:
            files[destination] = render(template(name), values)
    return files


@dataclass(frozen=True)
class Change:
    path: str
    kind: str  # "create" | "update" | "adopt" | "merge" | "owned" | "same"
    current: str
    wanted: str

    @property
    def writes(self) -> bool:
        return self.kind in {"create", "update", "adopt", "merge"}

    def diff(self) -> str:
        return "".join(
            difflib.unified_diff(
                self.current.splitlines(keepends=True),
                self.wanted.splitlines(keepends=True),
                fromfile=f"a/{self.path}",
                tofile=f"b/{self.path}",
            )
        )


def plan(root: Path, files: dict) -> list:
    changes = []
    for relative, wanted in files.items():
        path = root / relative
        current = path.read_text(encoding="utf-8") if path.is_file() else None

        if relative in MERGED:
            merged, added = merge_lines(current or "", wanted)
            kind = (
                "same"
                if current is not None and not added
                else ("merge" if current else "create")
            )
            changes.append(Change(relative, kind, current or "", merged))
            continue

        if current is None:
            changes.append(Change(relative, "create", "", wanted))
        elif current == wanted:
            changes.append(Change(relative, "same", current, wanted))
        elif is_managed(current):
            changes.append(Change(relative, "update", current, wanted))
        elif current == strip_marker(wanted):
            changes.append(Change(relative, "adopt", current, wanted))
        else:
            changes.append(Change(relative, "owned", current, wanted))
    return changes


def apply(root: Path, changes: list) -> list:
    written = []
    for change in changes:
        if not change.writes:
            continue
        path = root / change.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(change.wanted, encoding="utf-8")
        if change.path in EXECUTABLE:
            path.chmod(0o755)
        written.append(change.path)
    return written


def declared_recipients(text: str) -> set:
    """The age recipients a ``.sops.yaml`` text names, in any rule or group."""
    import yaml

    try:
        document = yaml.safe_load(strip_marker(text))
    except yaml.YAMLError:
        return set()
    found = set()
    try:
        for rule in document.get("creation_rules") or []:
            for group in rule.get("key_groups") or []:
                found.update(str(item) for item in group.get("age") or [])
    except AttributeError:
        pass
    return found


def recipients_changed(changes: list) -> bool:
    """Only a different set of recipients makes the ciphertext stale.

    A new console version changes the marker line of ``.sops.yaml`` too; that
    must not re-encrypt anything. A ``.sops.yaml`` that did not exist means
    the ciphertext was made under unknown rules, so it counts as changed.
    """
    for change in changes:
        if change.path != ".sops.yaml" or not change.writes:
            continue
        if change.kind == "create":
            return True
        return declared_recipients(change.current) != declared_recipients(change.wanted)
    return False


def reencrypt_environments(changes: list, environments: tuple) -> tuple:
    """The environments whose secrets file gets different recipients."""
    import yaml

    for change in changes:
        if change.path != ".sops.yaml" or not change.writes:
            continue
        if change.kind == "create":
            return tuple(environments)
        try:
            before = yaml.safe_load(strip_marker(change.current))
            after = yaml.safe_load(strip_marker(change.wanted))
        except yaml.YAMLError:
            return tuple(environments)
        return tuple(
            name
            for name in environments
            if set(rule_recipients(before, name)) != set(rule_recipients(after, name))
        )
    return ()


def behind(root: Path, files: dict) -> list:
    """Paths ``update`` would rewrite — what ``doctor`` reports."""
    return [
        change.path
        for change in plan(root, files)
        if change.kind in {"create", "update", "adopt", "merge"}
    ]


def owned_by_app(root: Path, files: dict) -> list:
    """Files without the marker that differ — deliberate, and worth a mention."""
    return [change.path for change in plan(root, files) if change.kind == "owned"]
