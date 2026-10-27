#!/usr/bin/env python3

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .contract_resources import contract_path

DEFAULT_SCHEMA = contract_path("platform-v1.schema.json")

PLATFORM_IMAGE_PATTERN = re.compile(r"\$\{PLATFORM_IMAGE(?::\?[^}]*)?\}")
PLATFORM_DB_NETWORK_PATTERN = re.compile(r"\$\{PLATFORM_DB_NETWORK(?::\?[^}]*)?\}")
IMMUTABLE_IMAGE_PATTERN = re.compile(r".+@sha256:[0-9a-f]{64}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a platform/v1 application contract.",
    )
    parser.add_argument(
        "manifest",
        type=Path,
        help="Path to deploy/platform.yml.",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA,
        help=f"JSON Schema path. Default: {DEFAULT_SCHEMA}",
    )
    parser.add_argument(
        "--app-root",
        type=Path,
        help=(
            "Application repository root. "
            "Default: parent of the manifest's deploy directory."
        ),
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        value = json.load(file)

    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")

    return value


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as file:
        return yaml.safe_load(file)


def format_path(path: list[Any]) -> str:
    result = "$"

    for item in path:
        if isinstance(item, int):
            result += f"[{item}]"
        else:
            result += f".{item}"

    return result


def validate_unique_domain_hosts(manifest: Any) -> list[str]:
    if not isinstance(manifest, dict):
        return []

    domains = manifest.get("domains")

    if not isinstance(domains, list):
        return []

    errors: list[str] = []
    first_occurrence: dict[str, int] = {}

    for index, domain in enumerate(domains):
        if not isinstance(domain, dict):
            continue

        host = domain.get("host")

        if not isinstance(host, str):
            continue

        if host in first_occurrence:
            errors.append(
                f"$.domains[{index}].host: duplicate domain host "
                f"{host}; first declared at "
                f"$.domains[{first_occurrence[host]}].host"
            )
        else:
            first_occurrence[host] = index

    return errors


def validate_manifest(
    manifest: Any,
    schema: dict[str, Any],
) -> list[str]:
    validator = Draft202012Validator(schema)
    errors = sorted(
        validator.iter_errors(manifest),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )

    formatted_errors = [
        f"{format_path(list(error.absolute_path))}: {error.message}" for error in errors
    ]

    formatted_errors.extend(validate_unique_domain_hosts(manifest))
    formatted_errors.extend(validate_web_domain_present(manifest))
    formatted_errors.extend(validate_database_resources(manifest))
    formatted_errors.extend(validate_no_database(manifest))
    formatted_errors.extend(validate_auth_paths(manifest))

    return formatted_errors


def validate_no_database(manifest: Any) -> list[str]:
    """Name the field the schema refuses without naming it.

    With ``database.mode: none`` the schema forbids ``restore_validation``
    — nothing can be restored, so a query there is a promise nobody keeps —
    but a ``false`` subschema reports at the manifest root, so the path is
    spelled out here.
    """
    try:
        mode = manifest["database"]["mode"]
    except (KeyError, TypeError):
        return []
    if mode != "none" or "restore_validation" not in manifest:
        return []
    return [
        "$.restore_validation: not allowed with database.mode none —"
        " there is no database to restore"
    ]


# One path component on the host's filesystem; the suffix must fit beside the host.
NAME_MAX = 255
HTPASSWD_PATHS_SUFFIX = "_paths"


def validate_auth_paths(manifest: Any) -> list[str]:
    """What the schema cannot see about ``auth.paths``: the metrics path and the file name.

    The metrics guard already writes a ``location`` pair for the metrics path
    when it is served on the proxied port; a second pair for the same path
    would make nginx refuse the configuration. And the hash file is named
    after the host plus a suffix, which a near-limit host name cannot carry.
    """
    try:
        domains = manifest["domains"]
        service = manifest["service"]
        web = service["web"]
    except (KeyError, TypeError):
        return []
    if not isinstance(domains, list) or not isinstance(service, dict):
        return []
    metrics = service.get("metrics")
    guarded = (
        metrics["path"]
        if isinstance(metrics, dict)
        and metrics.get("port") == service.get("internal_port")
        and isinstance(metrics.get("path"), str)
        else None
    )
    errors = []
    for index, domain in enumerate(domains):
        if not isinstance(domain, dict):
            continue
        auth = domain.get("auth")
        paths = auth.get("paths") if isinstance(auth, dict) else None
        if not isinstance(paths, list) or not paths:
            continue
        host = domain.get("host")
        if isinstance(host, str) and len(host) + len(HTPASSWD_PATHS_SUFFIX) > NAME_MAX:
            errors.append(
                f"$.domains[{index}].host: with auth.paths the host name must be at most"
                f" {NAME_MAX - len(HTPASSWD_PATHS_SUFFIX)} characters"
            )
        if guarded is None or domain.get("service", web) != web:
            continue
        # The guard answers 404 for the metrics path and everything under it;
        # a protected location there would be more specific and proxy instead.
        for path in paths:
            if isinstance(path, str) and (
                path == guarded or path.startswith(guarded + "/")
            ):
                errors.append(
                    f"$.domains[{index}].auth.paths: {path} is under the metrics"
                    f" path {guarded}, which the proxy never serves"
                )
    return errors


MINIMUM_DATABASE_MEMORY_MB = 256


def validate_database_resources(manifest: Any) -> list[str]:
    """The numbers the schema cannot relate: buffers against the limit.

    PostgreSQL keeps more than its buffers in memory — every connection,
    sorts, the maintenance work — so buffers above half the limit leave the
    rest nothing, and the kernel kills the server at the first busy moment.
    """
    try:
        resources = manifest["database"]["resources"]
    except (KeyError, TypeError):
        return []
    if not isinstance(resources, dict):
        return []
    pattern = re.compile(r"^[1-9][0-9]*[mg]$")
    values = {}
    for key in ("memory", "shared_buffers"):
        value = resources.get(key)
        if isinstance(value, str) and pattern.fullmatch(value):
            values[key] = int(value[:-1]) * (1024 if value.endswith("g") else 1)
    errors = []
    memory = values.get("memory")
    if memory is not None and memory < MINIMUM_DATABASE_MEMORY_MB:
        errors.append(
            f"$.database.resources.memory: at least {MINIMUM_DATABASE_MEMORY_MB}m"
        )
    buffers = values.get("shared_buffers")
    if memory is not None and buffers is not None and buffers * 2 > memory:
        errors.append("$.database.resources.shared_buffers: at most half of memory")
    return errors


def validate_web_domain_present(manifest: Any) -> list[str]:
    """The healthcheck and the workflow's check both address the web service by a domain."""
    try:
        web = manifest["service"]["web"]
        domains = manifest["domains"]
    except (KeyError, TypeError):
        return []
    if not isinstance(domains, list) or not all(isinstance(d, dict) for d in domains):
        return []
    if any((d.get("service") or web) == web for d in domains):
        return []
    return ["$.domains: at least one domain must belong to the web service"]


def resolve_compose_path(
    app_root: Path,
    compose_file: str,
) -> Path:
    resolved_root = app_root.resolve()
    resolved_compose = (resolved_root / compose_file).resolve()

    try:
        resolved_compose.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(
            f"compose file escapes application root: {compose_file}"
        ) from error

    return resolved_compose


def service_network_names(service: dict[str, Any]) -> set[str]:
    networks = service.get("networks", [])

    if isinstance(networks, list):
        return {network for network in networks if isinstance(network, str)}

    if isinstance(networks, dict):
        return set(networks)

    return set()


def is_allowed_image(image: Any) -> bool:
    if not isinstance(image, str):
        return False

    return bool(
        PLATFORM_IMAGE_PATTERN.fullmatch(image)
        or IMMUTABLE_IMAGE_PATTERN.fullmatch(image)
    )


def validate_compose(
    manifest: dict[str, Any],
    compose: Any,
) -> list[str]:
    errors: list[str] = []

    if not isinstance(compose, dict):
        return ["$.compose: Compose file must contain an object"]

    services = compose.get("services")

    if not isinstance(services, dict) or not services:
        return ["$.compose.services: must contain at least one service"]

    for service_name, service in services.items():
        path = f"$.compose.services.{service_name}"

        if not isinstance(service, dict):
            errors.append(f"{path}: service must contain an object")
            continue

        if "build" in service:
            errors.append(f"{path}.build: build is forbidden")

        if "ports" in service:
            errors.append(f"{path}.ports: host port publishing is forbidden")

        if service.get("network_mode") == "host":
            errors.append(f"{path}.network_mode: host networking is forbidden")

        if not is_allowed_image(service.get("image")):
            errors.append(
                f"{path}.image: must use PLATFORM_IMAGE "
                "or an immutable sha256 digest"
            )

    web_service_name = manifest["service"]["web"]
    web_service = services.get(web_service_name)

    if not isinstance(web_service, dict):
        errors.append(
            f"$.service.web: Compose service " f"{web_service_name!r} does not exist"
        )
    else:
        image = web_service.get("image")

        if not (isinstance(image, str) and PLATFORM_IMAGE_PATTERN.fullmatch(image)):
            errors.append(
                f"$.compose.services.{web_service_name}.image: "
                "web service must use PLATFORM_IMAGE"
            )

        exposed_ports = {
            str(port).split("/", maxsplit=1)[0]
            for port in web_service.get("expose", [])
            if isinstance(port, (str, int))
        }
        internal_port = str(manifest["service"]["internal_port"])

        if internal_port not in exposed_ports:
            errors.append(
                f"$.compose.services.{web_service_name}.expose: "
                f"must expose internal port {internal_port}"
            )

        metrics = manifest["service"].get("metrics")

        if metrics is not None and str(metrics["port"]) not in exposed_ports:
            errors.append(
                f"$.compose.services.{web_service_name}.expose: "
                f"must expose metrics port {metrics['port']}"
            )

        if "edge" not in service_network_names(web_service):
            errors.append(
                f"$.compose.services.{web_service_name}.networks: "
                "web service must join edge"
            )

    for index, domain in enumerate(manifest["domains"]):
        owner = domain.get("service")
        if not owner or owner == web_service_name:
            continue
        holder = services.get(owner)
        if not isinstance(holder, dict):
            errors.append(
                f"$.domains[{index}].service: Compose service {owner!r} does not exist"
            )
        elif "edge" not in service_network_names(holder):
            # docker-gen only sees containers on platform-edge; a domain on a
            # service outside it would be accepted and never answer.
            errors.append(
                f"$.compose.services.{owner}.networks: "
                f"service carrying {domain['host']} must join edge"
            )

    migration_service = manifest["deployment"].get("migration_service")

    if migration_service:
        migration = services.get(migration_service)

        if not isinstance(migration, dict):
            errors.append(
                f"$.deployment.migration_service: Compose service "
                f"{migration_service!r} does not exist"
            )
        else:
            image = migration.get("image")

            if not (isinstance(image, str) and PLATFORM_IMAGE_PATTERN.fullmatch(image)):
                errors.append(
                    f"$.compose.services.{migration_service}.image: "
                    "migration service must use PLATFORM_IMAGE"
                )

    networks = compose.get("networks")
    edge = networks.get("edge") if isinstance(networks, dict) else None

    if not isinstance(edge, dict):
        errors.append("$.compose.networks.edge: external edge network is required")
    else:
        if edge.get("external") is not True:
            errors.append("$.compose.networks.edge.external: must be true")

        if edge.get("name") != "platform-edge":
            errors.append("$.compose.networks.edge.name: " "must be 'platform-edge'")

    errors.extend(validate_database_network(manifest, services, networks))

    return errors


def validate_database_network(
    manifest: dict[str, Any],
    services: dict[str, Any],
    networks: Any,
) -> list[str]:
    # A docker-mode application must reach its database, and it does so
    # through the PLATFORM_DB_NETWORK interpolation, so the compose file
    # itself stays environment-agnostic. An application with an external
    # database, or none at all, referencing that variable would start with
    # an empty network name, so it is refused here, where the mistake is
    # legible.
    mode = manifest["database"]["mode"]
    db = networks.get("db") if isinstance(networks, dict) else None
    errors: list[str] = []

    if mode != "docker":
        # The key "db" stays free for application-owned helpers; only a
        # reference to the platform interpolation is a contradiction here.
        reason = (
            "for an external database"
            if mode == "external"
            else "when the manifest declares no database"
        )
        if isinstance(networks, dict):
            for key, network in networks.items():
                name = network.get("name") if isinstance(network, dict) else None

                if isinstance(name, str) and PLATFORM_DB_NETWORK_PATTERN.fullmatch(
                    name
                ):
                    errors.append(
                        f"$.compose.networks.{key}.name: PLATFORM_DB_NETWORK "
                        f"is forbidden {reason}"
                    )
        return errors

    if not isinstance(db, dict):
        return ["$.compose.networks.db: external db network is required"]

    if db.get("external") is not True:
        errors.append("$.compose.networks.db.external: must be true")

    name = db.get("name")

    if not (isinstance(name, str) and PLATFORM_DB_NETWORK_PATTERN.fullmatch(name)):
        errors.append("$.compose.networks.db.name: must use PLATFORM_DB_NETWORK")

    web_service_name = manifest["service"]["web"]
    required_members = {web_service_name: "web service"}
    migration_service = manifest["deployment"].get("migration_service")

    # Migrations run in their own service via `compose run`; a migration
    # service outside the db network would fail on the first connection.
    if migration_service:
        required_members.setdefault(migration_service, "migration service")

    for service_name, role in required_members.items():
        service = services.get(service_name)

        if isinstance(service, dict) and "db" not in service_network_names(service):
            errors.append(
                f"$.compose.services.{service_name}.networks: " f"{role} must join db"
            )

    return errors


def print_errors(
    title: str,
    path: Path,
    errors: list[str],
) -> None:
    print(f"invalid {title}: {path}", file=sys.stderr)

    for error in errors:
        print(f"  - {error}", file=sys.stderr)


def main() -> int:
    arguments = parse_arguments()

    try:
        schema = load_json(arguments.schema)
        Draft202012Validator.check_schema(schema)
        manifest = load_yaml(arguments.manifest)
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        yaml.YAMLError,
        SchemaError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    manifest_errors = validate_manifest(manifest, schema)

    if manifest_errors:
        print_errors(
            "manifest",
            arguments.manifest,
            manifest_errors,
        )
        return 1

    app_root = (
        arguments.app_root
        if arguments.app_root is not None
        else arguments.manifest.resolve().parent.parent
    )

    try:
        compose_path = resolve_compose_path(
            app_root,
            manifest["compose_file"],
        )
        compose = load_yaml(compose_path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    compose_errors = validate_compose(manifest, compose)

    if compose_errors:
        print_errors("Compose contract", compose_path, compose_errors)
        return 1

    print(f"valid application contract: {arguments.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
