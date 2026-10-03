"""Take a new host from the provider's SSH to the tailnet, the handbook's way.

Bootstrap is the one run that goes over the public address as root. Everything
after it goes over the tailnet as ``ops``, and convergence then closes the
public door. The checks here are the ones whose failure is cheap before the
run and expensive in the middle of it: is the host in the bootstrap inventory,
is the one-off tailnet key where the inventory says, does root SSH answer.
"""

import ipaddress
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .context import load_yaml

BOOTSTRAP_INVENTORY = "inventory/bootstrap.yml"
SHARED_SECRETS = "inventory/group_vars/all/local-secrets.yml"
AUTH_KEY = "tailscale_auth_key_source"
# printenv, not $SSH_CONNECTION: nothing here is left for a shell to expand.
HANDOVER = "id && hostname && sudo -n true && printenv SSH_CONNECTION && tailscale ip"


@dataclass(frozen=True)
class Entry:
    """How the bootstrap inventory reaches the host before the tailnet exists."""

    address: str
    user: str
    key: Optional[Path]
    port: int


def bootstrap_entry(root: Path, name: str) -> Optional[Entry]:
    document = load_yaml(root / BOOTSTRAP_INVENTORY)
    try:
        values = document["all"]["children"]["platform_hosts"]["hosts"][name]
    except (KeyError, TypeError):
        return None
    if not isinstance(values, dict) or not values.get("ansible_host"):
        return None
    key = values.get("ansible_ssh_private_key_file")
    try:
        port = int(values.get("ansible_port", 22))
    except (TypeError, ValueError):
        port = 22
    return Entry(
        address=str(values["ansible_host"]),
        user=str(values.get("ansible_user") or "root"),
        key=Path(str(key)).expanduser() if key else None,
        port=port,
    )


def auth_key_state(root: Path) -> tuple:
    """``("ok" | "missing-file" | "symlink" | "not-configured", path or None)``.

    Without the setting the role installs Tailscale and prints a
    ``tailscale up`` command for a human; that is a supported path, just not
    one a console can finish.
    """
    document = load_yaml(root / SHARED_SECRETS)
    value = document.get(AUTH_KEY) if isinstance(document, dict) else None
    if not isinstance(value, str) or not value:
        return "not-configured", None
    path = Path(value).expanduser()
    # The role refuses a symlink, and only after earlier roles have already
    # changed the host; refuse it here, before anything has.
    if path.is_symlink():
        return "symlink", path
    return ("ok" if path.is_file() else "missing-file"), path


def ssh_base(key: Optional[Path], port: int = 22) -> list:
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if key is not None:
        command += ["-i", str(key)]
    if port != 22:
        command += ["-p", str(port)]
    return command


def root_probe(entry: Entry) -> list:
    return [*ssh_base(entry.key, entry.port), f"{entry.user}@{entry.address}", "true"]


def handover_probe(address: str, user: str, key: Optional[Path]) -> list:
    return [*ssh_base(key), f"{user}@{address}", HANDOVER]


def shown(command: list) -> str:
    return shlex.join(command)


def answers(command: list, runner=subprocess.run) -> tuple:
    """``(True, output)`` when the command exits 0, else ``(False, reason)``."""
    try:
        result = runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    output = _text(result.stdout).strip()
    if result.returncode != 0:
        return False, _text(result.stderr).strip() or f"exit {result.returncode}"
    return True, output


def wait_for_handover(
    command: list,
    runner=subprocess.run,
    sleeper=time.sleep,
    attempts: int = 12,
    pause: int = 10,
) -> tuple:
    """The tailnet needs a moment to learn the new machine; ask a few times.

    Success needs this very session to have arrived at one of the host's
    tailnet addresses. SSH that works some other way — the public interface,
    a stale address in the inventory — proves nothing: convergence checks the
    same thing and would refuse, or would close the way that did work.
    """
    reason = "never tried"
    for attempt in range(attempts):
        ok, output = answers(command, runner)
        if ok:
            problem = handover_problem(output)
            if problem is None:
                return True, output
            reason = problem
        else:
            reason = output
        if attempt + 1 < attempts:
            sleeper(pause)
    return False, reason


def handover_problem(output: str) -> Optional[str]:
    """Why the probe's answer is not a handover; ``None`` when it is one."""
    lines = [line.strip() for line in output.splitlines()]
    addresses = {line for line in lines if is_address(line)}
    if not addresses:
        return "ops answers, but tailscale ip printed no address"
    # SSH_CONNECTION: client address, client port, server address, server port
    arrived = [
        fields[2]
        for fields in (line.split() for line in lines)
        if len(fields) == 4 and is_address(fields[0]) and is_address(fields[2])
    ]
    if not arrived:
        return "ops answers, but the session did not report where it arrived"
    if arrived[0] not in addresses:
        return (
            f"ops answers at {arrived[0]}, which is not a tailnet address of the host"
            " — the inventory address does not go through the tailnet"
        )
    return None


def is_address(line: str) -> bool:
    """``tailscale ip`` prints one address per line; nothing else in the probe does."""
    try:
        ipaddress.ip_address(line.strip())
    except ValueError:
        return False
    return True


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
