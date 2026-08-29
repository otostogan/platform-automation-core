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

    return formatted_errors


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

        if "edge" not in service_network_names(web_service):
            errors.append(
                f"$.compose.services.{web_service_name}.networks: "
                "web service must join edge"
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
    # itself stays environment-agnostic. An external-mode application
    # referencing that variable would start with an empty network name,
    # so it is refused here, where the mistake is legible.
    mode = manifest["database"]["mode"]
    db = networks.get("db") if isinstance(networks, dict) else None
    errors: list[str] = []

    if mode != "docker":
        # The key "db" stays free for application-owned helpers; only a
        # reference to the platform interpolation is a contradiction here.
        if isinstance(networks, dict):
            for key, network in networks.items():
                name = network.get("name") if isinstance(network, dict) else None

                if isinstance(name, str) and PLATFORM_DB_NETWORK_PATTERN.fullmatch(
                    name
                ):
                    errors.append(
                        f"$.compose.networks.{key}.name: PLATFORM_DB_NETWORK "
                        "is forbidden for an external database"
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
