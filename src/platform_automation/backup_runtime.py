#!/usr/bin/env python3

"""Produce encrypted, self-describing database dumps.

A dump is written by streaming `pg_dump` straight through age encryption to
its destination: the plaintext never reaches the disk. It is encrypted to the
same recipients as the application's own secrets, escrow included, so a backup
inherits the bus factor of everything else rather than growing its own.

The filename carries only a timestamp and a reason. Everything else lives in a
metadata card beside the dump *and* inside the encrypted stream, so a dump
found alone in a bucket describes itself once decrypted -- which it must be to
be used at all. The application version deliberately stays out of the
filename: object keys are readable by anyone who can list a bucket.
"""

import json
import os
import shutil
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .database_runtime import (
    DATABASE_NAME,
    DATABASE_SERVICE,
    DATABASE_USER,
    DatabaseRuntimeError,
    database_container_name,
    stored_database_password,
)

DEFAULT_BACKUPS_ROOT = Path("/var/backups/platform")
DEFAULT_AGE_EXECUTABLE = Path("/usr/local/bin/age")
DEFAULT_DOCKER_EXECUTABLE = Path("/usr/bin/docker")

DEFAULT_RETAIN = 14
DEFAULT_INTERVAL_MINUTES = 360

BACKUP_REASONS = ("operator", "schedule", "pre-migration")
DUMP_SUFFIX = ".dump.age"
CARD_SUFFIX = ".json"
CARD_MEMBER = "platform-backup.json"

# The card travels inside the encrypted stream as well as beside it, so a
# dump found alone still describes itself once decrypted. The envelope is a
# single readable line followed by the card and then the untouched pg_dump
# bytes: streamable in constant memory, and legible to a human holding
# nothing but `age`. Tar cannot do this -- it needs a member's size before
# its data, and a dump's size is not known until it has finished.
ENVELOPE_MAGIC = b"PLATFORM-BACKUP/1"
MAX_ENVELOPE_CARD_BYTES = 64 * 1024

STAMP_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-(?:[a-z-]+)$")


class BackupRuntimeError(RuntimeError):
    pass


def backup_stamp(reason: str, moment: datetime = None) -> str:
    if reason not in BACKUP_REASONS:
        raise BackupRuntimeError(f"unknown backup reason: {reason}")

    if moment is None:
        moment = datetime.now(timezone.utc)

    return f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{reason}"


def stamp_taken_at(stamp: str) -> Optional[datetime]:
    """Recover when a dump was taken from its own name."""
    if not STAMP_PATTERN.fullmatch(stamp):
        return None

    try:
        moment = datetime.strptime(stamp.split("-", 1)[0], "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None

    return moment.replace(tzinfo=timezone.utc)


def loss_window(
    latest_stamp: Optional[str],
    interval_minutes: Optional[int],
    now: datetime = None,
) -> dict[str, Any]:
    """Say how much data an outage right now would cost.

    An operator reading a timestamp has to do this arithmetic themselves, at
    the worst possible moment. The schedule bounds the answer; the age of the
    newest dump is the answer. When the age exceeds the interval by a clear
    margin the schedule has stopped without anyone noticing, and saying so is
    the whole point of reporting it.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    taken = stamp_taken_at(latest_stamp) if latest_stamp else None

    if taken is None:
        return {
            "interval_minutes": interval_minutes,
            "newest_age_minutes": None,
            "overdue": interval_minutes is not None,
        }

    age = max(int((now - taken).total_seconds() // 60), 0)

    # One whole interval of slack: a timer with a randomised delay is
    # expected to run late, and crying wolf is how a signal gets ignored.
    overdue = interval_minutes is not None and age > interval_minutes * 2

    return {
        "interval_minutes": interval_minutes,
        "newest_age_minutes": age,
        "overdue": overdue,
    }


def create_private_backup_directory(
    backups_root: Path,
    project: str,
    environment: str,
) -> Path:
    if backups_root.is_symlink():
        raise BackupRuntimeError("backups root cannot be a symbolic link")

    backups_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = backups_root.resolve()

    for part in (project, environment):
        current = current / part

        if current.is_symlink():
            raise BackupRuntimeError("backup path cannot contain a symbolic link")

        current.mkdir(mode=0o700, exist_ok=True)
        current.chmod(0o700)

    return current


def build_metadata_card(
    project: str,
    environment: str,
    stamp: str,
    reason: str,
    record: dict[str, Any],
    postgres_major: int,
) -> dict[str, Any]:
    """Describe the dump by release_id, not by a tag.

    A release tag is a human label and may be anything at all; release_id is
    always present and unique, so it is what a restore compares against.
    """
    return {
        "api_version": "platform-backup/v1",
        "project": project,
        "environment": environment,
        "stamp": stamp,
        "reason": reason,
        "created_at": (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
        "postgres_major": postgres_major,
        "release_id": record["release_id"],
        "release_tag": record["release_tag"],
        "image": record["image"]["reference"],
        "bundle_digest": record["bundle"]["digest"],
    }


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


def dump_command(
    container: str,
    password: str,
    docker_executable: Path,
) -> list[str]:
    return [
        str(docker_executable),
        "exec",
        "--env",
        f"PGPASSWORD={password}",
        container,
        "pg_dump",
        "--username",
        DATABASE_USER,
        "--dbname",
        DATABASE_NAME,
        "--format",
        "custom",
        "--no-owner",
        "--no-privileges",
    ]


def age_command(
    recipients: set[str],
    age_executable: Path,
) -> list[str]:
    if not recipients:
        raise BackupRuntimeError("a backup needs at least one age recipient")

    command = [str(age_executable), "--encrypt"]

    for recipient in sorted(recipients):
        command.extend(["--recipient", recipient])

    return command


def card_path(dump_path: Path) -> Path:
    """Name the sidecar from the dump it belongs to.

    Derived rather than substituted: a name that does not end in the dump
    suffix would otherwise produce the dump's own path, and the card would
    overwrite the thing it describes.
    """
    if not dump_path.name.endswith(DUMP_SUFFIX):
        raise BackupRuntimeError(f"not a dump path: {dump_path.name}")

    return dump_path.with_name(dump_path.name[: -len(DUMP_SUFFIX)] + CARD_SUFFIX)


def render_envelope_header(card: dict[str, Any]) -> bytes:
    """One readable line, then the card, then the dump."""
    body = json.dumps(card, sort_keys=True).encode("utf-8")

    if len(body) > MAX_ENVELOPE_CARD_BYTES:
        raise BackupRuntimeError("backup metadata card exceeds the size limit")

    return ENVELOPE_MAGIC + b" " + str(len(body)).encode("ascii") + b"\n" + body


def split_envelope(path: Path) -> tuple[Optional[dict[str, Any]], int]:
    """Return the card a dump carries and the offset where its bytes begin.

    An offset rather than a positioned file object, deliberately. A buffered
    reader's `seek` restores the Python-level position but leaves the file
    descriptor wherever read-ahead put it, and a subprocess handed that
    descriptor inherits the descriptor, not the buffer -- it would be given an
    empty stream. Callers seek the raw descriptor to this offset instead.

    Dumps written before the envelope existed begin with pg_dump's own magic
    and report offset zero: a backup that stopped restoring because the format
    improved would not be an improvement.
    """
    with path.open("rb") as stream:
        head = stream.read(len(ENVELOPE_MAGIC) + 32)

    if not head.startswith(ENVELOPE_MAGIC):
        return None, 0

    newline = head.find(b"\n")

    if newline < 0:
        raise BackupRuntimeError("backup envelope header is malformed")

    try:
        length = int(head[len(ENVELOPE_MAGIC) : newline])
    except ValueError as error:
        raise BackupRuntimeError("backup envelope header is malformed") from error

    if length < 0 or length > MAX_ENVELOPE_CARD_BYTES:
        raise BackupRuntimeError("backup envelope card exceeds the size limit")

    with path.open("rb") as stream:
        stream.seek(newline + 1)
        body = stream.read(length)

    if len(body) != length:
        raise BackupRuntimeError("backup envelope is truncated")

    try:
        card = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BackupRuntimeError("backup envelope card is not valid JSON") from error

    return (card if isinstance(card, dict) else None), newline + 1 + length


def stream_encrypted_dump(
    destination: Path,
    container: str,
    password: str,
    recipients: set[str],
    card: dict[str, Any],
    docker_executable: Path,
    age_executable: Path,
    popen=subprocess.Popen,
) -> int:
    """Pipe the envelope and pg_dump through age into a private file.

    Nothing is written until both processes succeed, and the plaintext exists
    only in the pipe between them -- the copy runs in bounded chunks rather
    than buffering a dump that may be far larger than memory.
    """
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_name)
    os.fchmod(descriptor, 0o600)

    try:
        with os.fdopen(descriptor, "wb") as output:
            dump = popen(
                dump_command(container, password, docker_executable),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            encrypt = popen(
                age_command(recipients, age_executable),
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.PIPE,
            )

            try:
                encrypt.stdin.write(render_envelope_header(card))
                shutil.copyfileobj(dump.stdout, encrypt.stdin)
            finally:
                # Let pg_dump see a closed pipe if age dies first.
                encrypt.stdin.close()
                dump.stdout.close()

            # Not communicate(): it flushes stdin, which is already closed.
            # Draining stderr blocks until each process exits, which is both
            # the wait and the guard against a full pipe.
            encrypt.stderr.read()
            dump.stderr.read()
            encrypt.wait(timeout=3600)
            dump.wait(timeout=60)

        if dump.returncode != 0:
            raise BackupRuntimeError("database dump failed")

        if encrypt.returncode != 0:
            raise BackupRuntimeError("dump encryption failed")

        written = temporary_path.stat().st_size

        if written == 0:
            raise BackupRuntimeError("database dump produced no output")

        os.replace(temporary_path, destination)
        destination.chmod(0o600)
    except (OSError, subprocess.TimeoutExpired) as error:
        temporary_path.unlink(missing_ok=True)
        raise BackupRuntimeError("database dump could not be executed") from error
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    write_private_file(
        card_path(destination),
        json.dumps(card, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )

    return written


def list_backups(directory: Path) -> list[str]:
    """Return dump stamps, oldest first. The stamp sorts chronologically."""
    if directory.is_symlink() or not directory.is_dir():
        return []

    stamps = []

    for path in directory.iterdir():
        if path.is_symlink() or not path.is_file():
            continue

        if not path.name.endswith(DUMP_SUFFIX):
            continue

        stamp = path.name[: -len(DUMP_SUFFIX)]

        if STAMP_PATTERN.fullmatch(stamp):
            stamps.append(stamp)

    return sorted(stamps)


def prune_backups(
    directory: Path,
    retain: int,
    warnings: list[str],
) -> list[str]:
    """Drop the oldest dumps beyond the retained count.

    Never removes the newest, and only ever warns: housekeeping does not get
    to fail a backup that already succeeded.
    """
    stamps = list_backups(directory)
    removed: list[str] = []

    for stamp in stamps[: max(len(stamps) - retain, 0)]:
        try:
            (directory / f"{stamp}{DUMP_SUFFIX}").unlink()
            (directory / f"{stamp}{CARD_SUFFIX}").unlink(missing_ok=True)
        except OSError as error:
            warnings.append(f"backup {stamp} not removed: {error}")
            continue

        removed.append(stamp)

    return removed


def resolve_retention(manifest: dict[str, Any]) -> int:
    backup = manifest["database"].get("backup") or {}
    retain = backup.get("retain", DEFAULT_RETAIN)

    if isinstance(retain, bool) or not isinstance(retain, int) or retain < 1:
        raise BackupRuntimeError("backup retain must be a positive integer")

    return retain


def create_backup(
    manifest: dict[str, Any],
    project: str,
    environment: str,
    record: dict[str, Any],
    secrets_document: dict[str, Any],
    reason: str,
    databases_root: Path,
    backups_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    age_executable: Path,
    docker_executable: Path,
    runner=subprocess.run,
    popen=subprocess.Popen,
    moment: datetime = None,
) -> dict[str, Any]:
    """Write one encrypted dump for a platform-owned database."""
    from .database_runtime import declared_database_mode
    from .sops_validation import valid_age_recipients

    if declared_database_mode(manifest) != "docker":
        raise BackupRuntimeError(
            "backups require a platform-owned database (mode: docker)"
        )

    recipients = valid_age_recipients(secrets_document)

    if not recipients:
        raise BackupRuntimeError(
            "application secrets carry no age recipients for the backup"
        )

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
        raise BackupRuntimeError(str(error)) from error

    stamp = backup_stamp(reason, moment)
    directory = create_private_backup_directory(backups_root, project, environment)
    destination = directory / f"{stamp}{DUMP_SUFFIX}"

    if destination.exists():
        raise BackupRuntimeError(f"backup already exists: {stamp}")

    card = build_metadata_card(
        project,
        environment,
        stamp,
        reason,
        record,
        manifest["database"]["postgres_major"],
    )
    written = stream_encrypted_dump(
        destination,
        database_container_name(project, environment),
        password,
        recipients,
        card,
        docker_executable,
        age_executable,
        popen=popen,
    )

    warnings: list[str] = []
    removed = prune_backups(directory, resolve_retention(manifest), warnings)

    return {
        "stamp": stamp,
        "reason": reason,
        "path": str(destination),
        "bytes": written,
        "recipients": len(recipients),
        "release_id": record["release_id"],
        "removed_backups": removed,
        "warnings": warnings,
    }
