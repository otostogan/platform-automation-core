"""A short-lived database login for an operator's own client.

The database is reachable from nowhere but the host, on purpose. An operator
who needs pgAdmin or psql on a laptop gets a tunnel from the console and,
from the host, a throwaway role: a member of ``app`` that can log in for a
few minutes and is dropped afterwards. The application's own credential
never leaves the host, and a password that survives in a terminal history
opens nothing once the session is closed or expired.
"""

import re
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .database_runtime import (
    DATABASE_NAME,
    DATABASE_PORT,
    DATABASE_USER,
    SAFE_PASSWORD_PATTERN,
    DatabaseRuntimeError,
    database_container_name,
    generate_password,
    stored_database_password,
)

ROLE_PATTERN = re.compile(r"^tunnel_[0-9a-f]{8}$")
MAX_MINUTES = 240
DEFAULT_MINUTES = 30


class DatabaseSessionError(ValueError):
    pass


def container_address(
    project: str, environment: str, docker_executable: Path, runner=subprocess.run
) -> str:
    """The container's address on its own network; it changes on every restart."""
    try:
        result = runner(
            [
                str(docker_executable),
                "inspect",
                "--format",
                "{{ range .NetworkSettings.Networks }}{{ .IPAddress }}{{ end }}",
                database_container_name(project, environment),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DatabaseSessionError(
            "database container could not be inspected"
        ) from error
    address = _text(result.stdout).strip()
    if result.returncode != 0 or not address:
        raise DatabaseSessionError(
            "database container is not running; is the application deployed here?"
        )
    return address


def run_sql(
    project: str,
    environment: str,
    app_password: str,
    script: str,
    docker_executable: Path,
    runner=subprocess.run,
    label: str = "database session",
) -> str:
    """psql over stdin as the application user: values travel as psql variables, never in argv."""
    try:
        result = runner(
            [
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
                "--no-align",
                "--tuples-only",
                "--set",
                "ON_ERROR_STOP=1",
            ],
            input=script.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DatabaseSessionError(f"{label} could not be executed") from error
    if result.returncode != 0:
        raise DatabaseSessionError(f"{label} failed")
    return _text(result.stdout)


SWEEP = (
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
    " WHERE usename IN (SELECT rolname FROM pg_roles"
    " WHERE rolname LIKE 'tunnel\\_%' ESCAPE '\\' AND rolvaliduntil < now());\n"
    "SELECT format('DROP ROLE %I', rolname) FROM pg_roles"
    " WHERE rolname LIKE 'tunnel\\_%' ESCAPE '\\' AND rolvaliduntil < now() \\gexec\n"
)


def open_session(
    project: str,
    environment: str,
    minutes: int,
    databases_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    docker_executable: Path,
    runner=subprocess.run,
) -> dict[str, Any]:
    if not isinstance(minutes, int) or not 1 <= minutes <= MAX_MINUTES:
        raise DatabaseSessionError(f"minutes must be between 1 and {MAX_MINUTES}")
    try:
        app_password = stored_database_password(
            databases_root, project, environment, age_key_file, sops_executable, runner
        )
    except DatabaseRuntimeError as error:
        raise DatabaseSessionError(str(error)) from error

    role = f"tunnel_{secrets.token_hex(4)}"
    password = generate_password()
    if not SAFE_PASSWORD_PATTERN.fullmatch(password):
        raise DatabaseSessionError("generated password has unexpected characters")
    expires = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
        minutes=minutes
    )
    expires_at = expires.isoformat().replace("+00:00", "Z")
    address = container_address(project, environment, docker_executable, runner)

    script = (
        SWEEP
        + f"\\set p '{password}'\n"
        + f"\\set until '{expires_at}'\n"
        + f"CREATE ROLE {role} WITH LOGIN INHERIT IN ROLE {DATABASE_USER}"
        " PASSWORD :'p' VALID UNTIL :'until';\n"
    )
    run_sql(
        project,
        environment,
        app_password,
        script,
        docker_executable,
        runner,
        "database session open",
    )

    return {
        "operation": "database-session",
        "project": project,
        "environment": environment,
        "user": role,
        "password": password,
        "database": DATABASE_NAME,
        "port": DATABASE_PORT,
        "address": address,
        "expires_at": expires_at,
        "minutes": minutes,
        "close_with": f"platform database-session --project {project} --environment {environment} --close {role}",
    }


def close_session(
    project: str,
    environment: str,
    role: str,
    databases_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    docker_executable: Path,
    runner=subprocess.run,
) -> dict[str, Any]:
    if not ROLE_PATTERN.fullmatch(role):
        raise DatabaseSessionError("not a session role: expected tunnel_<8 hex>")
    try:
        app_password = stored_database_password(
            databases_root, project, environment, age_key_file, sops_executable, runner
        )
    except DatabaseRuntimeError as error:
        raise DatabaseSessionError(str(error)) from error
    script = (
        f"\\set r '{role}'\n"
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = :'r';\n"
        f"DROP ROLE IF EXISTS {role};\n"
    )
    run_sql(
        project,
        environment,
        app_password,
        script,
        docker_executable,
        runner,
        "database session close",
    )
    return {
        "operation": "database-session",
        "project": project,
        "environment": environment,
        "closed": role,
    }


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
