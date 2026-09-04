"""The interactive front door: ``platform`` with no arguments.

Every choice ends in something that already exists — a host command with
``--json``, a playbook, a ``gh`` dispatch. The command is always printed
before it runs; a choice that is not wired yet prints only the command, so
what the console *would* do is never a guess.
"""

import subprocess
import sys
from dataclasses import dataclass
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


def app_actions(context: Context, scope) -> list:
    target = context.target_host or "<target host>"
    ident = ["--project", scope.project, "--environment", scope.environment]
    actions = [
        Action(
            "Deploy",
            deploy_command(context.dispatch_inputs, scope.environment),
            None,
            "#/flow-deploy",
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
        Action(
            "Re-key secrets",
            f"sops updatekeys deploy/secrets.{scope.environment}.sops.yaml",
            None,
            "#/flow-operators",
        ),
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
            actions = app_actions(context, scope)

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


def ask_app(context: Context, questionary, style) -> "AppAnswers":
    """Ask only what cannot be read; show what was."""
    from .context import read_hosts
    from .doctor import age_recipient, host_secret_path
    from .scaffold import (
        AppAnswers,
        DOMAIN_PATTERN,
        ENVIRONMENTS,
        PROJECT_PATTERN,
        SECRET_NAME_PATTERN,
        git_org,
    )

    root = context.root
    text = lambda message, default="", validate=None: questionary.text(
        message, default=default, validate=validate, style=style
    ).ask()

    project = text(
        "Project",
        default=root.name if PROJECT_PATTERN.match(root.name) else "",
        validate=lambda v: bool(PROJECT_PATTERN.match(v))
        or "lowercase, digits and dashes, 2–63 chars",
    )
    org = text(
        "ghcr.io owner (org)",
        default=git_org(root) or "",
        validate=lambda v: bool(v) or "required",
    )
    environments = questionary.checkbox(
        "Environments",
        choices=[
            questionary.Choice(e, checked=e in ("lab", "production"))
            for e in ENVIRONMENTS
        ],
        style=style,
    ).ask()
    if not environments:
        raise KeyboardInterrupt
    domains = {}
    for environment in environments:
        domains[environment] = text(
            f"Domain for {environment}",
            validate=lambda v: bool(DOMAIN_PATTERN.match(v))
            or "lowercase, at least one dot",
        )

    # Infrastructure → host → target, both recipients and the core pin, so the
    # application deploys with the version its hosts actually run.
    from .config import infras as registered_infras
    from .context import read_collection_pin
    from .recipients import host_recipient, read_recipients, recovery_recipient

    target_host = recipient_host = recipient_recovery = ""
    core_pin = None
    known = registered_infras()
    if not known:
        print(
            f"{DIM}  no infrastructure registered (platform infra add <path>) — asking instead{RESET}"
        )
    else:
        infra = (
            known[0]
            if len(known) == 1
            else choose(
                questionary,
                style,
                "Infrastructure",
                known,
                lambda i: f"{i.name}  {i.path}",
            )
        )
        hosts = read_hosts(infra.path)
        if not hosts:
            print(f"{DIM}  {infra.name} lists no hosts yet — asking instead{RESET}")
        else:
            host = choose(
                questionary,
                style,
                "Target host",
                list(hosts),
                lambda h: f"{h.name}  {h.address or ''}",
            )
            target_host = host.address or host.name
            published = read_recipients(infra.path)
            recipient_host = host_recipient(published, host.name) or ""
            recipient_recovery = recovery_recipient(published) or ""
            if not recipient_host:
                key_path = host_secret_path(infra.path, host, "secrets_age_key_source")
                if key_path is not None and key_path.is_file():
                    recipient_host = age_recipient(key_path, subprocess.run) or ""
            core_pin = read_collection_pin(infra.path)
            print(
                f"{DIM}  {infra.name}: host {target_host} · recipients "
                f"{'host ✓' if recipient_host else 'host ?'} {'recovery ✓' if recipient_recovery else 'recovery ?'}"
                f" · core {core_pin or '?'}{RESET}"
            )
    if not target_host:
        target_host = text(
            "Target host (MagicDNS name)", validate=lambda v: bool(v) or "required"
        )
    if not recipient_host:
        recipient_host = text(
            "Host age recipient (age1…)",
            validate=lambda v: v.startswith("age1") or "age1…",
        )
    if not recipient_recovery:
        recipient_recovery = text(
            "Recovery age recipient (age1…)",
            validate=lambda v: v.startswith("age1") or "age1…",
        )

    port = int(
        text(
            "Internal port", default="3000", validate=lambda v: v.isdigit() or "number"
        )
    )
    path = text(
        "Healthcheck path",
        default="/",
        validate=lambda v: v.startswith("/") or "starts with /",
    )
    timeout = int(
        text(
            "Healthcheck timeout, seconds",
            default="120",
            validate=lambda v: v.isdigit() or "number",
        )
    )
    mode = choose(
        questionary,
        style,
        "Database",
        ["docker", "external"],
        lambda m: (
            "platform-owned (docker)"
            if m == "docker"
            else "external — the platform will not back it up"
        ),
    )
    major = int(choose(questionary, style, "PostgreSQL major", [18, 17, 16], str))
    interval, retain = 15, 3
    if mode == "docker":
        interval = int(
            text(
                "Backup every N minutes",
                default="15",
                validate=lambda v: v.isdigit() and 15 <= int(v) <= 1440 or "15–1440",
            )
        )
        retain = int(
            text(
                "Keep N dumps locally",
                default="3",
                validate=lambda v: v.isdigit() and 1 <= int(v) <= 100 or "1–100",
            )
        )
    query = text("Restore validation query", default="SELECT 1")
    names = text(
        "Secret names, comma-separated (values come later via sops)",
        default="API_TOKEN, SESSION_SECRET",
        validate=lambda v: all(
            SECRET_NAME_PATTERN.match(n.strip()) for n in v.split(",") if n.strip()
        )
        or "environment variable names",
    )

    return AppAnswers(
        project=project,
        owner=org,
        environments=tuple(environments),
        domains=domains,
        target_host=target_host,
        recipient_host=recipient_host,
        recipient_recovery=recipient_recovery,
        internal_port=port,
        healthcheck_path=path,
        healthcheck_timeout=timeout,
        database_mode=mode,
        postgres_major=major,
        backup_interval=interval,
        backup_retain=retain,
        restore_query=query,
        secret_names=tuple(n.strip() for n in names.split(",") if n.strip()),
        **({"core_pin": core_pin} if core_pin else {}),
    )


def run_new_app(context: Context, questionary, style) -> int:
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
                "new app is for a new application; for an existing one use: platform doctor"
            )
            return 1
        print()
        print(f"{DIM}→ writing {len(files)} files under {root}{RESET}")
        written = write_files(root, files)
        encrypted = encrypt_secrets(root, files)
        report = validate_app(root, files)
    except ScaffoldError as error:
        print(f"{RED}{error}{RESET}")
        return 1

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

    print()
    print(f"{DIM}→ scaffold '{target}' is not wired yet.{RESET}")
    return 0


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

    if argv and argv[0] == "doctor":
        return run_doctor(context)

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
