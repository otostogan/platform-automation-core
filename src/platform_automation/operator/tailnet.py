"""What the workstation's own Tailscale client knows.

Read before any SSH attempt, because the five conditions convergence checks on
the host — connected, resolvable, the right user, the right address — can all
be answered here first, with a better error than a hung connection.
"""

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional

TAILSCALE_EXECUTABLE = "tailscale"


@dataclass(frozen=True)
class Peer:
    dns_name: str
    host_name: str
    online: bool
    tags: tuple


@dataclass(frozen=True)
class Tailnet:
    available: bool
    backend_state: Optional[str] = None
    self_dns: Optional[str] = None
    suffix: Optional[str] = None
    peers: dict = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.available and self.backend_state == "Running"


def read_tailnet(
    runner=subprocess.run, executable: str = TAILSCALE_EXECUTABLE
) -> Tailnet:
    try:
        result = runner(
            [executable, "status", "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return Tailnet(available=False, error=f"tailscale is not available: {error}")

    if result.returncode != 0:
        return Tailnet(available=False, error="tailscale status failed")

    try:
        document = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return Tailnet(
            available=False, error="tailscale status did not answer with JSON"
        )

    return parse_status(document)


def parse_status(document: Any) -> Tailnet:
    if not isinstance(document, dict):
        return Tailnet(available=False, error="tailscale status is not an object")

    own = document.get("Self") if isinstance(document.get("Self"), dict) else {}
    peers = {}

    for raw in (document.get("Peer") or {}).values():
        if not isinstance(raw, dict):
            continue
        peer = Peer(
            dns_name=_strip_dot(raw.get("DNSName")),
            host_name=str(raw.get("HostName") or ""),
            online=bool(raw.get("Online")),
            tags=tuple(str(tag) for tag in (raw.get("Tags") or [])),
        )
        if peer.dns_name:
            peers[peer.dns_name] = peer

    return Tailnet(
        available=True,
        backend_state=document.get("BackendState"),
        self_dns=_strip_dot(own.get("DNSName")) or None,
        suffix=_strip_dot(document.get("MagicDNSSuffix")) or None,
        peers=peers,
    )


def find_peer(tailnet: Tailnet, name: str) -> Optional[Peer]:
    """Match a MagicDNS name or a bare host name, however the inventory spelled it."""
    wanted = _strip_dot(name).lower()

    for dns_name, peer in tailnet.peers.items():
        if dns_name.lower() == wanted or peer.host_name.lower() == wanted:
            return peer

    if tailnet.suffix:
        with_suffix = f"{wanted}.{tailnet.suffix.lower()}"
        for dns_name, peer in tailnet.peers.items():
            if dns_name.lower() == with_suffix:
                return peer

    return None


def _strip_dot(value: Any) -> str:
    return str(value).rstrip(".") if isinstance(value, str) else ""
