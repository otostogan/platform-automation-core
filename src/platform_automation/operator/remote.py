"""Run ``platform … --json`` on a host over the operator's own SSH.

Nothing here holds a credential: the connection is the operator's SSH agent
and config, exactly as Ansible's would be. The host command already speaks
JSON for machines, so this module only carries it across and reports a
refusal the way the host printed it.
"""

import json
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

CONNECT_TIMEOUT_SECONDS = 10
COMMAND_TIMEOUT_SECONDS = 120

# Tailscale SSH under a policy with ``action: check`` holds the connection
# until the operator confirms it in a browser, and says so on stderr. The
# command's own patience must not count that wait; the operator gets this
# long to open the link instead.
CHECK_PROMPT = "# Tailscale SSH requires an additional check."
CHECK_LINK = "# To authenticate, visit: "
CHECK_DONE = "# Authentication checked with Tailscale SSH."
CHECK_TIMEOUT_SECONDS = 15 * 60
POLL_SECONDS = 0.2


class CheckExpired(subprocess.TimeoutExpired):
    """The browser check was not confirmed in time."""

    def __str__(self) -> str:
        minutes = int(self.timeout // 60)
        return (
            f"the Tailscale SSH check was not confirmed within {minutes} minutes;"
            " run the action again and open the link it prints"
        )


def is_check_line(line: str) -> bool:
    return line.strip() in (CHECK_PROMPT, CHECK_DONE) or line.strip().startswith(
        CHECK_LINK
    )


def strip_check_lines(stderr: str) -> str:
    """stderr without what Tailscale SSH said about the check; it was shown already."""
    return "\n".join(line for line in stderr.splitlines() if not is_check_line(line))


def _say(message: str) -> None:
    print(message, flush=True)


def checked_run(
    command: list,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check: bool = False,
    timeout: Optional[float] = None,
    popen=subprocess.Popen,
    notify: Callable[[str], None] = _say,
    clock: Callable[[], float] = time.monotonic,
    check_timeout: float = CHECK_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` for an ssh command that may stop for a Tailscale check.

    stderr is watched as it arrives. When Tailscale SSH asks for the check,
    the link is printed through ``notify`` and ``timeout`` stops counting
    until stderr reports the check done; only ``check_timeout`` bounds that
    wait. Everything else behaves as ``subprocess.run`` with captured output:
    the result is a ``CompletedProcess`` whose stderr no longer carries the
    check lines, and running out of time raises ``TimeoutExpired``.
    """
    process = popen(command, stdin=stdin, stdout=stdout, stderr=stderr)
    out_chunks: list = []
    err_lines: list = []
    state = {"pending": False, "asked_at": None, "resumed_at": None}
    lock = threading.Lock()

    def read_stdout() -> None:
        if process.stdout is not None:
            out_chunks.append(process.stdout.read() or b"")

    def read_stderr() -> None:
        if process.stderr is None:
            return
        for raw in iter(process.stderr.readline, b""):
            line = _text(raw).rstrip("\r\n")
            if not is_check_line(line):
                err_lines.append(line)
                continue
            if line.strip().startswith(CHECK_LINK):
                link = line.strip()[len(CHECK_LINK) :]
                with lock:
                    state["pending"] = True
                    state["asked_at"] = clock()
                notify("Tailscale SSH asks you to confirm this session in a browser:")
                notify(f"  {link}")
                notify(
                    "  waiting — approve there and come back; the check is per"
                    " session and lasts the policy's checkPeriod, so the next"
                    " actions will not ask again"
                )
            elif line.strip() == CHECK_DONE:
                with lock:
                    state["pending"] = False
                    state["resumed_at"] = clock()
                notify("  confirmed; continuing")

    readers = [
        threading.Thread(target=read_stdout, daemon=True),
        threading.Thread(target=read_stderr, daemon=True),
    ]
    for reader in readers:
        reader.start()

    expired: Optional[subprocess.TimeoutExpired] = None
    try:
        started = clock()
        while True:
            try:
                process.wait(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                pass
            now = clock()
            with lock:
                pending = state["pending"]
                asked_at = state["asked_at"]
                resumed_at = state["resumed_at"]
            if pending:
                if now - asked_at > check_timeout:
                    expired = CheckExpired(command, check_timeout)
            elif timeout is not None:
                since = resumed_at if resumed_at is not None else started
                if now - since > timeout:
                    expired = subprocess.TimeoutExpired(command, timeout)
            if expired is not None:
                process.kill()
                process.wait()
                break
    except BaseException:
        # Ctrl-C while waiting, as subprocess.run would: the ssh child does
        # not outlive the console, nor does whatever it was running.
        process.kill()
        process.wait()
        raise

    for reader in readers:
        reader.join()
    output = b"".join(out_chunks)
    errors = "\n".join(err_lines).encode("utf-8")
    if expired is not None:
        expired.output = output
        expired.stderr = errors
        raise expired
    result = subprocess.CompletedProcess(command, process.returncode, output, errors)
    if check:
        result.check_returncode()
    return result


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
    runner=checked_run,
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
    stderr = strip_check_lines(_text(result.stderr))

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
