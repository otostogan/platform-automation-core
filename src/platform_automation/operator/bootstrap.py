"""Take a new host from the provider's SSH to the tailnet, the handbook's way.

Bootstrap is the one run that goes over the public address as root. Everything
after it goes over the tailnet as ``ops``, and convergence then closes the
public door. The checks here are the ones whose failure is cheap before the
run and expensive in the middle of it: is the host in the bootstrap inventory,
is the one-off tailnet key where the inventory says, does root SSH answer.
"""

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
HANDOVER = "id && hostname && sudo -n true && tailscale ip"


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
    """``("ok" | "missing-file" | "not-configured", path or None)``.

    Without the setting the role installs Tailscale and prints a
    ``tailscale up`` command for a human; that is a supported path, just not
    one a console can finish.
    """
    document = load_yaml(root / SHARED_SECRETS)
    value = document.get(AUTH_KEY) if isinstance(document, dict) else None
    if not isinstance(value, str) or not value:
        return "not-configured", None
    path = Path(value).expanduser()
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

    Success needs a tailnet address in the answer: SSH that works while
    ``tailscale ip`` prints nothing means the host is reachable some other
    way, and convergence would then close that way.
    """
    reason = "never tried"
    for attempt in range(attempts):
        ok, output = answers(command, runner)
        if ok and any(line.startswith("100.") for line in output.splitlines()):
            return True, output
        reason = (
            output if not ok else "ops answers, but tailscale ip printed no address"
        )
        if attempt + 1 < attempts:
            sleeper(pause)
    return False, reason


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
