"""Generic repository and release boundary policy.

Customer-specific values must never be stored here. Exact private markers can be
supplied at runtime through ``CORE_FORBIDDEN_MARKERS`` as newline-separated text
or through the newline-separated file named by ``CORE_FORBIDDEN_MARKERS_FILE``.
"""

import ipaddress
import os
import re
from pathlib import PurePosixPath
from typing import Iterable, List


IPV4_PATTERN = re.compile(rb"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
IPV6_CANDIDATE_PATTERN = re.compile(
    rb"(?<![A-Za-z0-9_:])(?:[0-9A-Fa-f]*:){2,7}[0-9A-Fa-f]*" rb"(?![A-Za-z0-9_:])"
)
EMAIL_PATTERN = re.compile(rb"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

# A systemd template instance name puts an at-sign between the unit and the
# instance, which the address pattern above reads as an email. The exemption is
# keyed on the trailing unit type, so it cannot be widened into a real domain.
SYSTEMD_UNIT_TYPES = (
    "automount",
    "device",
    "mount",
    "path",
    "scope",
    "service",
    "slice",
    "socket",
    "swap",
    "target",
    "timer",
)
AGE_RECIPIENT_PATTERN = re.compile(rb"\bage1[0-9a-z]{20,}\b")
FQDN_PATTERN = re.compile(
    rb"(?<![A-Za-z0-9_-])"
    rb"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    rb"(?:ai|at|au|bg|biz|ca|ch|cloud|co|com|cz|de|dev|ee|es|eu|fr|ge|info|"
    rb"io|it|lt|lv|me|net|nl|org|pl|ro|ru|sk|tech|ua|uk|us|xyz)"
    rb"(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)

SECRET_PATTERNS = (
    re.compile(rb"BEGIN (?:RSA|OPENSSH|EC|DSA) PRIVATE KEY"),
    re.compile(rb"AGE-SECRET-KEY-1[0-9A-Z]+"),
    re.compile(rb"(?:github_pat_|ghp_)[A-Za-z0-9_]{20,}"),
    re.compile(rb"tskey-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"aws_secret_access_key\s*[:=]", re.IGNORECASE),
)

LOCAL_PATH_PATTERNS = (
    re.compile(rb"/Users/[A-Za-z0-9._-]+/"),
    re.compile(rb"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\"),
)

ALLOWED_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/32",
        "10.0.0.0/8",
        "127.0.0.0/8",
        "172.16.0.0/12",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.51.100.0/24",
        "203.0.113.0/24",
    )
)

ALLOWED_IPV6_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "::/128",
        "::1/128",
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
    )
)

# Keep this split: the boundary policy scans its own source and rejects the literal.
TAILSCALE_IPV6_PREFIX = ":".join(("fd7a", "115c", "a1e0", "", "")) + "/48"
FORBIDDEN_IPV6_NETWORKS = (ipaddress.ip_network(TAILSCALE_IPV6_PREFIX),)

ALLOWED_FQDN_SUFFIXES = (
    ".invalid",
    ".test",
)

ALLOWED_FQDNS = {
    "containerd.io",
    "docker.com",
    "docker.io",
    "example.com",
    "example.net",
    "example.org",
    "ghcr.io",
    "github.com",
    "json-schema.org",
    "letsencrypt.org",
    "pypi.org",
    "tailscale.com",
}

FORBIDDEN_PATH_PARTS = {
    ".DS_Store",
    "local-secrets.yml",
}


def runtime_forbidden_markers() -> List[bytes]:
    values = [os.environ.get("CORE_FORBIDDEN_MARKERS", "")]
    marker_file = os.environ.get("CORE_FORBIDDEN_MARKERS_FILE", "").strip()
    if marker_file:
        with open(marker_file, encoding="utf-8") as source:
            values.append(source.read())

    markers = {
        line.strip().encode("utf-8")
        for value in values
        for line in value.splitlines()
        if line.strip()
    }
    if not markers and os.environ.get("CORE_REQUIRE_FORBIDDEN_MARKERS", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        raise ValueError(
            "CORE_FORBIDDEN_MARKERS or CORE_FORBIDDEN_MARKERS_FILE must provide "
            "at least one marker"
        )

    return sorted(markers)


def is_allowed_fqdn(domain: str) -> bool:
    normalized = domain.lower().rstrip(".")
    if normalized.endswith(ALLOWED_FQDN_SUFFIXES):
        return True
    return any(
        normalized == allowed or normalized.endswith(f".{allowed}")
        for allowed in ALLOWED_FQDNS
    )


def validate_path(name: str, forbid_inventory: bool = False) -> List[str]:
    member = PurePosixPath(name)
    issues: List[str] = []

    if member.is_absolute() or ".." in member.parts:
        issues.append("unsafe absolute or parent-relative path")

    forbidden_parts = set(FORBIDDEN_PATH_PARTS)
    if forbid_inventory:
        forbidden_parts.add("inventory")

    forbidden = forbidden_parts.intersection(member.parts)
    if forbidden:
        issues.append(f"forbidden path component {sorted(forbidden)[0]}")

    if member.suffix in {".agekey", ".pem", ".tfstate"}:
        issues.append(f"forbidden private file extension {member.suffix}")

    return issues


def validate_content(content: bytes) -> List[str]:
    issues: List[str] = []

    for pattern in SECRET_PATTERNS:
        if pattern.search(content):
            issues.append("private key or credential pattern")

    for pattern in LOCAL_PATH_PATTERNS:
        if pattern.search(content):
            issues.append("controller-local user path")

    for match in IPV4_PATTERN.finditer(content):
        try:
            address = ipaddress.ip_address(match.group().decode("ascii"))
        except ValueError:
            continue

        if not any(address in network for network in ALLOWED_IPV4_NETWORKS):
            issues.append("non-documentation IPv4 address")

    for match in IPV6_CANDIDATE_PATTERN.finditer(content):
        candidate = match.group()
        try:
            address = ipaddress.ip_address(candidate.decode("ascii"))
        except ValueError:
            continue

        if address.version == 6:
            forbidden = any(address in network for network in FORBIDDEN_IPV6_NETWORKS)
            allowed = any(address in network for network in ALLOWED_IPV6_NETWORKS)
            if forbidden or not allowed:
                issues.append("non-documentation IPv6 address")

    for match in EMAIL_PATTERN.finditer(content):
        domain = match.group(1).decode("ascii").lower()

        if domain.rpartition(".")[2] in SYSTEMD_UNIT_TYPES:
            continue

        if not domain.endswith(".invalid"):
            issues.append("non-documentation email address")

    for match in FQDN_PATTERN.finditer(content):
        domain = match.group().decode("ascii").lower()
        if not is_allowed_fqdn(domain):
            issues.append("non-allowlisted FQDN")

    if AGE_RECIPIENT_PATTERN.search(content):
        issues.append("committed age recipient")

    lowered = content.lower()
    for marker in runtime_forbidden_markers():
        if marker.lower() in lowered:
            issues.append("runtime-supplied private marker")

    return sorted(set(issues))


def describe_issues(issues: Iterable[str]) -> str:
    return ", ".join(sorted(set(issues)))
