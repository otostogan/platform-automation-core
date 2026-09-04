"""Run ``platform … --json`` on a host over the operator's own SSH.

Nothing here holds a credential: the connection is the operator's SSH agent
and config, exactly as Ansible's would be. The host command already speaks
JSON for machines, so this module only carries it across and reports a
refusal the way the host printed it.
"""

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

CONNECT_TIMEOUT_SECONDS = 10
COMMAND_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class RemoteResult:
    ok: bool
    code: int
    document: Optional[dict]
    error: Optional[str]
    command: str


def build_command(
    host: str,
    user: str,
    arguments: list[str],
    identity: Optional[Path] = None,
) -> list[str]:
    remote = shlex.join(["sudo", "-n", "platform", *arguments, "--json"])
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
    ]

    if identity is not None:
        command += ["-i", str(identity)]

    return [*command, f"{user}@{host}", "--", remote]


def run_platform(
    host: str,
    user: str,
    arguments: list[str],
    identity: Optional[Path] = None,
    runner=subprocess.run,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> RemoteResult:
    command = build_command(host, user, arguments, identity)
    shown = shlex.join(command)

    try:
        result = runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return RemoteResult(False, -1, None, f"ssh could not run: {error}", shown)

    stdout = _text(result.stdout)
    stderr = _text(result.stderr)

    if result.returncode != 0:
        return RemoteResult(False, result.returncode, None, host_error(stderr), shown)

    try:
        document = json.loads(stdout)
    except json.JSONDecodeError:
        return RemoteResult(
            False, result.returncode, None, "host did not answer with JSON", shown
        )

    if not isinstance(document, dict):
        return RemoteResult(
            False, result.returncode, None, "host answer is not an object", shown
        )

    return RemoteResult(True, 0, document, None, shown)


def host_error(stderr: str) -> str:
    """The host prints one line of the form ``<command> error: …``; keep it verbatim."""
    for line in reversed(stderr.splitlines()):
        if " error: " in line:
            return line.strip()

    tail = stderr.strip().splitlines()
    return tail[-1] if tail else "host command failed without a message"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
