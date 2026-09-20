"""The interactive front door: ``platform`` with no arguments.

Every choice ends in something that already exists — a host command with
``--json``, a playbook, a ``gh`` dispatch. The command is always printed
before it runs; a choice that is not wired yet prints only the command, so
what the console *would* do is never a guess.
"""

import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from .. import __version__
from ..validate_manifest import (
    DEFAULT_SCHEMA,
    load_json,
    load_yaml,
    resolve_compose_path,
    validate_compose,
    validate_manifest,
)
from .context import Context, detect
from .doctor import diagnose
from .remote import run_platform
from .tailnet import read_tailnet

INSTALL_HINT = "pip install 'platform-automation-runtime[operator]'"
HANDBOOK = "docs/handbook.html"

CYAN = "\033[36m"
GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


class ConsoleUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Action:
    label: str
    command: str
    run: Optional[Callable[[], int]] = None
    anchor: str = ""
    remote: bool = False


def explain_host_error(error: str, core_pin: Optional[str]) -> Optional[str]:
    """Turn a refusal the host printed into the fact behind it, when we know it."""
    if "invalid choice:" in error and "argument command" in error:
        pinned = f"; the infrastructure pins {core_pin}" if core_pin else ""
        return (
            f"the host runs a core older than this console ({__version__}) and "
            f"does not have that command yet{pinned}. Move the pin and converge."
        )
    if "Permission denied (publickey" in error:
        return "the host did not accept your SSH key; check the inventory key path with: platform doctor"
    if "Could not resolve hostname" in error:
        return "MagicDNS did not resolve the name; check Tailscale on this machine with: platform doctor"
    return None


def tailnet_gate(tailnet) -> Optional[str]:
    """What stops a remote action before ssh is even tried; None when nothing does."""
    if not tailnet.available:
        return f"Tailscale is not available here: {tailnet.error}"
    if tailnet.backend_state != "Running":
        return (
            f"Tailscale on this machine is {tailnet.backend_state}; MagicDNS names "
            "do not resolve until it runs. Start it (`tailscale up`) and retry."
        )
    return None


def load_prompts():
    """Import the prompt toolkit lazily: hosts install the wheel without it."""
    try:
        import questionary
        from questionary import Style
    except ImportError as error:
        raise ConsoleUnavailable(
            "interactive prompts need the operator extra: " + INSTALL_HINT
        ) from error

    style = Style(
        [
            ("qmark", "fg:cyan bold"),
            ("question", "bold"),
            ("answer", "fg:cyan bold"),
            ("pointer", "fg:cyan bold"),
            ("highlighted", "fg:cyan bold"),
            ("selected", "fg:cyan"),
            ("instruction", "fg:#888888"),
        ]
    )
    return questionary, style


# ------------------------------------------------------------------ rendering


def banner(context: Context, stream=sys.stdout) -> None:
    where = {
        "host": "on a platform host",
        "infra": "infrastructure repository",
        "app": "application repository",
        "nowhere": "no repository here",
    }[context.kind]

    print(f"{BOLD}platform{RESET} {DIM}{__version__}{RESET} · {where}", file=stream)

    if context.kind == "infra":
        names = ", ".join(host.name for host in context.hosts) or "no hosts yet"
        pin = context.core_pin or "no core pin"
        print(f"{DIM}{context.root} · {names} · core {pin}{RESET}", file=stream)
    elif context.kind == "app":
        scopes = ", ".join(f"{s.project}/{s.environment}" for s in context.environments)
        target = context.target_host or "target host unknown"
        pin = context.core_pin or "no core pin"
        print(
            f"{DIM}{context.root} · {scopes} · {target} · core {pin}{RESET}",
            file=stream,
        )
    elif context.kind == "nowhere":
        print(f"{DIM}{context.root}{RESET}", file=stream)

    print(file=stream)


def render_status(document: dict) -> str:
    """The same facts ``platform status`` prints, arranged for a glance."""
    lines = [
        f"{BOLD}{document.get('project')}/{document.get('environment')}{RESET}  "
        f"{DIM}{document.get('release_count', 0)} release record(s){RESET}"
    ]

    current = document.get("current")
    if not current:
        lines.append("  current release: none")
    else:
        health = (current.get("healthcheck") or {}).get("status")
        migration = (current.get("migration") or {}).get("status")
        lines.append(
            f"  current release: {current.get('release_tag')}  "
            f"status={current.get('status')}  healthcheck={health}  migration={migration}"
        )

    backups = document.get("backups")
    if backups:
        lines.append(
            f"  backups: {backups.get('count', 0)}  latest={backups.get('latest') or 'none'}"
        )
        window = backups.get("loss_window") or {}
        if window.get("overdue"):
            lines.append(
                f"  {RED}loss window: unknown — the schedule has stopped producing dumps{RESET}"
            )
        elif window.get("newest_age_minutes") is not None:
            lines.append(
                f"  loss window now: up to {window['newest_age_minutes']} minute(s)"
            )
        verified = backups.get("last_verified")
        if verified:
            lines.append(
                f"  last proven restorable: {verified.get('outcome')} on {verified.get('stamp')}"
            )
        else:
            lines.append("  last proven restorable: never")
        offsite = backups.get("offsite") or {}
        if offsite.get("state"):
            lines.append(f"  offsite: {offsite['state']}")

    return "\n".join(lines)


def render_projects(document: dict) -> str:
    entries = document.get("projects") or []
    if not entries:
        return "No projects on this host"

    lines = [
        f"{'PROJECT':<24} {'ENV':<11} {'RELEASE':<22} {'STATUS':<11} {'HEALTH':<10} RECORDS"
    ]
    for entry in entries:
        shown = entry.get("current") or entry.get("latest")
        release = shown["release_tag"] if shown else "-"
        status = shown["status"] if shown else "none"
        health = shown.get("healthcheck", "-") if shown else "-"
        healthy = status == "deployed" and health == "succeeded"
        colour = GREEN if healthy else (RED if shown else DIM)
        lines.append(
            f"{entry['project']:<24} {entry['environment']:<11} {release:<22} "
            f"{colour}{status:<11}{RESET} {health:<10} {entry.get('release_count', 0)}"
        )
    return "\n".join(lines)


def render_backups(document: dict) -> str:
    entries = document.get("backups") or []
    if not entries:
        return f"No backups for {document.get('project')}/{document.get('environment')}"

    lines = [
        f"{'STAMP':<26} {'REASON':<14} {'SIZE':>10}  {'RELEASE':<22} {'OFF':<4} VERIFIED"
    ]
    for entry in entries:
        stamp = str(entry.get("stamp", "")).split("-", 1)[0]
        size = "-" if entry.get("bytes") is None else str(entry["bytes"])
        offsite = {None: "n/a", True: "yes", False: "NO"}.get(
            entry.get("offsite"), "n/a"
        )
        lines.append(
            f"{stamp:<26} {(entry.get('reason') or '-'):<14} {size:>10}  "
            f"{(entry.get('release_tag') or '-'):<22} {offsite:<4} "
            f"{'yes' if entry.get('verified') else ''}"
        )
    return "\n".join(lines)


# -------------------------------------------------------------------- actions


def report_failure(error: str, core_pin: Optional[str]) -> int:
    print(f"{RED}{error}{RESET}")
    reason = explain_host_error(error, core_pin)
    if reason:
        print(f"  {reason}")
        anchor = "#/flow-core-update" if "older" in reason else "#/flow-incidents"
        print(f"{DIM}  handbook: {HANDBOOK}{anchor}{RESET}")
    return 1


def remote_action(
    label: str,
    host: str,
    user: str,
    arguments: list,
    render,
    anchor: str,
    identity=None,
    core_pin=None,
) -> Action:
    shown = f"ssh {user}@{host} 'sudo -n platform {' '.join(arguments)} --json'"

    def run() -> int:
        result = run_platform(host, user, arguments, identity=identity)
        if not result.ok:
            return report_failure(result.error, core_pin)
        print(render(result.document))
        return 0

    return Action(label, shown, run, anchor, remote=True)


def validate_action(root: Path, manifest_path: Path) -> Action:
    def run() -> int:
        document = load_yaml(root / manifest_path)
        errors = validate_manifest(document, load_json(DEFAULT_SCHEMA))
        if not errors:
            compose = load_yaml(resolve_compose_path(root, document["compose_file"]))
            errors = validate_compose(document, compose)
        if errors:
            print(f"{RED}invalid application contract: {manifest_path}{RESET}")
            for error in errors:
                print(f"  - {error}")
            return 1
        print(f"{GREEN}valid application contract: {manifest_path}{RESET}")
        return 0

    return Action(
        "Validate manifest and Compose",
        f"platform-validate-manifest {manifest_path}",
        run,
        "#/ref-manifest",
    )


def host_connection(host) -> tuple:
    user = host.user or "ops"
    address = host.address or host.name
    identity = Path(host.key_file).expanduser() if host.key_file else None
    return address, user, identity


def fetch_scopes(host, core_pin=None) -> list:
    """Ask the host which projects it holds; an empty answer is not an error."""
    address, user, identity = host_connection(host)
    result = run_platform(address, user, ["projects"], identity=identity)
    if not result.ok:
        report_failure(result.error, core_pin)
        return []
    return [
        (entry["project"], entry["environment"])
        for entry in result.document.get("projects") or []
    ]


def scoped_action(host, prompts, label, verb, render, anchor, core_pin=None) -> Action:
    """A host action that first asks which project and environment it is about."""
    address, user, identity = host_connection(host)
    questionary, style = prompts

    def run() -> int:
        scopes = fetch_scopes(host, core_pin)
        if not scopes:
            print("The host reports no projects; nothing to select.")
            return 1
        project, environment = choose(
            questionary, style, "Project", scopes, lambda s: f"{s[0]}/{s[1]}"
        )
        arguments = [verb, "--project", project, "--environment", environment]
        shown = f"ssh {user}@{address} 'sudo -n platform {' '.join(arguments)} --json'"
        print(f"{DIM}→ running:{RESET}  {shown}")
        result = run_platform(address, user, arguments, identity=identity)
        if not result.ok:
            return report_failure(result.error, core_pin)
        print(render(result.document))
        return 0

    shown = f"ssh {user}@{address} 'sudo -n platform {verb} --project … --environment … --json'"
    return Action(label, shown, run, anchor, remote=True)


def host_actions(context: Context, host, prompts=None) -> list:
    address, user, identity = host_connection(host)
    ssh = f"ssh {user}@{address}"
    actions = [
        remote_action(
            "Status of every project",
            address,
            user,
            ["projects"],
            render_projects,
            "#/ref-cli",
            identity,
            core_pin=context.core_pin,
        ),
    ]
    if prompts is not None:
        actions += [
            scoped_action(
                host,
                prompts,
                "Project: status",
                "status",
                render_status,
                "#/flow-deploy",
            ),
            scoped_action(
                host,
                prompts,
                "Project: backups",
                "backups",
                render_backups,
                "#/flow-backups",
            ),
        ]
    actions += [
        Action(
            "Backups: take one now",
            f"{ssh} 'sudo -n platform backup --project <p> --environment <e> --json'",
            None,
            "#/flow-backups",
        ),
        Action(
            "Backups: prove restorable",
            f"{ssh} 'sudo -n platform verify-backup --project <p> --environment <e> --json'",
            None,
            "#/flow-backups",
        ),
        Action(
            "Converge (twice)",
            ".venv/bin/ansible-playbook otostogan.platform.converge --inventory inventory/hosts.yml",
            None,
            "#/flow-new-host",
        ),
        Action(
            "Readiness",
            ".venv/bin/ansible-playbook otostogan.platform.readiness --inventory inventory/hosts.yml",
            None,
            "#/flow-new-host",
        ),
    ]
    return actions


def deploy_command(dispatch_inputs: tuple, environment: str) -> str:
    """The dispatch the application's own workflow declares, input by input.

    Workflows differ — one takes ``environment`` and ``ref``, another takes an
    image reference and a release tag — so the names come from the file, and
    only the environment is filled in when the workflow asks for one.
    """
    if not dispatch_inputs:
        return "gh workflow run deploy.yml  (deploy.yml declares no workflow_dispatch inputs)"
    parts = ["gh", "workflow", "run", "deploy.yml"]
    for name in dispatch_inputs:
        value = environment if name == "environment" else f"<{name}>"
        parts += ["-f", f"{name}={value}"]
    return " ".join(parts)


def deploy_action(context: Context, scope, prompts) -> Action:
    """Dispatch the application's Deploy workflow and watch the run it started."""
    from .deploy import (
        DeployError,
        current_branch,
        dispatch,
        dispatch_arguments,
        find_run,
        list_run_ids,
        release_tags,
        watch,
    )

    root = context.root
    ident = ["--project", scope.project, "--environment", scope.environment]

    def run() -> int:
        questionary, style = prompts
        text, select = make_prompts(questionary, style)
        from .wizard import BACK, CANCEL

        branch = current_branch(root)
        inputs = context.dispatch_inputs
        if not inputs:
            print(
                f"{RED}deploy.yml declares no workflow_dispatch inputs; nothing to dispatch{RESET}"
            )
            return 1

        ref = ""
        if "ref" in inputs:
            tags = release_tags(root)
            LATEST, OTHER = "latest release", "another ref…"
            chosen = select(
                "What to deploy",
                [LATEST, *tags, OTHER],
                lambda v: {
                    LATEST: "latest release (ref left empty)",
                    OTHER: "type a tag, branch or commit",
                }.get(v, v),
            )
            if chosen in (BACK, CANCEL):
                print("cancelled — nothing dispatched")
                return 130
            if chosen == OTHER:
                chosen = text("Ref", "", lambda v: bool(v.strip()) or "required")
                if chosen in (BACK, CANCEL):
                    print("cancelled — nothing dispatched")
                    return 130
            ref = "" if chosen == LATEST else chosen.strip()

        if branch:
            answer = text(
                "Run the workflow from branch (the tailnet credential is bound to one)",
                branch,
                lambda v: bool(v.strip()) or "required",
            )
            if answer in (BACK, CANCEL):
                print("cancelled — nothing dispatched")
                return 130
            branch = answer.strip()

        arguments = dispatch_arguments(inputs, scope.environment, ref, "", branch)
        print()
        print(f"{DIM}→ gh {' '.join(arguments)}{RESET}")
        if scope.environment == "production":
            typed = questionary.text(
                "This is production. Type the environment name to confirm",
                style=style,
            ).ask()
            if typed != "production":
                print("cancelled — nothing dispatched")
                return 130
        elif not questionary.confirm("Dispatch?", default=True, style=style).ask():
            print("cancelled — nothing dispatched")
            return 130

        try:
            known = list_run_ids(root, branch)
            dispatch(root, arguments)
            print(f"{GREEN}dispatched{RESET} — waiting for the run to appear…")
            started = find_run(root, branch, known)
            print(f"{DIM}  {started.url}{RESET}")
            print()
            code = watch(root, started)
        except DeployError as error:
            print(f"{RED}{error}{RESET}")
            return 1

        print()
        if code == 0:
            print(f"{GREEN}deployment succeeded{RESET}")
        else:
            print(
                f"{RED}deployment failed — see the run above; the previous release keeps serving{RESET}"
            )
            print(f"{DIM}  handbook: {HANDBOOK}#/flow-incidents{RESET}")
        if context.target_host:
            status = remote_action(
                "Status on the host",
                context.target_host,
                "ops",
                ["status", *ident],
                render_status,
                "#/flow-deploy",
                core_pin=context.core_pin,
            )
            print()
            print(f"{DIM}→ {status.command}{RESET}")
            status.run()
        return code

    return Action(
        "Deploy",
        "gh workflow run deploy.yml … (ref and branch are asked first)",
        run,
        "#/flow-deploy",
    )


def secrets_action(context: Context, environment: str, verb: str) -> Action:
    """push: encrypt .env.<env> into the ciphertext (what the hook does); pull: read it back."""
    argv = [verb, environment]
    label = (
        f"Secrets: push .env.{environment} → ciphertext"
        if verb == "push"
        else f"Secrets: pull ciphertext → .env.{environment} (needs a key)"
    )

    def run() -> int:
        return run_secrets(context, argv)

    return Action(label, f"platform secrets {' '.join(argv)}", run, "#/flow-new-app")


def app_actions(context: Context, scope, prompts=None) -> list:
    target = context.target_host or "<target host>"
    ident = ["--project", scope.project, "--environment", scope.environment]
    actions = [
        (
            deploy_action(context, scope, prompts)
            if prompts
            else Action(
                "Deploy",
                deploy_command(context.dispatch_inputs, scope.environment),
                None,
                "#/flow-deploy",
            )
        ),
        remote_action(
            "Status on the host",
            target,
            "ops",
            ["status", *ident],
            render_status,
            "#/flow-deploy",
        ),
        remote_action(
            "Backups: list",
            target,
            "ops",
            ["backups", *ident],
            render_backups,
            "#/flow-backups",
        ),
        validate_action(context.root, scope.manifest),
        secrets_action(context, scope.environment, "push"),
        secrets_action(context, scope.environment, "pull"),
    ]
    if context.target_host is None:
        actions = [
            a for a in actions if a.run is None or a.label.startswith("Validate")
        ]
    return actions


def perform(action: Action) -> int:
    print()
    if action.run is None:
        print(f"{DIM}→ this will run:{RESET}")
        print(f"  {action.command}")
        print(f"{DIM}  (not wired yet — shown so the console never guesses){RESET}")
        return 0

    if action.remote:
        blocked = tailnet_gate(read_tailnet())
        if blocked:
            print(f"{RED}{blocked}{RESET}")
            print(
                f"{DIM}  handbook: {HANDBOOK}#/flow-incidents · or run: platform doctor{RESET}"
            )
            return 1

    print(f"{DIM}→ running:{RESET}  {action.command}")
    print()
    code = action.run()
    if code == 0 and action.anchor:
        print(f"{DIM}  handbook: {HANDBOOK}{action.anchor}{RESET}")
    return code


# ---------------------------------------------------------------------- menus


def choose(questionary, style, message: str, options: list, describe: Callable):
    answer = questionary.select(
        message,
        choices=[
            questionary.Choice(title=describe(item), value=item) for item in options
        ],
        style=style,
        pointer="»",
        instruction="(↑↓ to move, enter to select)",
    ).ask()

    if answer is None:  # Ctrl-C or Esc
        raise KeyboardInterrupt

    return answer


EXIT = object()
BACK = object()


def choose_scope(context: Context, questionary, style):
    """The first question: which host, or which environment. None means exit."""
    if context.kind == "infra":
        options = [*context.hosts, EXIT]
        answer = choose(
            questionary,
            style,
            "Host",
            options,
            lambda h: "Exit" if h is EXIT else h.name,
        )
    else:
        options = [*context.environments, EXIT]
        answer = choose(
            questionary,
            style,
            "Environment",
            options,
            lambda s: "Exit" if s is EXIT else f"{s.project}/{s.environment}",
        )
    return None if answer is EXIT else answer


def run_menu(context: Context) -> int:
    """Stay in the console: after an action, return to the first question.

    An operator rarely wants exactly one thing; leaving after every action
    meant re-entering the context each time. Esc, Ctrl-C or the Exit item end
    the session.
    """
    questionary, style = load_prompts()

    if context.kind == "infra" and not context.hosts:
        print("The inventory lists no hosts yet. Start with: platform new host")
        return 1
    if context.kind == "app" and not context.environments:
        print(
            "No platform/v1 manifest found under deploy/. Start with: platform new app"
        )
        return 1
    if context.kind not in ("infra", "app"):
        print("Nothing to operate here. Start with: platform new")
        return 1

    while True:
        scope = choose_scope(context, questionary, style)
        if scope is None:
            return 0

        if context.kind == "infra":
            actions = host_actions(context, scope, prompts=(questionary, style))
        else:
            actions = app_actions(context, scope, prompts=(questionary, style))

        while True:
            action = choose(
                questionary,
                style,
                "Action",
                [*actions, BACK],
                lambda a: "← Back" if a is BACK else a.label,
            )
            if action is BACK:
                break
            perform(action)
            print()
            break  # back to the first question, not to this menu


NEW_TARGETS = [
    (
        "company-infra",
        "Company infrastructure repository — inventory, pins, key layout",
    ),
    ("host", "Add a host to this infrastructure repository"),
    ("app", "Prepare an application — deploy/, workflows, encrypted secrets"),
]


def make_prompts(questionary, style) -> tuple:
    """``text`` and ``select`` with the wizard's back/cancel conventions."""
    from .wizard import BACK, CANCEL, is_back

    def text(message, default="", validate=None):
        # The previous value is shown, not pre-typed: a pre-filled buffer is
        # how a new answer gets glued onto the old one. Empty Enter keeps it.
        default = "" if default is None else str(default)
        keeps = f"Enter keeps {default} · " if default else ""

        def check(value):
            if is_back(value) or (value == "" and default):
                return True
            return validate(value) if validate else True

        answer = questionary.text(
            message,
            validate=check,
            style=style,
            instruction=f"({keeps}< to go back)",
        ).ask()
        if answer is None:
            return CANCEL
        if is_back(answer):
            return BACK
        return default if answer == "" and default else answer

    def select(message, options, describe):
        answer = questionary.select(
            message,
            choices=[questionary.Choice(title=describe(o), value=o) for o in options]
            + [questionary.Choice(title="← Back", value=BACK)],
            style=style,
            pointer="»",
            instruction="(↑↓ to move, enter to select)",
        ).ask()
        return CANCEL if answer is None else answer

    return text, select


def review_screen(questionary, style, steps, shown, question):
    """Show every answer, offer to change one, or confirm. None means confirm."""
    from .wizard import CANCEL

    WRITE = object()

    def review(state):
        print()
        print(f"{BOLD}Review{RESET}")
        for step in steps:
            if step.applies(state) and step.key in state:
                print(f"  {step.label:<22} {shown(state, step)}")
        options = [s for s in steps if s.applies(state) and s.key in state]
        # A choice whose value is None would be indistinguishable from Ctrl-C
        # (ask() returns None for both), so "write" is a sentinel of its own.
        answer = questionary.select(
            question,
            choices=[questionary.Choice(title="Yes, write", value=WRITE)]
            + [
                questionary.Choice(title=f"Change: {s.label}", value=s.key)
                for s in options
            ]
            + [questionary.Choice(title="Cancel", value=CANCEL)],
            style=style,
            pointer="»",
        ).ask()
        if answer is None:
            return CANCEL
        return None if answer is WRITE else answer

    return review


def ask_app(context: Context, questionary, style) -> "AppAnswers":
    """Ask only what cannot be read; show what was; let every answer be revisited."""
    from .config import infras as registered_infras
    from .context import read_collection_pin, read_hosts
    from .doctor import age_recipient, host_secret_path
    from .recipients import host_recipient, read_recipients, recovery_recipient
    from .scaffold import (
        AppAnswers,
        DOMAIN_PATTERN,
        ENVIRONMENTS,
        PROJECT_PATTERN,
        SECRET_NAME_PATTERN,
        git_org,
    )
    from .wizard import BACK, CANCEL, Step, is_back, run_wizard

    root = context.root
    hint = "(< to go back)"

    text, select = make_prompts(questionary, style)

    def number(message, key, default, low, high):
        def ask(state):
            answer = text(
                message,
                state.get(key, default),
                lambda v: (v.isdigit() and low <= int(v) <= high) or f"{low}–{high}",
            )
            return answer if answer in (BACK, CANCEL) else int(answer)

        return ask

    # -------------------------------------------------------------- steps
    def ask_project(state):
        return text(
            "Project",
            state.get("project")
            or (root.name if PROJECT_PATTERN.match(root.name) else ""),
            lambda v: bool(PROJECT_PATTERN.match(v))
            or "lowercase, digits and dashes, 2–63 chars",
        )

    def ask_owner(state):
        return text(
            "ghcr.io owner (org)",
            state.get("owner") or git_org(root) or "",
            lambda v: bool(v) or "required",
        )

    def ask_environments(state):
        chosen = set(state.get("environments") or ("lab", "production"))
        answer = questionary.checkbox(
            "Environments",
            choices=[questionary.Choice(e, checked=e in chosen) for e in ENVIRONMENTS],
            style=style,
            instruction="(space to toggle, enter to confirm)",
        ).ask()
        if answer is None:
            return CANCEL
        return tuple(answer) if answer else BACK

    def ask_domain(environment):
        def ask(state):
            return text(
                f"Domain for {environment}",
                state.get(f"domain:{environment}", ""),
                lambda v: bool(DOMAIN_PATTERN.match(v))
                or "lowercase, at least one dot",
            )

        return ask

    def env_applies(environment):
        return lambda state: environment in (state.get("environments") or ())

    def ask_host(state):
        """Infrastructure → host → target, recipients and pin; or typed."""
        known = registered_infras()
        if not known:
            print(
                f"{DIM}  no infrastructure registered (platform infra add <path>) — asking instead{RESET}"
            )
            return {}
        infra = (
            known[0]
            if len(known) == 1
            else select("Infrastructure", known, lambda i: f"{i.name}  {i.path}")
        )
        if infra in (BACK, CANCEL):
            return infra
        hosts = read_hosts(infra.path)
        if not hosts:
            print(f"{DIM}  {infra.name} lists no hosts yet — asking instead{RESET}")
            return {}
        host = select(
            "Target host", list(hosts), lambda h: f"{h.name}  {h.address or ''}"
        )
        if host in (BACK, CANCEL):
            return host
        published = read_recipients(infra.path)
        recipient_host = host_recipient(published, host.name) or ""
        if not recipient_host:
            key_path = host_secret_path(infra.path, host, "secrets_age_key_source")
            if key_path is not None and key_path.is_file():
                recipient_host = age_recipient(key_path, subprocess.run) or ""
        derived = {
            "target_host": host.address or host.name,
            "recipient_host": recipient_host,
            "recipient_recovery": recovery_recipient(published) or "",
            "core_pin": read_collection_pin(infra.path),
        }
        print(
            f"{DIM}  {infra.name}: host {derived['target_host']} · recipients "
            f"{'host ✓' if derived['recipient_host'] else 'host ?'} "
            f"{'recovery ✓' if derived['recipient_recovery'] else 'recovery ?'}"
            f" · core {derived['core_pin'] or '?'}{RESET}"
        )
        return derived

    def derived(state, key):
        return (state.get("infra") or {}).get(key) or ""

    def ask_target(state):
        return text(
            "Target host (MagicDNS name)",
            state.get("target_host", ""),
            lambda v: bool(v) or "required",
        )

    def ask_recipient(label, key):
        def ask(state):
            return text(
                f"{label} age recipient (age1…)",
                state.get(key, ""),
                lambda v: v.startswith("age1") or "age1…",
            )

        return ask

    def ask_database(state):
        return select(
            "Database",
            ["docker", "external"],
            lambda m: (
                "platform-owned (docker)"
                if m == "docker"
                else "external — the platform will not back it up"
            ),
        )

    def ask_major(state):
        return select("PostgreSQL major", [18, 17, 16], str)

    def ask_schedule(state):
        return select(
            "Scheduled backups",
            [True, False],
            lambda on: (
                "yes — a timer takes dumps"
                if on
                else "no — only the dump before each migration"
            ),
        )

    def ask_query(state):
        return text("Restore validation query", state.get("restore_query", "SELECT 1"))

    def ask_secret_names(state):
        answer = text(
            "Secret names, comma-separated (values come later via sops)",
            ", ".join(state.get("secret_names") or ("API_TOKEN", "SESSION_SECRET")),
            lambda v: all(
                SECRET_NAME_PATTERN.match(n.strip()) for n in v.split(",") if n.strip()
            )
            or "environment variable names",
        )
        if answer in (BACK, CANCEL):
            return answer
        return tuple(n.strip() for n in answer.split(",") if n.strip())

    steps = [
        Step("project", ask_project, label="Project"),
        Step("owner", ask_owner, label="ghcr.io owner"),
        Step("environments", ask_environments, label="Environments"),
        *[
            Step(f"domain:{e}", ask_domain(e), env_applies(e), label=f"Domain for {e}")
            for e in ENVIRONMENTS
        ],
        Step("infra", ask_host, label="Infrastructure and host"),
        Step(
            "target_host",
            ask_target,
            lambda st: not derived(st, "target_host"),
            label="Target host",
        ),
        Step(
            "recipient_host",
            ask_recipient("Host", "recipient_host"),
            lambda st: not derived(st, "recipient_host"),
            label="Host recipient",
        ),
        Step(
            "recipient_recovery",
            ask_recipient("Recovery", "recipient_recovery"),
            lambda st: not derived(st, "recipient_recovery"),
            label="Recovery recipient",
        ),
        Step(
            "internal_port",
            number("Internal port", "internal_port", 3000, 1, 65535),
            label="Internal port",
        ),
        Step(
            "healthcheck_path",
            lambda st: text(
                "Healthcheck path",
                st.get("healthcheck_path", "/"),
                lambda v: v.startswith("/") or "starts with /",
            ),
            label="Healthcheck path",
        ),
        Step(
            "healthcheck_timeout",
            number("Healthcheck timeout, seconds", "healthcheck_timeout", 120, 1, 3600),
            label="Healthcheck timeout",
        ),
        Step("database_mode", ask_database, label="Database"),
        Step("postgres_major", ask_major, label="PostgreSQL major"),
        Step(
            "backup_enabled",
            ask_schedule,
            lambda st: st.get("database_mode") == "docker",
            label="Scheduled backups",
        ),
        Step(
            "backup_interval",
            number("Backup every N minutes", "backup_interval", 15, 15, 1440),
            lambda st: st.get("database_mode") == "docker"
            and st.get("backup_enabled", True),
            label="Backup interval",
        ),
        Step(
            "backup_retain",
            number("Keep N dumps locally", "backup_retain", 3, 1, 100),
            lambda st: st.get("database_mode") == "docker"
            and st.get("backup_enabled", True),
            label="Dumps to keep",
        ),
        Step("restore_query", ask_query, label="Restore query"),
        Step("secret_names", ask_secret_names, label="Secret names"),
    ]

    def shown(state, step):
        value = state.get(step.key)
        if step.key == "infra":
            return f"{derived(state, 'target_host') or 'typed below'} · core {derived(state, 'core_pin') or '—'}"
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, (tuple, list)):
            return ", ".join(str(v) for v in value)
        return "" if value is None else str(value)

    review = review_screen(questionary, style, steps, shown, "Write these files?")
    state = run_wizard(steps, review=review)
    infra = state.get("infra") or {}
    environments = state["environments"]
    return AppAnswers(
        project=state["project"],
        owner=state["owner"],
        environments=environments,
        domains={e: state[f"domain:{e}"] for e in environments},
        target_host=infra.get("target_host") or state.get("target_host", ""),
        recipient_host=infra.get("recipient_host") or state.get("recipient_host", ""),
        recipient_recovery=infra.get("recipient_recovery")
        or state.get("recipient_recovery", ""),
        internal_port=state["internal_port"],
        healthcheck_path=state["healthcheck_path"],
        healthcheck_timeout=state["healthcheck_timeout"],
        database_mode=state["database_mode"],
        backup_enabled=state.get("backup_enabled", True),
        postgres_major=state["postgres_major"],
        backup_interval=state.get("backup_interval", 15),
        backup_retain=state.get("backup_retain", 3),
        restore_query=state["restore_query"],
        secret_names=state["secret_names"],
        **({"core_pin": infra["core_pin"]} if infra.get("core_pin") else {}),
    )


def host_steps(
    questionary, style, text, known: dict, published: dict, suffix, prefix=""
):
    """The questions a host needs; shared by ``new host`` and ``new company-infra``.

    ``known`` holds what an existing host already answered (or Nones), and the
    recovery question is asked only when nothing is published yet.
    """
    from .hosts import HOST_PATTERN, INTERFACE_PATTERN, IPV4_PATTERN
    from .scaffold import DOMAIN_PATTERN
    from .wizard import CANCEL, Step

    def ask_name(state):
        return text(
            f"{prefix}Host name",
            state.get("name", ""),
            lambda v: bool(HOST_PATTERN.match(v)) or "lowercase, digits and dashes",
        )

    def ask_public(state):
        return text(
            "Public address from the provider",
            state.get("public_address", ""),
            lambda v: bool(IPV4_PATTERN.match(v) or DOMAIN_PATTERN.match(v))
            or "IPv4 or hostname",
        )

    def ask_tailnet(state):
        default = state.get("tailnet") or (
            f"{state['name']}.{suffix}" if suffix else ""
        )
        return text(
            "Tailnet address (MagicDNS name)",
            default,
            lambda v: bool(DOMAIN_PATTERN.match(v)) or "lowercase, at least one dot",
        )

    def ask_interface(state):
        return text(
            "Public network interface",
            state.get("interface") or known.get("interface") or "eth0",
            lambda v: bool(INTERFACE_PATTERN.match(v))
            or "an interface name such as eth0",
        )

    def ask_ssh(state):
        default = (
            state.get("ssh_key")
            or known.get("ssh_key")
            or f"~/.ssh/{state['name']}-ops"
        )
        return text(
            "Operator SSH private key", default, lambda v: bool(v) or "required"
        )

    def ask_keys_dir(state):
        return text(
            "Directory for age keys",
            state.get("keys_dir") or known.get("keys_dir") or "~/.config/platform-keys",
            lambda v: bool(v) or "required",
        )

    def ask_recovery(state):
        if published.get("recovery"):
            return False
        answer = questionary.confirm(
            "No recovery recipient is published yet. Generate the company recovery key now?",
            default=True,
            style=style,
        ).ask()
        return CANCEL if answer is None else answer

    return [
        Step("name", ask_name, label="Host"),
        Step("public_address", ask_public, label="Public address"),
        Step("tailnet", ask_tailnet, label="Tailnet address"),
        Step("interface", ask_interface, label="Interface"),
        Step("ssh_key", ask_ssh, label="SSH key"),
        Step("keys_dir", ask_keys_dir, label="Keys dir"),
        Step("recovery", ask_recovery, label="Recovery key"),
    ]


def host_from_state(state: dict, offsite: bool) -> "HostAnswers":
    from .hosts import HostAnswers

    return HostAnswers(
        name=state["name"],
        public_address=state["public_address"],
        tailnet=state["tailnet"],
        interface=state["interface"],
        ssh_key=state["ssh_key"],
        keys_dir=state["keys_dir"],
        offsite=offsite,
        recovery_needed=bool(state.get("recovery")),
    )


def tailnet_suffix(fallback=None):
    from .tailnet import read_tailnet

    if fallback:
        return fallback
    tailnet = read_tailnet()
    if tailnet.self_dns and "." in tailnet.self_dns:
        return tailnet.self_dns.rstrip(".").split(".", 1)[1]
    return None


def shown_host(state, step):
    if step.key == "recovery":
        return "generate now" if state["recovery"] else "already published"
    return state[step.key]


def ask_new_host(context: Context, questionary, style) -> "HostAnswers":
    """Two age keys are made, not asked; the rest defaults to what the last host answered."""
    from .hosts import defaults_from, offsite_enabled
    from .recipients import read_recipients
    from .wizard import run_wizard

    root = context.root
    text, _ = make_prompts(questionary, style)
    known = defaults_from(root)
    steps = host_steps(
        questionary,
        style,
        text,
        known,
        read_recipients(root),
        tailnet_suffix(known["suffix"]),
    )
    review = review_screen(
        questionary,
        style,
        steps,
        shown_host,
        "Write these files and generate the keys?",
    )
    state = run_wizard(steps, review=review)
    return host_from_state(state, offsite_enabled(root))


def ask_company(questionary, style, root: Path) -> "CompanyAnswers":
    from .company import (
        ACME_PRODUCTION,
        ACME_STAGING,
        COMPANY_PATTERN,
        EMAIL_PATTERN,
        SSH_PUBLIC_PATTERN,
        CompanyAnswers,
    )
    from .wizard import BACK, CANCEL, Step, run_wizard

    text, select = make_prompts(questionary, style)
    user = os.environ.get("USER") or "operator"

    def ask_company_name(state):
        return text(
            "Company (short, lowercase)",
            state.get("company")
            or (root.name if COMPANY_PATTERN.match(root.name) else ""),
            lambda v: bool(COMPANY_PATTERN.match(v)) or "lowercase, digits and dashes",
        )

    def ask_email(state):
        return text(
            "ACME contact email (Let's Encrypt notices)",
            state.get("acme_email", ""),
            lambda v: bool(EMAIL_PATTERN.match(v)) or "an email address",
        )

    def ask_ca(state):
        return select(
            "ACME directory",
            [ACME_STAGING, ACME_PRODUCTION],
            lambda v: (
                "staging — start here, switch after acceptance"
                if v == ACME_STAGING
                else "production — rate-limited on failed validations"
            ),
        )

    def ask_operator(state):
        return text(
            "Your name (key comment ops:<name>)",
            state.get("operator") or user,
            lambda v: bool(v.strip()) or "required",
        )

    def ask_operator_key(state):
        return text(
            "Your ops SSH private key (generated if missing)",
            state.get("operator_key") or f"~/.ssh/{state['company']}-ops",
            lambda v: bool(v) or "required",
        )

    def ask_second(state):
        answer = text(
            "Second operator's public key line (Enter to skip)",
            state.get("second", ""),
            lambda v: v == ""
            or bool(SSH_PUBLIC_PATTERN.match(v.strip()))
            or "an OpenSSH public key line",
        )
        return answer

    company = [
        Step("company", ask_company_name, label="Company"),
        Step("acme_email", ask_email, label="ACME email"),
        Step("acme_ca", ask_ca, label="ACME CA"),
        Step("operator", ask_operator, label="Operator"),
        Step("operator_key", ask_operator_key, label="Ops SSH key"),
        Step("second", ask_second, label="Second operator"),
    ]
    known = {"ssh_key": None, "keys_dir": None, "interface": None}

    def keys_dir_default(state):
        return f"~/.config/platform-keys/{state['company']}"

    host = host_steps(
        questionary, style, text, known, {}, tailnet_suffix(), prefix="First "
    )

    def ask_host_ssh(state):
        return text(
            "Operator SSH private key",
            state.get("ssh_key") or state["operator_key"],
            lambda v: bool(v) or "required",
        )

    def ask_host_keys_dir(state):
        return text(
            "Directory for age keys",
            state.get("keys_dir") or keys_dir_default(state),
            lambda v: bool(v) or "required",
        )

    overrides = {"ssh_key": ask_host_ssh, "keys_dir": ask_host_keys_dir}
    host = [
        replace(step, ask=overrides[step.key]) if step.key in overrides else step
        for step in host
    ]
    steps = company + host

    def shown(state, step):
        if step.key == "acme_ca":
            return "staging" if state["acme_ca"] == ACME_STAGING else "production"
        if step.key == "second":
            return state["second"][:40] + "…" if state["second"] else "none yet"
        if step.key in {
            "recovery",
            "name",
            "public_address",
            "tailnet",
            "interface",
            "ssh_key",
            "keys_dir",
        }:
            return shown_host(state, step)
        return state[step.key]

    review = review_screen(
        questionary, style, steps, shown, "Create the repository and generate the keys?"
    )
    state = run_wizard(steps, review=review)
    return CompanyAnswers(
        company=state["company"],
        acme_email=state["acme_email"],
        acme_ca=state["acme_ca"],
        operator=state["operator"],
        operator_key=state["operator_key"],
        extra_ops_keys=(state["second"].strip(),) if state["second"].strip() else (),
        host=host_from_state(state, offsite=False),
    )


def run_new_company(context: Context, questionary, style) -> int:
    from .company import CompanyError, next_steps, venv_commands, write_company
    from .wizard import Cancelled

    if context.kind in ("infra", "app"):
        print(
            f"{RED}new company-infra starts from an empty directory, not inside {context.kind} {context.root}{RESET}"
        )
        return 2
    root = Path.cwd()
    try:
        answers = ask_company(questionary, style, root)
        print()
        print(
            f"{DIM}→ ssh-keygen -t ed25519 -f {answers.operator_key} (if missing){RESET}"
        )
        print(f"{DIM}→ age-keygen --output {answers.host.age_key}{RESET}")
        print(f"{DIM}→ age-keygen --output {answers.host.recovery_key}{RESET}")
        result = write_company(root, answers)
    except CompanyError as error:
        print(f"{RED}{error}{RESET}")
        return 1
    except Cancelled:
        print("cancelled — nothing written")
        return 130

    for label, recipient in result.recipients.items():
        print(f"{GREEN}{label}: {recipient}{RESET}")
    print(f"{GREEN}ops key: {result.operator_public_key[:40]}…{RESET}")
    print("Written (nothing committed):")
    for relative in result.written:
        print(f"  {relative}")
    if result.git_initialised:
        print(f"{DIM}git init done; registered in ~/.config/platform/config.yml{RESET}")

    commands = venv_commands()
    print()
    print("Controller venv and the pinned collection:")
    for command in commands:
        print(f"  {command}")
    answer = questionary.confirm(
        "Run these now? (a couple of minutes)", default=True, style=style
    ).ask()
    if answer:
        for command in commands:
            print(f"{DIM}→ {command}{RESET}")
            completed = subprocess.run(command, shell=True, cwd=str(root))
            if completed.returncode != 0:
                print(f"{RED}failed: {command}{RESET}")
                return 1
        print(f"{GREEN}collection installed{RESET}")

    print()
    print(next_steps(answers, result))
    print(f"{DIM}  handbook: {HANDBOOK}#/flow-new-host{RESET}")
    return 0


def run_new_host(context: Context, questionary, style) -> int:
    from .hosts import HostError, next_steps, plan_host, write_host
    from .wizard import Cancelled

    if context.kind != "infra":
        print(f"{RED}new host works inside an infrastructure repository{RESET}")
        return 2
    try:
        answers = ask_new_host(context, questionary, style)
        plan = plan_host(context.root, answers)
        print()
        for label, path in plan.keys:
            print(f"{DIM}→ age-keygen --output {path}{RESET}")
        recipients = write_host(context.root, answers, plan)
    except HostError as error:
        print(f"{RED}{error}{RESET}")
        return 1
    except Cancelled:
        print("cancelled — nothing written")
        return 130

    for label, recipient in recipients.items():
        print(f"{GREEN}{label}: {recipient}{RESET}")
    print("Written (nothing committed):")
    for relative in plan.files:
        print(f"  {relative}")
    print()
    print(next_steps(answers, recipients))
    print(f"{DIM}  handbook: {HANDBOOK}#/flow-new-host{RESET}")
    return 0


def run_new_app(context: Context, questionary, style) -> int:
    from .wizard import Cancelled
    from .scaffold import (
        ScaffoldError,
        encrypt_secrets,
        existing_targets,
        next_steps,
        render_app,
        validate_app,
        write_files,
    )

    root = context.root if context.kind != "nowhere" else Path.cwd()
    try:
        answers = ask_app(context, questionary, style)
        files = render_app(answers)
        clashes = existing_targets(root, files)
        if clashes:
            print(f"{RED}refusing to overwrite: {', '.join(clashes)}{RESET}")
            print(
                "new app never edits what is already there; remove or rename these first"
            )
            return 1
        print()
        print(f"{DIM}→ writing {len(files)} files under {root}{RESET}")
        written = write_files(root, files)
        encrypted = encrypt_secrets(root, files)
        report = validate_app(root, files)
        from .secrets import enable_hooks

        hooks = enable_hooks(root)
    except ScaffoldError as error:
        print(f"{RED}{error}{RESET}")
        return 1
    except Cancelled:
        print("cancelled — nothing written")
        return 130

    failed = 0
    for relative, errors in report.items():
        if errors:
            failed += 1
            print(f"{RED}invalid application contract: {relative}{RESET}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"{GREEN}valid application contract: {relative}{RESET}")
    for relative in encrypted:
        print(f"{GREEN}encrypted: {relative}{RESET}")
    if hooks:
        print(
            f"{GREEN}hooks enabled: core.hooksPath=.githooks, push.followTags=true{RESET}"
        )
    else:
        print(
            f"{DIM}not a git repository yet — after git init: git config core.hooksPath .githooks{RESET}"
        )
    print()
    print(next_steps(answers, written))
    print(f"{DIM}  handbook: {HANDBOOK}#/flow-new-app{RESET}")
    return 1 if failed else 0


def run_new(context: Context, target: Optional[str]) -> int:
    questionary, style = load_prompts()

    if target is None:
        # Choice titles are prompt_toolkit formatted text, not a terminal
        # stream: styling goes through a class, never through escape codes.
        target = choose(
            questionary,
            style,
            "What do you want to create?",
            [name for name, _ in NEW_TARGETS],
            lambda name: [
                ("", f"{name:<14}"),
                ("class:instruction", dict(NEW_TARGETS)[name]),
            ],
        )

    if target == "app":
        return run_new_app(context, questionary, style)
    if target == "host":
        return run_new_host(context, questionary, style)
    if target == "company-infra":
        return run_new_company(context, questionary, style)

    print()
    print(f"{DIM}→ scaffold '{target}' is not wired yet.{RESET}")
    return 0


def run_secrets(context: Context, argv: list) -> int:
    """platform secrets push [env] [--stale] [--stage] | pull <env>."""
    from .secrets import (
        SecretsError,
        enable_hooks,
        pull_env,
        stage,
        staged_plaintext,
        sync,
    )

    if context.kind != "app":
        print(
            "secrets needs an application repository (deploy/platform.<env>.yml)",
            file=sys.stderr,
        )
        return 2
    action = argv[0] if argv else "push"
    flags = {a for a in argv[1:] if a.startswith("--")}
    names = [a for a in argv[1:] if not a.startswith("--")]
    root = context.root
    try:
        if action == "pull":
            if not names:
                print("platform secrets pull <environment>", file=sys.stderr)
                return 2
            path = pull_env(root, names[0])
            print(
                f"{GREEN}written: {path.relative_to(root)}{RESET}  (mode 0600, ignored by git)"
            )
            return 0
        if action != "push":
            print(f"unknown secrets action: {action}", file=sys.stderr)
            return 2
        leaked = staged_plaintext(root)
        if leaked:
            print(
                f"{RED}refusing: staged plaintext {', '.join(leaked)} — .env.* never enters git{RESET}"
            )
            return 1
        results = sync(
            root, only=names[0] if names else None, stale_only="--stale" in flags
        )
        if not results:
            print("no environments found under deploy/", file=sys.stderr)
            return 1
        to_stage = []
        for result in results:
            mark = GREEN + "✓" + RESET if result.written else DIM + "–" + RESET
            note = (
                f" (dropped {', '.join(result.dropped)}: the platform provides it)"
                if result.dropped
                else ""
            )
            print(f" {mark} {result.environment:<12} {result.reason}{note}")
            if result.written:
                to_stage.append(f"deploy/secrets.{result.environment}.sops.yaml")
        if "--stage" in flags and to_stage:
            stage(root, to_stage)
            print(f"{DIM}  staged: {', '.join(to_stage)}{RESET}")
        enable_hooks(root)
        return 0
    except SecretsError as error:
        print(f"{RED}secrets error: {error}{RESET}", file=sys.stderr)
        return 1


def run_infra(argv: list) -> int:
    """platform infra list|add <path>|forget <path> — paths only, no secrets."""
    from .config import config_path, forget_infra, infras, register_infra

    action = argv[0] if argv else "list"
    if action == "list":
        known = infras()
        if not known:
            print(
                "No infrastructure registered yet. Run platform doctor inside one, or: platform infra add <path>"
            )
            return 0
        for infra in known:
            keys = f"  keys {infra.keys}" if infra.keys else ""
            print(f"{infra.name:<20} {infra.path}{keys}")
        print(f"{DIM}  {config_path()}{RESET}")
        return 0
    if len(argv) < 2:
        print(f"platform infra {action} needs a path", file=sys.stderr)
        return 2
    path = Path(argv[1]).expanduser()
    if action == "add":
        if not (path / "inventory/hosts.yml").is_file():
            print(
                f"{path} has no inventory/hosts.yml — not an infrastructure repository",
                file=sys.stderr,
            )
            return 1
        print("registered" if register_infra(path) else "already registered")
        return 0
    print("forgotten" if forget_infra(path) else "was not registered")
    return 0


def run_core_update(context: Context, argv: list) -> int:
    """Pin → install → converge twice → readiness, host by host, with versions shown."""
    from .context import read_collection_pin
    from .core_update import (
        CoreUpdateError,
        HostVersion,
        artifact_url,
        host_version,
        install_collection,
        latest_release,
        parse_recap,
        playbook_command,
        rewrite_pin,
        run_playbook,
        verdict,
    )
    from .doctor import default_collections_root, installed_collection

    root = context.root
    check_only = "--check" in argv
    installed = installed_collection(default_collections_root(Path.home()))
    have = f"v{installed['version']}" if installed else None
    pin = read_collection_pin(root)
    release = latest_release()
    print(
        f"{DIM}console v{__version__} · installed {have or '—'} · requirements.yml {pin or '—'}"
        f" · latest release {release.tag if release else '? (gh unavailable)'}{RESET}"
    )
    print()

    blocked = tailnet_gate(read_tailnet())
    versions = []
    for host in context.hosts:
        address, user, identity = host_connection(host)
        found = (
            HostVersion(address, None, blocked)
            if blocked
            else host_version(address, user, identity)
        )
        versions.append((host, found))
        mark = (
            f"{GREEN}{found.version}{RESET}"
            if found.version and found.version == pin
            else (
                f"{CYAN}{found.version}{RESET}"
                if found.version
                else f"{RED}? {found.error}{RESET}"
            )
        )
        print(f"  {host.name:<24} {mark}")
    print()

    behind = [h for h, v in versions if v.version != pin]
    if check_only:
        if behind:
            print(f"{len(behind)} host(s) do not run the pinned {pin}")
            return 1
        print(f"{GREEN}every host runs {pin}{RESET}")
        return 0

    questionary, style = load_prompts()
    text, select = make_prompts(questionary, style)
    from .wizard import BACK, CANCEL

    OTHER = "another tag…"
    candidates = []
    if release:
        candidates.append(release.tag)
    if pin and pin not in candidates:
        candidates.append(pin)
    candidates.append(OTHER)
    labels = {}
    if release:
        labels[release.tag] = f"{release.tag}  latest release"
    if pin:
        labels[pin] = f"{pin}  current pin — reinstall and converge only"
    labels[OTHER] = "type a release tag"
    target = select("Target core version", candidates, lambda v: labels.get(v, v))
    if target in (BACK, CANCEL):
        print("cancelled — nothing changed")
        return 130
    if target == OTHER:
        target = text(
            "Release tag", "", lambda v: v.startswith("v") or "a tag such as v0.16.1"
        )
        if target in (BACK, CANCEL):
            print("cancelled — nothing changed")
            return 130

    if release and target == release.tag and release.notes.strip():
        print()
        print(f"{BOLD}Release notes {release.tag}{RESET}")
        for line in release.notes.strip().splitlines()[:40]:
            print(f"  {line}")
        print()
    if target != f"v{__version__}":
        print(
            f"{DIM}this console is v{__version__}; after the hosts move to {target},"
            f" update the console too (git pull in the core checkout, or pipx upgrade){RESET}"
        )

    try:
        if target != pin:
            path = root / "requirements.yml"
            new_text = rewrite_pin(path.read_text(encoding="utf-8"), target)
            print(f"{DIM}→ requirements.yml: {pin or 'no pin'} → {target}{RESET}")
            if not questionary.confirm(
                "Move the pin? (not committed)", default=True, style=style
            ).ask():
                print("cancelled — nothing changed")
                return 130
            path.write_text(new_text, encoding="utf-8")
            print(
                f"{GREEN}pinned {target} in requirements.yml — commit it as its own change{RESET}"
            )

        print(
            f"{DIM}→ .venv/bin/ansible-galaxy collection install --force --requirement requirements.yml{RESET}"
        )
        print(
            f"{DIM}  (falls back to {artifact_url(target)} when Galaxy times out){RESET}"
        )
        if not questionary.confirm(
            "Install the collection?", default=True, style=style
        ).ask():
            print(
                "stopped after the pin — install and converge by hand or run update again"
            )
            return 0
        used = install_collection(root, target)
        print(f"{GREEN}installed: {used}{RESET}")
    except CoreUpdateError as error:
        print(f"{RED}{error}{RESET}")
        return 1

    if blocked:
        print(f"{RED}{blocked}{RESET}")
        print(
            f"{DIM}  converge needs the tailnet — handbook: {HANDBOOK}#/flow-incidents{RESET}"
        )
        return 1
    choices = [
        questionary.Choice(
            f"{h.name}  ({v.version or '?'})",
            value=h.name,
            checked=(v.version != target),
        )
        for h, v in versions
    ]
    chosen = questionary.checkbox(
        "Converge which hosts?",
        choices=choices,
        style=style,
        instruction="(space to toggle, enter to confirm)",
    ).ask()
    if not chosen:
        print("no hosts chosen — the pin and the collection are updated, hosts are not")
        return 0

    try:
        for attempt in (1, 2):
            command = playbook_command(root, "converge", chosen)
            print()
            print(f"{DIM}→ converge #{attempt}: {' '.join(command)}{RESET}")
            if (
                attempt == 1
                and not questionary.confirm(
                    "Run converge twice on these hosts?", default=True, style=style
                ).ask()
            ):
                print("cancelled before converge")
                return 130
            code, output = run_playbook(root, command)
            problems = verdict(parse_recap(output), chosen, second=(attempt == 2))
            if code != 0 or problems:
                print()
                for problem in problems or [f"ansible-playbook exited {code}"]:
                    print(f"{RED}{problem}{RESET}")
                print(f"{DIM}  handbook: {HANDBOOK}#/flow-core-update{RESET}")
                return 1
            print(f"{GREEN}converge #{attempt}: clean{RESET}")

        command = playbook_command(root, "readiness", chosen)
        print()
        print(f"{DIM}→ readiness: {' '.join(command)}{RESET}")
        code, output = run_playbook(root, command)
        problems = verdict(parse_recap(output), chosen, second=False)
        if code != 0 or problems:
            for problem in problems or [f"ansible-playbook exited {code}"]:
                print(f"{RED}{problem}{RESET}")
            return 1
        print(f"{GREEN}readiness: passed{RESET}")
    except CoreUpdateError as error:
        print(f"{RED}{error}{RESET}")
        return 1

    print()
    print(
        f"{GREEN}{', '.join(chosen)}: {target}, second converge changed nothing, readiness passed{RESET}"
    )
    print(
        "Next: commit requirements.yml; in every application that deploys here: platform update"
    )
    print(f"{DIM}  handbook: {HANDBOOK}#/flow-core-update{RESET}")
    return 0


def run_update(context: Context, argv: list) -> int:
    """Rewrite the platform-owned files from the console's templates, after a look."""
    from .update import (
        UpdateError,
        apply,
        console_ahead_of_hosts,
        gather_facts,
        plan,
        recipients_changed,
        render_managed,
    )

    assume_yes = "--yes" in argv
    check_only = "--check" in argv

    if context.kind == "infra":
        return run_core_update(context, argv)

    if context.kind != "app":
        print(
            "platform update works inside an application repository"
            " (one with deploy/platform.<environment>.yml)",
            file=sys.stderr,
        )
        return 2

    try:
        facts = gather_facts(context, Path.home())
    except UpdateError as error:
        print(f"{RED}{error}{RESET}")
        return 1

    if console_ahead_of_hosts(facts.core_pin):
        print(
            f"{RED}this console is v{__version__} but {facts.pin_source} pins {facts.core_pin}:"
            f" a workflow from a newer core may ask the host for what it lacks{RESET}"
        )
        print("update the hosts first — handbook #/flow-core-update")
        return 1

    changes = plan(context.root, render_managed(facts))
    print(
        f"{DIM}templates v{__version__} · core pin {facts.core_pin} from {facts.pin_source}"
        f" · recipients from {facts.recipients_source}{RESET}"
    )
    print()

    labels = {
        "same": f"{DIM}=  up to date{RESET}",
        "create": f"{GREEN}+  missing, will be created{RESET}",
        "update": f"{CYAN}~  will be rewritten{RESET}",
        "adopt": f"{CYAN}~  identical; will gain the platform-managed marker{RESET}",
        "merge": f"{CYAN}~  missing lines will be appended{RESET}",
        "owned": f"{RED}!  owned by the application (no marker) — differs, left alone{RESET}",
    }
    for change in changes:
        print(f"  {change.path:<36} {labels[change.kind]}")
    pending = [change for change in changes if change.writes]
    owned = [change for change in changes if change.kind == "owned"]
    print()

    for change in pending + owned:
        if change.kind in {"adopt"}:
            continue
        for line in change.diff().splitlines():
            colour = (
                GREEN if line.startswith("+") else RED if line.startswith("-") else DIM
            )
            print(f"{colour}{line}{RESET}")
        print()

    if owned:
        print(
            f"{DIM}owned files are yours: apply the diff by hand, or restore the"
            f" '# platform-managed' first line to hand them back{RESET}"
        )
    if not pending:
        print(f"{GREEN}nothing to update{RESET}")
        return 0
    if check_only:
        print(f"{len(pending)} file(s) behind the templates")
        return 1

    if not assume_yes:
        questionary, style = load_prompts()
        answer = questionary.confirm(
            f"Rewrite {len(pending)} file(s)? (nothing is committed)",
            default=True,
            style=style,
        ).ask()
        if not answer:
            print("cancelled — nothing written")
            return 130

    written = apply(context.root, changes)
    for relative in written:
        print(f"{GREEN}written: {relative}{RESET}")

    if recipients_changed(changes):
        from .secrets import SecretsError, encrypt_env, env_path

        for environment in facts.environments:
            if not env_path(context.root, environment).is_file():
                print(
                    f"{DIM}deploy/secrets.{environment}.sops.yaml still carries the old"
                    f" recipients: no .env.{environment} here — platform secrets pull"
                    f" {environment} with a key, then platform secrets push{RESET}"
                )
                continue
            try:
                result = encrypt_env(context.root, environment)
            except SecretsError as error:
                print(f"{RED}{error}{RESET}")
                return 1
            print(
                f"{GREEN}re-encrypted for the new recipients: {result.written}{RESET}"
            )

    print()
    print(
        "Review with git diff, then commit — the pin and the templates move as one reviewed change."
    )
    print(f"{DIM}  handbook: {HANDBOOK}#/flow-core-update{RESET}")
    return 0


def run_doctor(context: Context) -> int:
    """Non-interactive on purpose: it has to work from a script and over a pipe."""
    findings = diagnose(context, read_tailnet())
    marks = {
        "ok": f"{GREEN}✓{RESET}",
        "fail": f"{RED}✗{RESET}",
        "skip": f"{DIM}–{RESET}",
    }
    failed = 0

    for finding in findings:
        failed += finding.failed
        line = f" {marks[finding.status]} {finding.title:<40} {finding.detail}"
        if finding.failed and finding.anchor:
            line += f"  {DIM}→ {HANDBOOK}{finding.anchor}{RESET}"
        print(line)

    print()
    if failed:
        print(f"{RED}{failed} check(s) failed{RESET}")
        return 1
    print(f"{GREEN}all checks passed{RESET}")
    return 0


def run(argv: list, start: Optional[Path] = None) -> int:
    context = detect(start)

    if context.kind == "host":
        print(
            "This is a platform host. The operator console runs on your workstation;"
            " here, use: sudo -n platform <command>",
            file=sys.stderr,
        )
        return 2

    banner(context, stream=sys.stdout if sys.stdout.isatty() else sys.stderr)

    if context.kind == "infra":
        from .config import register_infra

        if register_infra(context.root):
            print(
                f"{DIM}registered this infrastructure for new app and doctor: {context.root}{RESET}\n"
            )

    if argv and argv[0] == "infra":
        return run_infra(argv[1:])

    if argv and argv[0] == "secrets":
        return run_secrets(context, argv[1:])

    if argv and argv[0] == "doctor":
        return run_doctor(context)

    if argv and argv[0] == "update":
        if ("--yes" in argv or "--check" in argv) or (
            sys.stdin.isatty() and sys.stdout.isatty()
        ):
            return run_update(context, argv[1:])
        print(
            "platform update asks before writing; pass --yes or --check without a terminal",
            file=sys.stderr,
        )
        return 2

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("The console is interactive and needs a terminal.", file=sys.stderr)
        return 2

    try:
        if argv and argv[0] == "new":
            return run_new(context, argv[1] if len(argv) > 1 else None)
        return run_menu(context)
    except ConsoleUnavailable as error:
        print(f"platform: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
        return 130
