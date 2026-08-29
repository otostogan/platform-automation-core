#!/usr/bin/env python3

"""Run the database an application declared, without letting it own one.

With ``database.mode: docker`` the application states only that it needs a
PostgreSQL instance. Everything about how that instance exists — image,
volume, network, credential — is decided here, so backups can later operate
on objects the platform created and fully understands.

The generated Compose model is host state, like the release ledger: it lives
under ``/var/lib/platform/databases`` and is never part of a bundle. The
credential is generated on first deploy and stored SOPS-encrypted to the same
age recipients as the application's own secrets, escrow included, so the bus
factor of the database password equals the bus factor of everything else.
"""

import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml

from .runtime_secrets import (
    RuntimeSecretsError,
    decrypt_sops_document,
    validate_runtime_identity,
)
from .sops_validation import valid_age_recipients

DEFAULT_DATABASES_ROOT = Path("/var/lib/platform/databases")

DATABASE_SERVICE = "postgres"
DATABASE_ALIAS = "db"
DATABASE_NAME = "app"
DATABASE_USER = "app"
DATABASE_PORT = 5432

CREDENTIALS_FILENAME = "credentials.sops.json"
COMPOSE_FILENAME = "compose.yml"
DATABASE_ENV_FILENAME = "database.env"

PASSWORD_ENTROPY_BYTES = 32
SUPPORTED_POSTGRES_MAJORS = (16, 17, 18)


class DatabaseRuntimeError(RuntimeError):
    pass


def database_resource_name(project: str, environment: str) -> str:
    """One name for the Compose project, the network, and the volume."""
    return f"platform-db-{project}-{environment}"


def database_container_name(project: str, environment: str) -> str:
    """Compose names one replica of one service predictably."""
    return f"{database_resource_name(project, environment)}-{DATABASE_SERVICE}-1"


def database_volume_exists(
    project: str,
    environment: str,
    docker_executable: Path,
    runner=subprocess.run,
) -> bool:
    """Report whether data already exists for this project and environment."""
    try:
        result = runner(
            [
                str(docker_executable),
                "volume",
                "inspect",
                database_resource_name(project, environment),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DatabaseRuntimeError("database volume could not be inspected") from error

    return result.returncode == 0


def existing_postgres_major(
    existing_compose: Optional[dict[str, Any]]
) -> Optional[int]:
    try:
        image = existing_compose["services"][DATABASE_SERVICE]["image"]
    except (KeyError, TypeError):
        return None

    if not isinstance(image, str) or not image.startswith("postgres:"):
        return None

    major = image[len("postgres:") :].partition("@")[0]

    return int(major) if major.isdigit() else None


def stored_database_password(
    databases_root: Path,
    project: str,
    environment: str,
    age_key_file: Path,
    sops_executable: Path,
    runner=subprocess.run,
) -> str:
    """Read the credential without touching Docker or generating anything."""
    credentials_path = databases_root / project / environment / CREDENTIALS_FILENAME

    if credentials_path.is_symlink() or not credentials_path.is_file():
        raise DatabaseRuntimeError("database credential is missing")

    try:
        document = decrypt_sops_document(
            credentials_path,
            age_key_file=age_key_file,
            sops_executable=sops_executable,
            runner=runner,
        )
    except RuntimeSecretsError as error:
        raise DatabaseRuntimeError(str(error)) from error

    password = document.get("password")

    if not isinstance(password, str) or not password:
        raise DatabaseRuntimeError("stored database credential is invalid")

    return password


def database_url(password: str) -> str:
    return (
        f"postgresql://{DATABASE_USER}:{password}"
        f"@{DATABASE_ALIAS}:{DATABASE_PORT}/{DATABASE_NAME}"
    )


def declared_database_mode(manifest: dict[str, Any]) -> str:
    return manifest["database"]["mode"]


def create_private_database_directory(
    databases_root: Path,
    project: str,
    environment: str,
) -> Path:
    if databases_root.is_symlink():
        raise DatabaseRuntimeError("databases root cannot be a symbolic link")

    databases_root.mkdir(
        mode=0o750,
        parents=True,
        exist_ok=True,
    )

    current = databases_root.resolve()

    for part in (project, environment):
        current = current / part

        if current.is_symlink():
            raise DatabaseRuntimeError("database path cannot contain a symbolic link")

        current.mkdir(mode=0o700, exist_ok=True)
        current.chmod(0o700)

    return current


def write_private_file(destination: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_name)

    try:
        os.fchmod(descriptor, 0o600)

        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary_path, destination)
        destination.chmod(0o600)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def generate_password() -> str:
    # token_urlsafe stays within [A-Za-z0-9_-], so the password needs no
    # escaping inside a connection URL.
    return secrets.token_urlsafe(PASSWORD_ENTROPY_BYTES)


def encrypt_credentials(
    password: str,
    recipients: set[str],
    destination: Path,
    scratch_directory: Path,
    sops_executable: Path,
    runner=subprocess.run,
) -> None:
    """Encrypt the credential to explicit recipients, plaintext on tmpfs only."""
    if not recipients:
        raise DatabaseRuntimeError(
            "database credential needs at least one age recipient"
        )

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".credentials.",
        suffix=".json",
        dir=scratch_directory,
    )
    temporary_path = Path(temporary_name)

    try:
        os.fchmod(descriptor, 0o600)

        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump({"password": password}, file)

        try:
            result = runner(
                [
                    str(sops_executable),
                    "encrypt",
                    "--age",
                    ",".join(sorted(recipients)),
                    "--input-type",
                    "json",
                    "--output-type",
                    "json",
                    str(temporary_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DatabaseRuntimeError(
                "database credential encryption could not be executed"
            ) from error

        if result.returncode != 0 or not result.stdout.strip():
            raise DatabaseRuntimeError("database credential encryption failed")
    finally:
        temporary_path.unlink(missing_ok=True)

    write_private_file(destination, result.stdout)


def read_stored_recipients(credentials_path: Path) -> set[str]:
    try:
        document = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DatabaseRuntimeError(
            "stored database credential is unreadable"
        ) from error

    return valid_age_recipients(document)


def load_or_create_credentials(
    database_directory: Path,
    recipients: set[str],
    scratch_directory: Path,
    age_key_file: Path,
    sops_executable: Path,
    volume_exists: bool = False,
    runner=subprocess.run,
) -> str:
    """Return the password, creating or re-enveloping the credential.

    The password itself never rotates here; only its encryption envelope
    follows the application's current recipient set, so an escrow rollout
    reaches the database credential on the next deploy.
    """
    credentials_path = database_directory / CREDENTIALS_FILENAME

    if credentials_path.is_symlink():
        raise DatabaseRuntimeError("database credential cannot be a symbolic link")

    if not credentials_path.is_file() and volume_exists:
        # A fresh password would not reach a database that already exists --
        # POSTGRES_PASSWORD only applies while initialising an empty volume --
        # so the application would fail to authenticate against a deploy that
        # reported success. The operator resets it through the unix socket.
        raise DatabaseRuntimeError(
            "database volume exists but its credential is missing; "
            "restore the credential or reset the password on the running "
            "database before deploying"
        )

    if credentials_path.is_file():
        try:
            document = decrypt_sops_document(
                credentials_path,
                age_key_file=age_key_file,
                sops_executable=sops_executable,
                runner=runner,
            )
        except RuntimeSecretsError as error:
            raise DatabaseRuntimeError(str(error)) from error

        password = document.get("password")

        if not isinstance(password, str) or not password:
            raise DatabaseRuntimeError("stored database credential is invalid")

        if read_stored_recipients(credentials_path) != recipients:
            encrypt_credentials(
                password,
                recipients,
                credentials_path,
                scratch_directory,
                sops_executable,
                runner=runner,
            )

        return password

    password = generate_password()
    encrypt_credentials(
        password,
        recipients,
        credentials_path,
        scratch_directory,
        sops_executable,
        runner=runner,
    )

    return password


def resolve_postgres_image(
    postgres_major: int,
    existing_compose: Optional[dict[str, Any]],
    docker_executable: Path,
    runner=subprocess.run,
) -> str:
    """Pin the image on first use; keep the pin until the major changes.

    ``postgres:{major}`` is a mutable tag, which the platform otherwise
    forbids. It is resolved to a digest exactly once, so the running database
    changes only when the manifest's major does — a minor update becomes a
    deliberate act, not a side effect of a redeploy.
    """
    if postgres_major not in SUPPORTED_POSTGRES_MAJORS:
        raise DatabaseRuntimeError(f"unsupported PostgreSQL major: {postgres_major}")

    if existing_compose is not None:
        try:
            existing_image = existing_compose["services"][DATABASE_SERVICE]["image"]
        except (KeyError, TypeError):
            existing_image = None

        if isinstance(existing_image, str) and existing_image.startswith(
            f"postgres:{postgres_major}@sha256:"
        ):
            return existing_image

    tag = f"postgres:{postgres_major}"

    for action, command in (
        ("pull", [str(docker_executable), "pull", tag]),
        (
            "inspect",
            [
                str(docker_executable),
                "image",
                "inspect",
                tag,
                "--format",
                "{{index .RepoDigests 0}}",
            ],
        ),
    ):
        try:
            result = runner(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise DatabaseRuntimeError(
                f"PostgreSQL image {action} could not be executed"
            ) from error

        if result.returncode != 0:
            raise DatabaseRuntimeError(f"PostgreSQL image {action} failed")

    reference = result.stdout.decode("utf-8", errors="replace").strip()
    digest = reference.rpartition("@")[2]

    if not digest.startswith("sha256:") or len(digest) != 71:
        raise DatabaseRuntimeError("PostgreSQL image digest is unavailable")

    return f"{tag}@{digest}"


def postgres_data_mount(postgres_major: int) -> str:
    """Mount the volume where the image actually keeps the cluster.

    PostgreSQL 18 images moved PGDATA under /var/lib/postgresql/18/docker
    and expect the persistent mount one level up; mounting the pre-18 path
    there would leave the real cluster in image-managed storage, silently
    lost on the first container recreation.
    """
    if postgres_major >= 18:
        return "data:/var/lib/postgresql"

    return "data:/var/lib/postgresql/data"


def build_database_compose(
    project: str,
    environment: str,
    image: str,
    database_env_path: Path,
    postgres_major: int,
) -> dict[str, Any]:
    resource = database_resource_name(project, environment)

    return {
        "name": resource,
        "services": {
            DATABASE_SERVICE: {
                "image": image,
                "restart": "unless-stopped",
                "env_file": [str(database_env_path)],
                "shm_size": "128m",
                "networks": {
                    "db": {"aliases": [DATABASE_ALIAS]},
                },
                "volumes": [postgres_data_mount(postgres_major)],
                "healthcheck": {
                    "test": [
                        "CMD",
                        "pg_isready",
                        "--username",
                        DATABASE_USER,
                        "--dbname",
                        DATABASE_NAME,
                    ],
                    "interval": "5s",
                    "timeout": "5s",
                    "retries": 12,
                    "start_period": "30s",
                },
            },
        },
        "networks": {
            # internal: outbound NAT is connectivity the database never
            # needs, and the runbook promises its absence.
            "db": {"name": resource, "internal": True},
        },
        "volumes": {
            "data": {"name": resource},
        },
    }


def write_database_environment(
    runtime_directory: Path,
    password: str,
) -> Path:
    """Hand the container its bootstrap credential through tmpfs.

    POSTGRES_PASSWORD only matters the first time the volume is initialised;
    afterwards the password lives inside the database. The file is still kept
    current so a recreated container initialising a fresh volume agrees with
    the stored credential.
    """
    destination = runtime_directory / DATABASE_ENV_FILENAME

    write_private_file(
        destination,
        (
            f"POSTGRES_DB={DATABASE_NAME}\n"
            f"POSTGRES_USER={DATABASE_USER}\n"
            f"POSTGRES_PASSWORD={password}\n"
        ).encode("utf-8"),
    )

    return destination


def create_private_runtime_scope(
    runtime_secrets_root: Path,
    project: str,
    environment: str,
) -> Path:
    current = runtime_secrets_root

    for part in (project, environment):
        current = current / part

        if current.is_symlink():
            raise DatabaseRuntimeError("runtime secrets path is unsafe")

        current.mkdir(mode=0o700, parents=True, exist_ok=True)
        current.chmod(0o700)

    return current


def start_database(
    compose_path: Path,
    project: str,
    environment: str,
    docker_executable: Path,
    runner=subprocess.run,
) -> None:
    try:
        result = runner(
            [
                str(docker_executable),
                "compose",
                "--project-name",
                database_resource_name(project, environment),
                "--file",
                str(compose_path),
                "up",
                "--detach",
                "--wait",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DatabaseRuntimeError("database startup could not be executed") from error

    if result.returncode != 0:
        raise DatabaseRuntimeError("database is not healthy")


def ensure_project_database(
    manifest: dict[str, Any],
    project: str,
    environment: str,
    secrets_document: dict[str, Any],
    databases_root: Path,
    runtime_secrets_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    docker_executable: Path,
    runner=subprocess.run,
) -> Optional[str]:
    """Bring the declared database up and return its password.

    Returns ``None`` for ``mode: external`` — the platform then owns nothing.
    Runs before the release is recorded: an application whose database will
    not start has nothing worth writing into the ledger.
    """
    if declared_database_mode(manifest) != "docker":
        return None

    validate_runtime_identity(project, environment, "0" * 32)

    recipients = valid_age_recipients(secrets_document)

    if not recipients:
        raise DatabaseRuntimeError(
            "application secrets carry no age recipients for the " "database credential"
        )

    database_directory = create_private_database_directory(
        databases_root,
        project,
        environment,
    )
    # An existing volume is sacred: every mismatch around it is a stop for
    # the operator rather than an improvisation by the platform.
    volume_exists = database_volume_exists(
        project,
        environment,
        docker_executable,
        runner=runner,
    )
    runtime_directory = create_private_runtime_scope(
        runtime_secrets_root,
        project,
        environment,
    )

    password = load_or_create_credentials(
        database_directory,
        recipients,
        runtime_directory,
        age_key_file,
        sops_executable,
        volume_exists=volume_exists,
        runner=runner,
    )

    compose_path = database_directory / COMPOSE_FILENAME

    if compose_path.is_symlink():
        raise DatabaseRuntimeError("database compose cannot be a symbolic link")

    existing_compose = None

    if compose_path.is_file():
        try:
            existing_compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise DatabaseRuntimeError(
                "existing database compose is unreadable"
            ) from error

    requested_major = manifest["database"]["postgres_major"]
    running_major = existing_postgres_major(existing_compose)

    if volume_exists and running_major is not None and running_major != requested_major:
        # Data does not move between majors on its own, and PostgreSQL 18
        # changed where the cluster lives inside the volume, so the old one
        # would simply be ignored. The supported path is dump and restore.
        raise DatabaseRuntimeError(
            f"cannot change PostgreSQL {running_major} to {requested_major} "
            "on an existing volume; dump the database, remove the volume, "
            "and restore into the new major"
        )

    image = resolve_postgres_image(
        requested_major,
        existing_compose,
        docker_executable,
        runner=runner,
    )

    database_env_path = write_database_environment(
        runtime_directory,
        password,
    )
    compose_document = build_database_compose(
        project,
        environment,
        image,
        database_env_path,
        manifest["database"]["postgres_major"],
    )
    write_private_file(
        compose_path,
        yaml.safe_dump(
            compose_document,
            sort_keys=False,
            default_flow_style=False,
        ).encode("utf-8"),
    )

    start_database(
        compose_path,
        project,
        environment,
        docker_executable,
        runner=runner,
    )

    return password


def inject_database_url(
    runtime_secrets_path: Path,
    password: Optional[str],
) -> None:
    """Append DATABASE_URL to a freshly materialised env file.

    The variable is platform-owned: an application that ships its own
    DATABASE_URL in encrypted secrets is contradicting the manifest that
    asked the platform to run the database.
    """
    if password is None:
        return

    content = runtime_secrets_path.read_text(encoding="utf-8")

    if any(
        line.split("=", maxsplit=1)[0] == "DATABASE_URL"
        for line in content.splitlines()
    ):
        raise DatabaseRuntimeError(
            "application secrets must not define DATABASE_URL when the "
            "platform runs the database"
        )

    with runtime_secrets_path.open("a", encoding="utf-8") as file:
        file.write(f'DATABASE_URL="{database_url(password)}"\n')


def restore_database_environment(
    manifest: dict[str, Any],
    project: str,
    environment: str,
    runtime_secrets_path: Path,
    databases_root: Path,
    runtime_secrets_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    runner=subprocess.run,
) -> None:
    """Re-materialise database material after a reboot, without Docker.

    Boot recovery runs before the Docker daemon, so this only restores the
    tmpfs files: DATABASE_URL in the application env and the bootstrap env
    for the container. The container itself is restarted by Docker from its
    baked configuration.
    """
    if declared_database_mode(manifest) != "docker":
        return

    database_directory = databases_root / project / environment
    credentials_path = database_directory / CREDENTIALS_FILENAME

    if credentials_path.is_symlink() or not credentials_path.is_file():
        raise DatabaseRuntimeError(
            "database credential is missing for a docker-mode application"
        )

    try:
        document = decrypt_sops_document(
            credentials_path,
            age_key_file=age_key_file,
            sops_executable=sops_executable,
            runner=runner,
        )
    except RuntimeSecretsError as error:
        raise DatabaseRuntimeError(str(error)) from error

    password = document.get("password")

    if not isinstance(password, str) or not password:
        raise DatabaseRuntimeError("stored database credential is invalid")

    runtime_directory = create_private_runtime_scope(
        runtime_secrets_root,
        project,
        environment,
    )
    write_database_environment(runtime_directory, password)
    inject_database_url(runtime_secrets_path, password)
