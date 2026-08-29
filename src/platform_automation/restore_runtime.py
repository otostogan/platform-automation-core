#!/usr/bin/env python3

"""Read backups back, and prove that reading them back works.

Two operations live here and they are deliberately different in temperament.

`restore` is destructive and touches the live database, so it refuses unless
an operator says so explicitly, and refuses outright while a deployment holds
the lock.

`verify` touches nothing. It restores the newest dump into a throwaway
container with no network, runs the query the application declared in
`restore_validation`, and tears the container down whether or not any of that
worked. Its whole purpose is to turn "we have backups" into a dated fact.
"""

import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .backup_runtime import (
    CARD_SUFFIX,
    DUMP_SUFFIX,
    BackupRuntimeError,
    list_backups,
    write_private_file,
)
from .database_runtime import (
    DATABASE_NAME,
    DATABASE_USER,
    DatabaseRuntimeError,
    database_container_name,
    stored_database_password,
)

DEFAULT_AGE_EXECUTABLE = Path("/usr/local/bin/age")
DEFAULT_DOCKER_EXECUTABLE = Path("/usr/bin/docker")

VERIFICATION_LOG = "verifications.json"
MAX_VERIFICATION_ENTRIES = 50
VERIFY_READY_TIMEOUT_SECONDS = 120

STAMP_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-z-]+$")


class RestoreRuntimeError(RuntimeError):
    pass


def utc_now() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def environment_directory(
    backups_root: Path,
    project: str,
    environment: str,
) -> Path:
    directory = backups_root / project / environment

    if directory.is_symlink() or not directory.is_dir():
        raise RestoreRuntimeError(f"no backups exist for {project}/{environment}")

    return directory


def select_backup(directory: Path, stamp: Optional[str]) -> str:
    """Name a dump explicitly, or take the newest."""
    stamps = list_backups(directory)

    if not stamps:
        raise RestoreRuntimeError("no backups exist to restore from")

    if stamp is None:
        return stamps[-1]

    if not STAMP_PATTERN.fullmatch(stamp):
        raise RestoreRuntimeError(f"invalid backup stamp: {stamp}")

    if stamp not in stamps:
        raise RestoreRuntimeError(f"backup does not exist: {stamp}")

    return stamp


def read_metadata_card(directory: Path, stamp: str) -> dict[str, Any]:
    path = directory / f"{stamp}{CARD_SUFFIX}"

    if path.is_symlink() or not path.is_file():
        raise RestoreRuntimeError(f"backup has no metadata card: {stamp}")

    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RestoreRuntimeError(f"metadata card is unreadable: {stamp}") from error

    if not isinstance(card, dict):
        raise RestoreRuntimeError(f"metadata card is not an object: {stamp}")

    return card


def describe_revision_gap(
    card: dict[str, Any],
    current_record: Optional[dict[str, Any]],
) -> Optional[str]:
    """Warn when a dump predates the running release.

    The schema lives inside the dump. Restoring an older one under a newer
    application breaks on the first query for a column the dump does not have.
    This warns rather than refuses: restoring across a revision is sometimes
    exactly what an operator means to do.
    """
    if current_record is None:
        return None

    if card.get("release_id") == current_record["release_id"]:
        return None

    return (
        f"this dump was taken on release {card.get('release_tag')} "
        f"({str(card.get('release_id'))[:8]}); "
        f"{current_record['release_tag']} "
        f"({current_record['release_id'][:8]}) is deployed now"
    )


def decrypt_dump(
    directory: Path,
    stamp: str,
    destination: Path,
    age_key_file: Path,
    age_executable: Path,
    runner=subprocess.run,
) -> None:
    source = directory / f"{stamp}{DUMP_SUFFIX}"

    if source.is_symlink() or not source.is_file():
        raise RestoreRuntimeError(f"backup does not exist: {stamp}")

    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )

    try:
        with os.fdopen(descriptor, "wb") as output:
            result = runner(
                [
                    str(age_executable),
                    "--decrypt",
                    "--identity",
                    str(age_key_file),
                    str(source),
                ],
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
                timeout=1800,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        destination.unlink(missing_ok=True)
        raise RestoreRuntimeError("backup decryption could not be executed") from error

    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        raise RestoreRuntimeError("backup decryption failed")

    if destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise RestoreRuntimeError("decrypted backup is empty")


def run_pg_restore(
    container: str,
    password: str,
    dump_path: Path,
    docker_executable: Path,
    runner=subprocess.run,
) -> None:
    """Replace the contents of a database from a custom-format dump."""
    try:
        with dump_path.open("rb") as dump:
            result = runner(
                [
                    str(docker_executable),
                    "exec",
                    "--interactive",
                    "--env",
                    f"PGPASSWORD={password}",
                    container,
                    "pg_restore",
                    "--username",
                    DATABASE_USER,
                    "--dbname",
                    DATABASE_NAME,
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                ],
                stdin=dump,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=3600,
            )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RestoreRuntimeError("database restore could not be executed") from error

    if result.returncode != 0:
        raise RestoreRuntimeError("database restore failed")


def restore_backup(
    project: str,
    environment: str,
    stamp: Optional[str],
    current_record: Optional[dict[str, Any]],
    databases_root: Path,
    backups_root: Path,
    runtime_secrets_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    age_executable: Path,
    docker_executable: Path,
    runner=subprocess.run,
) -> dict[str, Any]:
    """Restore a dump over the live database. Destructive by definition."""
    directory = environment_directory(backups_root, project, environment)
    selected = select_backup(directory, stamp)
    card = read_metadata_card(directory, selected)

    try:
        password = stored_database_password(
            databases_root,
            project,
            environment,
            age_key_file,
            sops_executable,
            runner=runner,
        )
    except DatabaseRuntimeError as error:
        raise RestoreRuntimeError(str(error)) from error

    scratch = runtime_secrets_root / project / environment
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".restore-", dir=scratch) as name:
        dump_path = Path(name) / "restore.dump"
        decrypt_dump(
            directory,
            selected,
            dump_path,
            age_key_file,
            age_executable,
            runner=runner,
        )
        run_pg_restore(
            database_container_name(project, environment),
            password,
            dump_path,
            docker_executable,
            runner=runner,
        )

    return {
        "operation": "restore",
        "stamp": selected,
        "release_id": card.get("release_id"),
        "release_tag": card.get("release_tag"),
        "restored_at": utc_now(),
        "revision_gap": describe_revision_gap(card, current_record),
    }


def wait_until_ready(
    container: str,
    docker_executable: Path,
    runner=subprocess.run,
    sleeper=time.sleep,
    timeout: int = VERIFY_READY_TIMEOUT_SECONDS,
) -> None:
    deadline = timeout

    while deadline > 0:
        try:
            result = runner(
                [
                    str(docker_executable),
                    "exec",
                    container,
                    "pg_isready",
                    "--username",
                    DATABASE_USER,
                    "--dbname",
                    DATABASE_NAME,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RestoreRuntimeError(
                "verification container could not be inspected"
            ) from error

        if result.returncode == 0:
            return

        sleeper(2)
        deadline -= 2

    raise RestoreRuntimeError("verification container never became ready")


def start_verification_container(
    name: str,
    image: str,
    password: str,
    docker_executable: Path,
    runner=subprocess.run,
) -> None:
    """Start a disposable database with no network and no volume.

    It briefly holds a full plaintext copy of production data, so it gets no
    network at all and everything it writes dies with the container.
    """
    try:
        result = runner(
            [
                str(docker_executable),
                "run",
                "--detach",
                "--rm",
                "--name",
                name,
                "--network",
                "none",
                "--env",
                f"POSTGRES_PASSWORD={password}",
                "--env",
                f"POSTGRES_USER={DATABASE_USER}",
                "--env",
                f"POSTGRES_DB={DATABASE_NAME}",
                image,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RestoreRuntimeError(
            "verification container could not be started"
        ) from error

    if result.returncode != 0:
        raise RestoreRuntimeError("verification container failed to start")


def remove_container(
    name: str,
    docker_executable: Path,
    runner=subprocess.run,
) -> None:
    try:
        runner(
            [str(docker_executable), "rm", "--force", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Teardown runs in a finally block; a failure to remove must not mask
        # the outcome the operator actually asked about.
        pass


def run_validation_query(
    container: str,
    password: str,
    query: str,
    docker_executable: Path,
    runner=subprocess.run,
) -> str:
    try:
        result = runner(
            [
                str(docker_executable),
                "exec",
                "--env",
                f"PGPASSWORD={password}",
                container,
                "psql",
                "--username",
                DATABASE_USER,
                "--dbname",
                DATABASE_NAME,
                "--tuples-only",
                "--no-align",
                "--command",
                query,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RestoreRuntimeError(
            "restore validation query could not be executed"
        ) from error

    if result.returncode != 0:
        raise RestoreRuntimeError("restore validation query failed")

    return result.stdout.decode("utf-8", errors="replace").strip()


def record_verification(directory: Path, entry: dict[str, Any]) -> None:
    """Keep a short history so status can answer when a restore was proven."""
    path = directory / VERIFICATION_LOG
    entries: list[dict[str, Any]] = []

    if path.is_file() and not path.is_symlink():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                entries = [item for item in loaded if isinstance(item, dict)]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            entries = []

    entries.append(entry)
    entries = entries[-MAX_VERIFICATION_ENTRIES:]

    write_private_file(
        path,
        json.dumps(entries, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )


def last_verification(directory: Path) -> Optional[dict[str, Any]]:
    path = directory / VERIFICATION_LOG

    if path.is_symlink() or not path.is_file():
        return None

    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(entries, list) or not entries:
        return None

    return entries[-1] if isinstance(entries[-1], dict) else None


def verify_backup(
    manifest: dict[str, Any],
    project: str,
    environment: str,
    stamp: Optional[str],
    databases_root: Path,
    backups_root: Path,
    runtime_secrets_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    age_executable: Path,
    docker_executable: Path,
    runner=subprocess.run,
    sleeper=time.sleep,
) -> dict[str, Any]:
    """Restore a dump into a throwaway container and query it.

    The live database is never touched. A failure here is the point of the
    exercise, so it is recorded as loudly as a success.
    """
    directory = environment_directory(backups_root, project, environment)
    selected = select_backup(directory, stamp)
    card = read_metadata_card(directory, selected)
    query = manifest["restore_validation"]["query"]
    image = (
        f"postgres:{card.get('postgres_major', manifest['database']['postgres_major'])}"
    )

    password = secrets.token_urlsafe(24)
    container = f"platform-verify-{project}-{environment}-{secrets.token_hex(4)}"
    started_at = utc_now()

    entry: dict[str, Any] = {
        "stamp": selected,
        "started_at": started_at,
        "outcome": "failed",
        "error": None,
        "result": None,
        "release_id": card.get("release_id"),
    }

    scratch = runtime_secrets_root / project / environment
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix=".verify-", dir=scratch) as name:
            dump_path = Path(name) / "verify.dump"
            decrypt_dump(
                directory,
                selected,
                dump_path,
                age_key_file,
                age_executable,
                runner=runner,
            )

            try:
                start_verification_container(
                    container,
                    image,
                    password,
                    docker_executable,
                    runner=runner,
                )
                wait_until_ready(
                    container,
                    docker_executable,
                    runner=runner,
                    sleeper=sleeper,
                )
                run_pg_restore(
                    container,
                    password,
                    dump_path,
                    docker_executable,
                    runner=runner,
                )
                entry["result"] = run_validation_query(
                    container,
                    password,
                    query,
                    docker_executable,
                    runner=runner,
                )
                entry["outcome"] = "succeeded"
            finally:
                remove_container(container, docker_executable, runner=runner)
    except (RestoreRuntimeError, BackupRuntimeError, OSError) as error:
        entry["error"] = str(error)[:2048]
        entry["completed_at"] = utc_now()
        record_verification(directory, entry)
        raise RestoreRuntimeError(str(error)) from error

    entry["completed_at"] = utc_now()
    record_verification(directory, entry)

    return {
        "operation": "verify-backup",
        "stamp": selected,
        "outcome": entry["outcome"],
        "result": entry["result"],
        "query": query,
        "release_id": card.get("release_id"),
        "verified_at": entry["completed_at"],
    }
