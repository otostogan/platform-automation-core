#!/usr/bin/env python3

"""Run one declared export: a query, a CSV, an e-mail.

The query runs inside the database container as the application's own
role, in a read-only transaction with a time limit, so a slip in a weekly
report can read anything the application can and change nothing. The CSV
is held in memory, bounded, and leaves the host only as an attachment over
TLS; nothing of it is written to disk. What is written is a small card per
job saying when it last ran and how it ended, which the metrics export turns
into an alert when it failed.
"""

import csv
import gzip
import io
import json
import os
import smtplib
import ssl
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any, Optional

from .database_runtime import (
    DATABASE_NAME,
    DATABASE_USER,
    DatabaseRuntimeError,
    database_container_name,
    stored_database_password,
)
from .htpasswd import read_env_file

DEFAULT_EXPORTS_ROOT = Path("/var/lib/platform/exports")

CARD_API_VERSION = "platform-export/v1"
# Queries that take longer than this are reports, not exports.
STATEMENT_TIMEOUT_SECONDS = 600
# Held in memory and sent as one attachment; relays commonly stop at 25 MB.
MAX_CSV_BYTES = 64 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
DEFAULT_SMTP_PORT = 587
IMPLICIT_TLS_PORT = 465
SMTP_TIMEOUT_SECONDS = 60

OUTCOME_SENT = "sent"
OUTCOME_FAILED = "failed"


class ExportRuntimeError(RuntimeError):
    pass


def bare_query(query: str) -> str:
    """The query as it may sit inside ``COPY (...)``: no trailing semicolon."""
    text = query.strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    if not text:
        raise ExportRuntimeError("the export query is empty")
    return text


def render_script(query: str) -> str:
    """What psql runs: a bounded, read-only transaction around the COPY.

    ``COPY ... TO STDOUT`` is server-side SQL that psql streams to its own
    stdout, so the query may span lines, unlike the ``\\copy`` meta-command.
    READ ONLY is what keeps a query that breaks out of the parentheses from
    changing anything: a write there fails instead of running.
    """
    return (
        f"SET statement_timeout TO {STATEMENT_TIMEOUT_SECONDS * 1000};\n"
        "START TRANSACTION READ ONLY;\n"
        f"COPY (\n{bare_query(query)}\n) TO STDOUT WITH (FORMAT csv, HEADER);\n"
        "COMMIT;\n"
    )


def run_query(
    project: str,
    environment: str,
    app_password: str,
    query: str,
    docker_executable: Path,
    popen=subprocess.Popen,
) -> bytes:
    """The CSV bytes psql wrote; the password travels in the environment, never argv.

    The output is read as it arrives and the query is killed the moment it
    passes the cap, so a broad SELECT costs the host at most the cap in
    memory rather than whatever the table holds.
    """
    command = [
        str(docker_executable),
        "exec",
        "--interactive",
        "--env",
        f"PGPASSWORD={app_password}",
        database_container_name(project, environment),
        "psql",
        "--username",
        DATABASE_USER,
        "--dbname",
        DATABASE_NAME,
        "--quiet",
        "--no-psqlrc",
        "--set",
        "ON_ERROR_STOP=1",
    ]
    try:
        process = popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ExportRuntimeError("docker could not be executed") from error
    deadline = time.monotonic() + STATEMENT_TIMEOUT_SECONDS + 60
    chunks = []
    total = 0
    try:
        # The script is a few kilobytes at most: it fits the pipe, so it is
        # written whole before psql is asked for anything.
        try:
            process.stdin.write(render_script(query).encode("utf-8"))
            process.stdin.close()
        except BrokenPipeError:
            pass
        while True:
            chunk = process.stdout.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CSV_BYTES:
                raise ExportRuntimeError(
                    f"the export is larger than {MAX_CSV_BYTES // (1024 * 1024)} MB;"
                    " narrow the query"
                )
            chunks.append(chunk)
            if time.monotonic() > deadline:
                raise ExportRuntimeError("the export query did not finish in time")
        try:
            process.wait(timeout=max(deadline - time.monotonic(), 1))
        except subprocess.TimeoutExpired as error:
            raise ExportRuntimeError(
                "the export query did not finish in time"
            ) from error
    except ExportRuntimeError:
        _kill(process)
        raise
    if process.returncode != 0:
        detail = _text(process.stderr.read()).strip().splitlines()
        reason = detail[-1] if detail else "psql failed"
        raise ExportRuntimeError(f"the export query failed: {reason}")
    return b"".join(chunks)


def _kill(process) -> None:
    try:
        process.kill()
        process.wait(timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass


def count_rows(data: bytes) -> int:
    """Rows below the header, counted as CSV so a newline inside a value is not one."""
    csv.field_size_limit(MAX_CSV_BYTES)
    reader = csv.reader(io.StringIO(data.decode("utf-8", errors="replace")))
    try:
        return max(sum(1 for _ in reader) - 1, 0)
    except csv.Error as error:
        raise ExportRuntimeError(
            f"the export is not readable as CSV: {error}"
        ) from error


def attachment_name(name: str, moment: datetime, compress: bool) -> str:
    stamp = moment.strftime("%Y%m%d")
    return f"{name}-{stamp}.csv" + (".gz" if compress else "")


def build_message(
    job: dict[str, Any],
    smtp: dict[str, Any],
    project: str,
    environment: str,
    data: bytes,
    rows: int,
    moment: datetime,
) -> EmailMessage:
    compress = bool(job.get("compress"))
    payload = gzip.compress(data, mtime=0) if compress else data
    if len(payload) > MAX_ATTACHMENT_BYTES:
        raise ExportRuntimeError(
            f"the attachment is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB;"
            " narrow the query or set compress: true"
        )
    filename = attachment_name(job["name"], moment, compress)
    message = EmailMessage()
    message["From"] = smtp["from"]
    message["To"] = ", ".join(job["to"])
    message["Subject"] = job.get("subject") or (
        f"{project}/{environment}: {job['name']} export"
    )
    message["Date"] = formatdate(moment.timestamp(), usegmt=True)
    message["Message-ID"] = make_msgid(domain=smtp["host"])
    message.set_content(
        f"Export {job['name']} of {project}/{environment}.\n"
        f"Rows: {rows}\n"
        f"Taken at: {moment.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Attachment: {filename}\n"
    )
    if compress:
        message.add_attachment(
            payload, maintype="application", subtype="gzip", filename=filename
        )
    else:
        message.add_attachment(
            payload, maintype="text", subtype="csv", filename=filename
        )
    return message


def send_message(
    message: EmailMessage, smtp: dict[str, Any], password: str, client=None
) -> None:
    """Over TLS, always: implicit on 465, STARTTLS elsewhere, never plain."""
    host = smtp["host"]
    port = int(smtp.get("port") or DEFAULT_SMTP_PORT)
    username = smtp.get("username") or smtp["from"]
    context = ssl.create_default_context()
    try:
        if port == IMPLICIT_TLS_PORT:
            connection = (client or smtplib.SMTP_SSL)(
                host, port, timeout=SMTP_TIMEOUT_SECONDS, context=context
            )
        else:
            connection = (client or smtplib.SMTP)(
                host, port, timeout=SMTP_TIMEOUT_SECONDS
            )
        with connection:
            if port != IMPLICIT_TLS_PORT:
                connection.ehlo()
                connection.starttls(context=context)
                connection.ehlo()
            connection.login(username, password)
            refused = connection.send_message(message) or {}
        if refused:
            # A relay that takes some recipients and refuses others answers
            # with a dictionary, not an exception; a report half delivered
            # is a failed run, named by who did not get it.
            raise ExportRuntimeError(
                f"the SMTP server {host} refused recipient(s): "
                + ", ".join(sorted(refused))
            )
    except smtplib.SMTPAuthenticationError as error:
        raise ExportRuntimeError(
            f"the SMTP server {host} refused the login for {username}"
        ) from error
    except (smtplib.SMTPException, OSError, ssl.SSLError) as error:
        raise ExportRuntimeError(
            f"sending through {host}:{port} failed: {error}"
        ) from error


def smtp_password(runtime_secrets_path: Optional[Path], variable: str) -> str:
    if runtime_secrets_path is None or not Path(runtime_secrets_path).is_file():
        raise ExportRuntimeError(
            "the release's environment secrets are not on this host"
        )
    values = read_env_file(Path(runtime_secrets_path))
    password = values.get(variable)
    if not password:
        raise ExportRuntimeError(
            f"SMTP password variable {variable} is not in the environment secrets"
            " of this release, or is empty"
        )
    return password


def card_path(exports_root: Path, project: str, environment: str, name: str) -> Path:
    return exports_root / project / environment / f"{name}.json"


def write_card(exports_root: Path, card: dict[str, Any]) -> Path:
    """The last run of a job, replacing the one before: a status, not a log."""
    destination = card_path(
        exports_root, card["project"], card["environment"], card["name"]
    )
    directory = destination.parent
    for path in (exports_root, exports_root / card["project"], directory):
        if path.is_symlink():
            raise ExportRuntimeError("exports path is unsafe")
        path.mkdir(mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".card.", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(card, file, indent=2, sort_keys=True)
            file.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return destination


def read_card(
    exports_root: Path, project: str, environment: str, name: str
) -> Optional[dict[str, Any]]:
    path = card_path(exports_root, project, environment, name)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return card if isinstance(card, dict) else None


def list_cards(exports_root: Path, project: str, environment: str) -> list:
    directory = exports_root / project / environment
    try:
        names = sorted(p.stem for p in directory.iterdir() if p.suffix == ".json")
    except OSError:
        return []
    cards = []
    for name in names:
        card = read_card(exports_root, project, environment, name)
        if card is not None:
            cards.append(card)
    return cards


def run_export(
    manifest: dict[str, Any],
    project: str,
    environment: str,
    name: str,
    record: dict[str, Any],
    runtime_secrets_path: Optional[Path],
    databases_root: Path,
    exports_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    docker_executable: Path,
    runner=subprocess.run,
    mailer=send_message,
    now: Optional[datetime] = None,
    popen=subprocess.Popen,
) -> dict[str, Any]:
    """Query, attach, send; and leave a card that says how it went, either way."""
    from .export_schedule import ExportScheduleError, find_job

    moment = now or datetime.now(timezone.utc)
    card = {
        "api_version": CARD_API_VERSION,
        "project": project,
        "environment": environment,
        "name": name,
        "started_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "release_tag": record.get("release_tag"),
        "release_id": record.get("release_id"),
        "outcome": OUTCOME_FAILED,
        "error": None,
        "rows": None,
        "bytes": None,
        "attachment": None,
        "to": [],
    }
    try:
        if manifest["database"]["mode"] != "docker":
            raise ExportRuntimeError(
                "exports require a platform-owned database (mode: docker)"
            )
        try:
            job = find_job(manifest, name)
        except ExportScheduleError as error:
            raise ExportRuntimeError(str(error)) from error
        smtp = manifest["exports"]["smtp"]
        card["to"] = list(job["to"])
        card["schedule"] = job.get("schedule")
        password = smtp_password(runtime_secrets_path, smtp["password_env"])
        try:
            app_password = stored_database_password(
                databases_root,
                project,
                environment,
                age_key_file,
                sops_executable,
                runner=runner,
            )
        except DatabaseRuntimeError as error:
            raise ExportRuntimeError(str(error)) from error
        data = run_query(
            project, environment, app_password, job["query"], docker_executable, popen
        )
        rows = count_rows(data)
        message = build_message(job, smtp, project, environment, data, rows, moment)
        mailer(message, smtp, password)
        card.update(
            {
                "outcome": OUTCOME_SENT,
                "rows": rows,
                "bytes": len(data),
                "attachment": message.get_payload()[1].get_filename(),
                "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        )
    except ExportRuntimeError as error:
        card["error"] = str(error)
        write_card(exports_root, card)
        raise
    except Exception as error:
        # Whatever else went wrong, the card must not keep saying "sent":
        # the alert reads the card, not the journal.
        card["error"] = f"unexpected: {error}"
        write_card(exports_root, card)
        raise ExportRuntimeError(card["error"]) from error
    write_card(exports_root, card)
    return card


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


__all__ = [
    "DEFAULT_EXPORTS_ROOT",
    "ExportRuntimeError",
    "OUTCOME_FAILED",
    "OUTCOME_SENT",
    "bare_query",
    "build_message",
    "card_path",
    "count_rows",
    "list_cards",
    "read_card",
    "render_script",
    "run_export",
    "run_query",
    "send_message",
    "smtp_password",
    "write_card",
]
