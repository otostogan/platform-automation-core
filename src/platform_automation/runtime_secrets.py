#!/usr/bin/env python3

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_RUNTIME_ROOT = Path("/run/platform/secrets")
DEFAULT_AGE_KEY_FILE = Path("/etc/platform/keys/age.key")
DEFAULT_SOPS_EXECUTABLE = Path("/usr/local/bin/sops")

PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
RELEASE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALLOWED_ENVIRONMENTS = {"lab", "staging", "production"}

MAX_DECRYPTED_BYTES = 1024 * 1024


class RuntimeSecretsError(ValueError):
    pass


def validate_runtime_identity(
    project: str,
    environment: str,
    release_id: str,
) -> None:
    if not PROJECT_PATTERN.fullmatch(project):
        raise RuntimeSecretsError("invalid secrets project")

    if environment not in ALLOWED_ENVIRONMENTS:
        raise RuntimeSecretsError("invalid secrets environment")

    if not RELEASE_ID_PATTERN.fullmatch(release_id):
        raise RuntimeSecretsError("invalid secrets release ID")


def decrypt_sops_document(
    encrypted_file: Path,
    age_key_file: Path = DEFAULT_AGE_KEY_FILE,
    sops_executable: Path = DEFAULT_SOPS_EXECUTABLE,
    runner=None,
) -> dict[str, Any]:
    if runner is None:
        runner = subprocess.run

    if encrypted_file.is_symlink() or not encrypted_file.is_file():
        raise RuntimeSecretsError("encrypted secrets file is not a regular file")

    if age_key_file.is_symlink() or not age_key_file.is_file():
        raise RuntimeSecretsError("age private key is unavailable")

    environment = os.environ.copy()
    environment["SOPS_AGE_KEY_FILE"] = str(age_key_file)

    try:
        result = runner(
            [
                str(sops_executable),
                "decrypt",
                "--output-type",
                "json",
                str(encrypted_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeSecretsError("SOPS decryption could not be executed") from error

    if result.returncode != 0:
        raise RuntimeSecretsError("SOPS decryption failed")

    if len(result.stdout) > MAX_DECRYPTED_BYTES:
        raise RuntimeSecretsError("decrypted secrets exceed the size limit")

    try:
        document = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeSecretsError("decrypted secrets are not valid JSON") from error

    if not isinstance(document, dict):
        raise RuntimeSecretsError("decrypted secrets must contain an object")

    return document


def format_env_value(value: Any) -> str:
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        text = str(value)
    else:
        raise RuntimeSecretsError("environment secret values must be scalar")

    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )

    return f'"{escaped}"'


def render_env_file(document: dict[str, Any]) -> bytes:
    lines: list[str] = []

    for key in sorted(document):
        if not ENV_KEY_PATTERN.fullmatch(key):
            raise RuntimeSecretsError(f"invalid environment variable name: {key}")

        lines.append(f"{key}={format_env_value(document[key])}")

    return ("\n".join(lines) + "\n").encode("utf-8")


def create_private_runtime_directory(
    runtime_root: Path,
    project: str,
    environment: str,
    release_id: str,
) -> Path:
    paths = [runtime_root]
    current = runtime_root

    for part in (project, environment, release_id):
        current = current / part
        paths.append(current)

    for path in paths:
        if path.is_symlink():
            raise RuntimeSecretsError("runtime secrets path is unsafe")

        path.mkdir(
            mode=0o700,
            exist_ok=True,
        )

        if path.is_symlink() or not path.is_dir():
            raise RuntimeSecretsError("runtime secrets path is unsafe")

        path.chmod(0o700)

    return current


def write_runtime_env(
    destination: Path,
    content: bytes,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".app.env.",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_name)

    try:
        os.fchmod(descriptor, 0o600)

        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

        os.replace(
            temporary_path,
            destination,
        )
        destination.chmod(0o600)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def materialize_env_secrets(
    encrypted_file: Path,
    project: str,
    environment: str,
    release_id: str,
    runtime_root: Path = DEFAULT_RUNTIME_ROOT,
    age_key_file: Path = DEFAULT_AGE_KEY_FILE,
    sops_executable: Path = DEFAULT_SOPS_EXECUTABLE,
) -> Path:
    validate_runtime_identity(
        project,
        environment,
        release_id,
    )

    document = decrypt_sops_document(
        encrypted_file,
        age_key_file=age_key_file,
        sops_executable=sops_executable,
    )
    content = render_env_file(document)
    runtime_directory = create_private_runtime_directory(
        runtime_root,
        project,
        environment,
        release_id,
    )
    destination = runtime_directory / "app.env"

    write_runtime_env(
        destination,
        content,
    )

    return destination
