"""Start a deployment from the workstation the way the handbook does: through
the application's own Deploy workflow, never around it.

The console never talks to the host for a deployment. It dispatches the
workflow with ``gh``, finds the run it started, and watches it — so what the
operator sees in the terminal is exactly what GitHub Actions shows, and the
host is reached only by the CI identity the tailnet policy grants.
"""

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

WORKFLOW = "deploy.yml"
TAG_PATTERN = re.compile(r"^v\d+\.\d+\.\d+$")


class DeployError(RuntimeError):
    pass


@dataclass(frozen=True)
class Run:
    id: int
    url: str
    status: str
    conclusion: Optional[str]


def gh(root: Path, arguments: list, runner=subprocess.run, timeout=60):
    try:
        return runner(
            ["gh", *arguments],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise DeployError(
            "gh (GitHub CLI) is not installed on this workstation"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise DeployError(f"gh {arguments[0]} did not answer in {timeout}s") from error


def current_branch(root: Path, runner=subprocess.run) -> Optional[str]:
    result = runner(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    name = _text(result.stdout).strip()
    return name if result.returncode == 0 and name and name != "HEAD" else None


def release_tags(root: Path, runner=subprocess.run, limit: int = 8) -> list:
    """Newest version tags first — the things an operator usually deploys."""
    result = runner(
        ["git", "tag", "--list", "v*", "--sort=-v:refname"],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    tags = [line.strip() for line in _text(result.stdout).splitlines()]
    return [tag for tag in tags if TAG_PATTERN.match(tag)][:limit]


def dispatch_arguments(
    inputs: tuple,
    environment: str,
    ref: str = "",
    label: str = "",
    branch: Optional[str] = None,
) -> list:
    """``gh workflow run`` with exactly the inputs deploy.yml declares."""
    arguments = ["workflow", "run", WORKFLOW]
    if branch:
        arguments += ["--ref", branch]
    values = {"environment": environment, "ref": ref, "label": label}
    for name in inputs:
        value = values.get(name, "")
        if name == "environment" or value:
            arguments += ["-f", f"{name}={value}"]
    return arguments


def dispatch(root: Path, arguments: list, runner=subprocess.run) -> None:
    result = gh(root, arguments, runner)
    if result.returncode != 0:
        raise DeployError(_text(result.stderr).strip() or "gh workflow run failed")


def parse_runs(text: str) -> list:
    try:
        document = json.loads(text or "[]")
    except ValueError:
        return []
    runs = []
    for item in document if isinstance(document, list) else []:
        if not isinstance(item, dict) or "databaseId" not in item:
            continue
        runs.append(
            Run(
                id=int(item["databaseId"]),
                url=str(item.get("url", "")),
                status=str(item.get("status", "")),
                conclusion=item.get("conclusion") or None,
            )
        )
    return runs


def find_run(
    root: Path,
    branch: Optional[str],
    known: set,
    runner=subprocess.run,
    attempts: int = 10,
    sleeper=time.sleep,
) -> Run:
    """The run that appeared after the dispatch — GitHub registers it a moment later."""
    arguments = [
        "run",
        "list",
        "--workflow",
        WORKFLOW,
        "--limit",
        "5",
        "--json",
        "databaseId,url,status,conclusion",
    ]
    if branch:
        arguments += ["--branch", branch]
    for attempt in range(attempts):
        result = gh(root, arguments, runner)
        for run in parse_runs(_text(result.stdout)):
            if run.id not in known:
                return run
        sleeper(2)
    raise DeployError(
        "the dispatch was accepted but no new run appeared; check the Actions tab"
    )


def list_run_ids(root: Path, branch: Optional[str], runner=subprocess.run) -> set:
    arguments = [
        "run",
        "list",
        "--workflow",
        WORKFLOW,
        "--limit",
        "5",
        "--json",
        "databaseId",
    ]
    if branch:
        arguments += ["--branch", branch]
    result = gh(root, arguments, runner)
    return {run.id for run in parse_runs(_text(result.stdout))}


def watch(root: Path, run: Run, runner=subprocess.run) -> int:
    """Stream ``gh run watch`` to the terminal; exit code follows the run."""
    try:
        completed = runner(
            ["gh", "run", "watch", str(run.id), "--exit-status"],
            cwd=str(root),
            check=False,
        )
    except FileNotFoundError as error:
        raise DeployError(
            "gh (GitHub CLI) is not installed on this workstation"
        ) from error
    return completed.returncode


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
