#!/usr/bin/env python3

"""Copy encrypted dumps off the host that made them.

A backup on the same disk as its database does not survive losing the disk.
Dumps are already encrypted to the application's own age recipients before
this module ever sees them, so nothing readable leaves the host.

The upload reconciles rather than pushes: every run sends whatever local dump
is missing remotely, not merely the one just taken. Enabling offsite storage
therefore carries the existing dumps up on the first run, and an upload that
failed yesterday is retried today instead of being lost. The work is bounded
by local retention, which has already capped how many dumps exist.

Object keys deliberately carry no application version: they are readable by
anyone who can list the bucket, and the version belongs in the metadata card.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from .backup_runtime import CARD_SUFFIX, DUMP_SUFFIX, list_backups

DEFAULT_OFFSITE_CONFIG = Path("/etc/platform/backup-offsite.json")
DEFAULT_CREDENTIALS_FILE = Path("/etc/platform/keys/s3.env")

BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


class OffsiteError(RuntimeError):
    pass


def load_offsite_config(
    path: Path = DEFAULT_OFFSITE_CONFIG,
) -> Optional[dict[str, Any]]:
    """Read the offsite settings, or report that there are none.

    A host with no configuration keeps its backups local, which stays a valid
    and supported answer rather than an error.
    """
    if path.is_symlink() or not path.is_file():
        return None

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OffsiteError("offsite configuration is unreadable") from error

    if not isinstance(document, dict):
        raise OffsiteError("offsite configuration must be an object")

    if not document.get("enabled"):
        return None

    for field in ("bucket", "prefix", "region"):
        if not isinstance(document.get(field), str) or not document[field]:
            raise OffsiteError(f"offsite configuration needs {field}")

    if not BUCKET_PATTERN.fullmatch(document["bucket"]):
        raise OffsiteError("offsite bucket name is invalid")

    if not PREFIX_PATTERN.fullmatch(document["prefix"]):
        raise OffsiteError("offsite prefix is invalid")

    return document


def load_credentials(
    path: Path = DEFAULT_CREDENTIALS_FILE,
) -> tuple[str, str]:
    if path.is_symlink() or not path.is_file():
        raise OffsiteError("offsite credentials are missing")

    values: dict[str, str] = {}

    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            name, _, value = line.partition("=")
            values[name.strip()] = value.strip().strip('"').strip("'")
    except (OSError, UnicodeDecodeError) as error:
        raise OffsiteError("offsite credentials are unreadable") from error

    key = values.get("AWS_ACCESS_KEY_ID")
    secret = values.get("AWS_SECRET_ACCESS_KEY")

    if not key or not secret:
        raise OffsiteError("offsite credentials are incomplete")

    return key, secret


def object_key(prefix: str, project: str, environment: str, name: str) -> str:
    """Host prefix first, so one IAM condition covers every project on it."""
    return f"{prefix.strip('/')}/{project}/{environment}/{name}"


def build_client(config: dict[str, Any], credentials: tuple[str, str]):
    try:
        import boto3
        from botocore.config import Config
    except ImportError as error:  # pragma: no cover - host package
        raise OffsiteError("boto3 is not installed on this host") from error

    key, secret = credentials

    return boto3.client(
        "s3",
        region_name=config["region"],
        endpoint_url=config.get("endpoint") or None,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        config=Config(
            retries={"max_attempts": 5, "mode": "standard"},
            connect_timeout=30,
            read_timeout=300,
        ),
    )


def remote_names(
    client,
    config: dict[str, Any],
    project: str,
    environment: str,
) -> set[str]:
    """List what is already there, using the one permission the host has."""
    prefix = object_key(config["prefix"], project, environment, "")
    names: set[str] = set()
    token = None

    while True:
        arguments = {"Bucket": config["bucket"], "Prefix": prefix}

        if token:
            arguments["ContinuationToken"] = token

        response = client.list_objects_v2(**arguments)

        for item in response.get("Contents", []) or []:
            names.add(str(item.get("Key", "")).rpartition("/")[2])

        if not response.get("IsTruncated"):
            return names

        token = response.get("NextContinuationToken")

        if not token:
            return names


def pending_uploads(local: list[str], remote: set[str]) -> list[tuple[str, str]]:
    """Pair every local file with its remote name, oldest stamp first."""
    pending: list[tuple[str, str]] = []

    for stamp in local:
        for suffix in (DUMP_SUFFIX, CARD_SUFFIX):
            name = f"{stamp}{suffix}"

            if name not in remote:
                pending.append((stamp, name))

    return pending


def upload_backups(
    project: str,
    environment: str,
    directory: Path,
    config_path: Path = DEFAULT_OFFSITE_CONFIG,
    credentials_path: Path = DEFAULT_CREDENTIALS_FILE,
    client_factory=build_client,
) -> dict[str, Any]:
    """Send every local dump that is not in the bucket yet."""
    config = load_offsite_config(config_path)

    if config is None:
        return {"state": "not-configured", "uploaded": [], "bucket": None}

    client = client_factory(config, load_credentials(credentials_path))
    local = list_backups(directory)

    try:
        remote = remote_names(client, config, project, environment)
    except Exception as error:  # boto3 raises provider-specific errors
        raise OffsiteError(f"offsite listing failed: {error}") from error

    uploaded: list[str] = []

    for _, name in pending_uploads(local, remote):
        source = directory / name

        if source.is_symlink() or not source.is_file():
            continue

        key = object_key(config["prefix"], project, environment, name)

        try:
            with source.open("rb") as body:
                client.put_object(
                    Bucket=config["bucket"],
                    Key=key,
                    Body=body,
                )
        except Exception as error:
            raise OffsiteError(f"offsite upload failed for {name}: {error}") from error

        uploaded.append(name)

    return {
        "state": "uploaded",
        "bucket": config["bucket"],
        "prefix": object_key(config["prefix"], project, environment, ""),
        "uploaded": uploaded,
        "local_backups": len(local),
    }


def read_operator_credentials(stream) -> tuple[str, str]:
    """Take a reader key from the operator, at the moment it is needed.

    The host holds a write-only credential; reading the history back is an
    operator action, so the key arrives on standard input exactly as a
    registry token does for a deployment, and never lands on disk.
    """
    payload = stream.read(8192)

    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")

    values: dict[str, str] = {}

    for line in payload.splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip('"').strip("'")

    key = values.get("AWS_ACCESS_KEY_ID")
    secret = values.get("AWS_SECRET_ACCESS_KEY")

    if not key or not secret:
        raise OffsiteError(
            "reader credentials must supply AWS_ACCESS_KEY_ID and "
            "AWS_SECRET_ACCESS_KEY on standard input"
        )

    return key, secret


def download_backup(
    project: str,
    environment: str,
    stamp: str,
    directory: Path,
    credentials: tuple[str, str],
    config_path: Path = DEFAULT_OFFSITE_CONFIG,
    client_factory=build_client,
) -> list[str]:
    """Fetch a dump and its card back onto the host."""
    config = load_offsite_config(config_path)

    if config is None:
        raise OffsiteError("offsite storage is not configured on this host")

    client = client_factory(config, credentials)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fetched: list[str] = []

    for suffix in (DUMP_SUFFIX, CARD_SUFFIX):
        name = f"{stamp}{suffix}"
        destination = directory / name

        if destination.exists():
            continue

        key = object_key(config["prefix"], project, environment, name)

        try:
            response = client.get_object(Bucket=config["bucket"], Key=key)
            body = response["Body"].read()
        except Exception as error:
            raise OffsiteError(
                f"offsite download failed for {name}: {error}"
            ) from error

        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )

        with os.fdopen(descriptor, "wb") as output:
            output.write(body)

        fetched.append(name)

    return fetched


def offsite_status(
    project: str,
    environment: str,
    directory: Path,
    config_path: Path = DEFAULT_OFFSITE_CONFIG,
    credentials_path: Path = DEFAULT_CREDENTIALS_FILE,
    client_factory=build_client,
) -> dict[str, Any]:
    """Report whether offsite storage is configured and current.

    A host set up for offsite backups that has quietly stopped uploading is
    exactly the failure discovered too late, so the gap is reported as a
    count rather than left to a log.
    """
    try:
        config = load_offsite_config(config_path)
    except OffsiteError as error:
        return {"state": "error", "error": str(error)}

    if config is None:
        return {"state": "not-configured"}

    try:
        client = client_factory(config, load_credentials(credentials_path))
        remote = remote_names(client, config, project, environment)
    except OffsiteError as error:
        return {"state": "error", "bucket": config["bucket"], "error": str(error)}
    except Exception as error:
        return {
            "state": "error",
            "bucket": config["bucket"],
            "error": f"offsite listing failed: {error}",
        }

    local = list_backups(directory)
    missing = [stamp for stamp, _ in pending_uploads(local, remote)]

    return {
        "state": "current" if not missing else "behind",
        "bucket": config["bucket"],
        "remote_objects": len(remote),
        "not_uploaded": sorted(set(missing)),
    }
