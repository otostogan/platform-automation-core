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
    DEPLOY_WORKFLOW,
    DESTINATIONS,
    ENVIRONMENTS,
    EXECUTABLE,
    MANAGED,
    MERGED,
    environment_options,
    is_managed,
    merge_lines,
    render,
    strip_marker,
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


def read_repository(root: Path) -> dict:
    """Owner and project from the build workflow, recipients from .sops.yaml."""
    facts = {"owner": None, "project": None, "recipients": ()}
    document = load_yaml(root / DESTINATIONS["workflow_build.yml"])
    jobs = document.get("jobs") if isinstance(document, dict) else None
    for job in (jobs or {}).values():
        env = job.get("env") if isinstance(job, dict) else None
        repository = env.get("REPOSITORY") if isinstance(env, dict) else None
        if isinstance(repository, str) and repository.startswith("ghcr.io/"):
            parts = repository.split("/")
            if len(parts) == 3:
                facts["owner"], facts["project"] = parts[1], parts[2]

    sops = load_yaml(root / ".sops.yaml")
    declared = []
    try:
        for rule in sops.get("creation_rules") or []:
            for group in rule.get("key_groups") or []:
                declared += [str(item) for item in group.get("age") or []]
    except AttributeError:
        pass
    facts["recipients"] = tuple(declared)
    return facts


def gather_facts(context: Context, home: Path) -> Facts:
    if context.kind != "app" or context.root is None:
        raise UpdateError("not inside an application repository")
    if context.target_host is None:
        raise UpdateError(
            f"{DEPLOY_WORKFLOW} names no target_host; nothing to update from"
        )

    repository = read_repository(context.root)
    project = repository["project"] or (
        context.environments[0].project if context.environments else None
    )
    if not project or not repository["owner"]:
        raise UpdateError(
            ".github/workflows/build.yml names no ghcr.io/<owner>/<project> image"
        )

    environments = tuple(
        name
        for name in ENVIRONMENTS
        if any(item.environment == name for item in context.environments)
    )
    if not environments:
        raise UpdateError("no deploy/platform.<environment>.yml found")

    infra = infra_for_host(context.target_host, home)
    core_pin, pin_source = context.core_pin, "deploy.yml"
    recipients, recipients_source = repository["recipients"], ".sops.yaml"

    if infra is not None:
        pin = read_collection_pin(infra.path)
        if pin:
            core_pin, pin_source = pin, f"infra:{infra.name}"
        published = read_recipients(infra.path)
        host_name = context.target_host.split(".")[0]
        wanted = (host_recipient(published, host_name), recovery_recipient(published))
        if all(wanted):
            recipients, recipients_source = wanted, f"infra:{infra.name}"

    if not core_pin:
        raise UpdateError(
            "no core pin: neither deploy.yml nor a registered infrastructure has one"
        )
    if len(recipients) < 2:
        raise UpdateError(".sops.yaml declares fewer than two age recipients")

    return Facts(
        project=project,
        owner=repository["owner"],
        target_host=context.target_host,
        core_pin=core_pin,
        environments=environments,
        recipient_host=recipients[0],
        recipient_recovery=recipients[1],
        pin_source=pin_source,
        recipients_source=recipients_source,
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
        "tailnet": facts.target_host,
        "core_pin": facts.core_pin,
        "recipient_host": facts.recipient_host,
        "recipient_recovery": facts.recipient_recovery,
    }
    files = {}
    for name, destination in DESTINATIONS.items():
        if destination in MANAGED:
            text = render(template(name), values)
            if destination == DEPLOY_WORKFLOW:
                text = environment_options(text, facts.environments)
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
