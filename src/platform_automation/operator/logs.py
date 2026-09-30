"""Follow the logs of an application's containers from the workstation.

Read-only by construction: the only Docker verbs used are ``ps`` and
``logs``. Nothing is installed on the host, and the environment of a
container — where the secrets are — is never printed.
"""

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PROXY_CONTAINERS = (
    ("platform-nginx", "nginx (every site on this host)"),
    ("platform-docker-gen", "docker-gen (proxy config renders)"),
    ("platform-acme-companion", "acme-companion (certificates)"),
)
NGINX_CONTAINER = "platform-nginx"
TAIL_LINES = 200
HOST_PATTERN = re.compile(r"^[a-z0-9.-]+$")


class LogsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Container:
    name: str
    service: str
    project: str
    status: str

    @property
    def running(self) -> bool:
        return self.status.lower().startswith("up")


def ssh_command(
    host: str,
    user: str,
    remote: str,
    identity: Optional[Path] = None,
    tty: bool = False,
) -> list:
    command = ["ssh", "-o", "ConnectTimeout=10"]
    command.append("-t" if tty else "-oBatchMode=yes")
    if identity is not None:
        command += ["-i", str(identity)]
    return [*command, f"{user}@{host}", "--", remote]


PS_FORMAT = '{{.Names}}\\t{{.Label "com.docker.compose.service"}}\\t{{.Label "com.docker.compose.project"}}\\t{{.Status}}'


def ps_remote(projects: Optional[list] = None) -> str:
    """``docker ps -a`` for the given Compose projects, or every container."""
    parts = ["sudo", "-n", "docker", "ps", "--all", "--format", PS_FORMAT]
    if projects:
        commands = [
            shlex.join([*parts, "--filter", f"label=com.docker.compose.project={name}"])
            for name in projects
        ]
        return " ; ".join(commands)
    return shlex.join(parts)


def parse_containers(output: str) -> list:
    containers = []
    seen = set()
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 4 or not fields[0] or fields[0] in seen:
            continue
        seen.add(fields[0])
        containers.append(Container(fields[0], fields[1], fields[2], fields[3]))
    return containers


def list_containers(
    host: str,
    user: str,
    projects: Optional[list] = None,
    identity: Optional[Path] = None,
    runner=subprocess.run,
) -> list:
    try:
        result = runner(
            ssh_command(host, user, ps_remote(projects), identity),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LogsError(f"ssh could not run: {error}") from error
    if result.returncode != 0:
        tail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise LogsError(tail[-1] if tail else "docker ps failed on the host")
    return parse_containers(result.stdout.decode("utf-8", "replace"))


def follow_container(container: str, tail: int = TAIL_LINES) -> str:
    return shlex.join(
        [
            "sudo",
            "-n",
            "docker",
            "logs",
            "--follow",
            "--timestamps",
            "--tail",
            str(tail),
            container,
        ]
    )


def follow_project(project: str, tail: int = 100) -> str:
    """Every service of one Compose project, interleaved, prefixed by service."""
    return shlex.join(
        [
            "sudo",
            "-n",
            "docker",
            "compose",
            "--project-name",
            project,
            "logs",
            "--follow",
            "--tail",
            str(tail),
        ]
    )


def follow_nginx_for(domains: list, tail: int = 2000) -> str:
    """The shared access log, narrowed to one application's hosts.

    nginx-proxy's ``vhost`` log format starts every access line with the
    host, so an anchored match is exact; error lines name the server.
    """
    hosts = [d for d in domains if HOST_PATTERN.fullmatch(d)]
    if not hosts:
        raise LogsError("the manifest names no domains to filter the proxy log by")
    # Hosts are [a-z0-9.-] only, so the dot is the one character to escape;
    # re.escape would also write "\\-", which GNU grep warns about.
    escaped = "|".join(host.replace(".", "\\.") for host in hosts)
    pattern = f"^({escaped}) |server: ({escaped})[,;]"
    logs = shlex.join(
        [
            "sudo",
            "-n",
            "docker",
            "logs",
            "--follow",
            "--tail",
            str(tail),
            NGINX_CONTAINER,
        ]
    )
    return f"{logs} 2>&1 | grep --line-buffered -E {shlex.quote(pattern)}"
