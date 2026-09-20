"""Which Compose service a manifest domain belongs to.

A domain belongs to ``service.web`` unless it names another service. The
proxy learns a domain from the container that carries it in ``VIRTUAL_HOST``,
so every service gets the hosts that are its own — the web service through
``PLATFORM_VIRTUAL_HOSTS``, any helper through a variable named after it —
and no host is ever claimed by two containers.
"""

import re
from typing import Any

VARIABLE_SUFFIX = re.compile(r"[^A-Z0-9]")


def domain_service(manifest: dict[str, Any], domain: dict[str, Any]) -> str:
    return domain.get("service") or manifest["service"]["web"]


def web_domains(manifest: dict[str, Any]) -> list:
    web = manifest["service"]["web"]
    return [d for d in manifest["domains"] if domain_service(manifest, d) == web]


def helper_domains(manifest: dict[str, Any]) -> dict:
    """service name → its domains, for every service that is not the web one."""
    web = manifest["service"]["web"]
    grouped: dict = {}
    for domain in manifest["domains"]:
        service = domain_service(manifest, domain)
        if service != web:
            grouped.setdefault(service, []).append(domain)
    return grouped


def variable_suffix(service: str) -> str:
    """``mail-pit`` → ``MAIL_PIT``: what a Compose file can reference."""
    return VARIABLE_SUFFIX.sub("_", service.upper())


def helper_host_variables(manifest: dict[str, Any]) -> dict:
    variables = {}
    for service, domains in helper_domains(manifest).items():
        suffix = variable_suffix(service)
        variables[f"PLATFORM_VIRTUAL_HOSTS_{suffix}"] = ",".join(
            d["host"] for d in domains
        )
        variables[f"PLATFORM_TLS_HOSTS_{suffix}"] = ",".join(
            d["host"] for d in domains if d["tls"]
        )
    return variables
