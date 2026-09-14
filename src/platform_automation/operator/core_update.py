"""Move an infrastructure to a new core the way the handbook's flow does.

Pin, install, converge twice, readiness — with the one thing an operator
cannot see from the inventory: which version each host actually runs. The
pin is rewritten as one line; nothing is committed; every host is asked
before it is touched.
"""

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .context import INFRA_REQUIREMENTS

CORE_REPOSITORY = "otostogan/platform-automation-core"
RUNTIME_VERSION_FILE = "/opt/platform/runtime/platform_automation/__init__.py"
ARTIFACT = "releases/download/{tag}/otostogan-platform-{version}.tar.gz"
PIN_PATTERN = re.compile(
    r"releases/download/v(\d+\.\d+\.\d+)/otostogan-platform-(\d+\.\d+\.\d+)\.tar\.gz"
)
RECAP_PATTERN = re.compile(
    r"^(?P<host>\S+)\s*:\s*ok=(?P<ok>\d+)\s+changed=(?P<changed>\d+)\s+unreachable=(?P<unreachable>\d+)"
    r"\s+failed=(?P<failed>\d+)"
)
TAG_PATTERN = re.compile(r"^v\d+\.\d+\.\d+$")
GALAXY_TIMEOUT_SECONDS = 300


class CoreUpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Release:
    tag: str
    notes: str


@dataclass(frozen=True)
class HostVersion:
    name: str
    version: Optional[str]  # None: could not be read
    error: Optional[str] = None


def artifact_url(tag: str) -> str:
    return f"https://github.com/{CORE_REPOSITORY}/" + ARTIFACT.format(
        tag=tag, version=tag.lstrip("v")
    )


def rewrite_pin(text: str, tag: str) -> str:
    """Both places the version appears in the one URL line move together."""
    if not TAG_PATTERN.match(tag):
        raise CoreUpdateError(f"not a release tag: {tag}")
    replaced, count = PIN_PATTERN.subn(
        ARTIFACT.format(tag=tag, version=tag.lstrip("v")), text
    )
    if count != 1:
        raise CoreUpdateError(
            f"{INFRA_REQUIREMENTS} should pin the core exactly once; found {count} pin(s)"
        )
    return replaced


def latest_release(runner=subprocess.run) -> Optional[Release]:
    """The newest published release of the core; None when gh cannot tell."""
    try:
        result = runner(
            [
                "gh",
                "release",
                "view",
                "--repo",
                CORE_REPOSITORY,
                "--json",
                "tagName,body",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        document = json.loads(_text(result.stdout))
        return Release(str(document["tagName"]), str(document.get("body") or ""))
    except (ValueError, KeyError, TypeError):
        return None


def parse_runtime_version(text: str) -> Optional[str]:
    match = re.search(r'__version__\s*=\s*"(\d+\.\d+\.\d+)"', text)
    return f"v{match.group(1)}" if match else None


def host_version(
    address: str, user: str, identity: Optional[Path], runner=subprocess.run
) -> HostVersion:
    """What the host runs, read from the runtime the last converge installed."""
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if identity is not None:
        command += ["-i", str(identity)]
    command += [f"{user}@{address}", "--", shlex.join(["cat", RUNTIME_VERSION_FILE])]
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
        return HostVersion(address, None, f"ssh could not run: {error}")
    if result.returncode != 0:
        tail = _text(result.stderr).strip().splitlines()
        return HostVersion(address, None, tail[-1] if tail else "unreachable")
    version = parse_runtime_version(_text(result.stdout))
    return HostVersion(
        address, version, None if version else "no runtime version on the host"
    )


def install_collection(root: Path, tag: str, runner=subprocess.run) -> str:
    """``ansible-galaxy … --requirement``; the artifact URL when Galaxy times out.

    Galaxy serves ``community.general`` and has been slow enough to time out
    twice in one afternoon; the core itself comes from GitHub and is what
    actually changes, so it is installed directly when the first attempt
    fails. Returns the command that succeeded.
    """
    galaxy = root / ".venv/bin/ansible-galaxy"
    if not galaxy.is_file():
        raise CoreUpdateError(
            ".venv/bin/ansible-galaxy is missing — handbook #/flow-new-host, the venv step"
        )
    attempts = [
        [
            str(galaxy),
            "collection",
            "install",
            "--force",
            "--timeout",
            str(GALAXY_TIMEOUT_SECONDS),
            "--requirement",
            str(INFRA_REQUIREMENTS),
        ],
        [
            str(galaxy),
            "collection",
            "install",
            "--force",
            "--timeout",
            str(GALAXY_TIMEOUT_SECONDS),
            artifact_url(tag),
        ],
    ]
    last = ""
    for command in attempts:
        try:
            result = runner(
                command,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=GALAXY_TIMEOUT_SECONDS + 60,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            last = str(error)
            continue
        if result.returncode == 0:
            return shlex.join(command)
        last = _text(result.stdout).strip().splitlines()[-1:] or ["install failed"]
        last = last[0]
    raise CoreUpdateError(f"collection install failed: {last}")


def parse_recap(output: str) -> dict:
    """Per-host counters from ``PLAY RECAP``; empty when the run never got there."""
    recap = {}
    seen = False
    for line in output.splitlines():
        if line.startswith("PLAY RECAP"):
            seen = True
            continue
        if not seen:
            continue
        match = RECAP_PATTERN.match(line.strip())
        if match:
            recap[match.group("host")] = {
                key: int(match.group(key))
                for key in ("ok", "changed", "unreachable", "failed")
            }
    return recap


def playbook_command(
    root: Path, playbook: str, hosts: list, extra: Optional[list] = None
) -> list:
    return [
        str(root / ".venv/bin/ansible-playbook"),
        f"otostogan.platform.{playbook}",
        "--inventory",
        "inventory/hosts.yml",
        "--limit",
        ",".join(hosts),
        *(extra or []),
    ]


def run_playbook(
    root: Path, command: list, spawn=subprocess.Popen, echo=print
) -> tuple:
    """Stream the playbook to the terminal and keep its output for the recap."""
    if not Path(command[0]).is_file():
        raise CoreUpdateError(
            ".venv/bin/ansible-playbook is missing — handbook #/flow-new-host, the venv step"
        )
    try:
        process = spawn(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as error:
        raise CoreUpdateError(f"ansible-playbook could not start: {error}") from error
    lines = []
    for line in process.stdout:
        lines.append(line)
        echo(line.rstrip("\n"))
    code = process.wait()
    return code, "".join(lines)


def verdict(recap: dict, hosts: list, second: bool) -> list:
    """What went wrong per host, in words; empty when the run is clean."""
    problems = []
    for host in hosts:
        counters = recap.get(host)
        if counters is None:
            problems.append(f"{host}: no recap line — the run did not reach it")
            continue
        if counters["unreachable"]:
            problems.append(f"{host}: unreachable")
        elif counters["failed"]:
            problems.append(f"{host}: {counters['failed']} task(s) failed")
        elif second and counters["changed"]:
            problems.append(
                f"{host}: second converge still changed {counters['changed']} task(s) — "
                "not idempotent; a core defect, not something to re-run"
            )
    return problems


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
