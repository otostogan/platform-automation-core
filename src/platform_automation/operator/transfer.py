"""A backup moved from one host to another, re-keyed on the workstation.

A dump is encrypted to the host that took it and to the company's recovery
key; another host cannot open it. The operator who holds the source host's
identity can: the dump is fetched, decrypted and re-encrypted to the target
host's recipients in one pipe on this machine — plaintext exists nowhere but
in that pipe — and handed to the target host's ``platform backup-import``,
which proves with its own identity that the envelope opens before keeping it.
Restoring it afterwards is the ordinary ``platform restore``.
"""

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..validate_manifest import load_yaml

BACKUPS_ROOT = "/var/backups/platform"
DUMP_SUFFIX = ".dump.age"
AGE_EXECUTABLE = "age"


class TransferError(RuntimeError):
    pass


@dataclass(frozen=True)
class Source:
    """A host a dump can come from, with what this machine holds for it."""

    infra: str
    name: str
    address: str
    key_file: Optional[Path]
    age_key: Optional[Path]
    retired: bool = False

    @property
    def label(self) -> str:
        state = " · retired" if self.retired else ""
        return f"{self.name}  {self.address}  ({self.infra}{state})"


def sops_recipients(root: Path) -> list:
    """The age recipients the application encrypts to: what its host can open."""
    document = load_yaml(root / ".sops.yaml")
    found = []
    try:
        for rule in document.get("creation_rules") or []:
            for group in rule.get("key_groups") or []:
                for item in group.get("age") or []:
                    recipient = str(item)
                    if recipient not in found:
                        found.append(recipient)
    except AttributeError:
        pass
    if len(found) < 2:
        raise TransferError(
            ".sops.yaml must name the target host and the recovery recipient"
        )
    return found


def candidate_sources(
    target_host: Optional[str],
    infras: list,
    read_hosts,
    read_retired_hosts,
    secret_path,
) -> list:
    """Every host of every registered infrastructure except the target itself."""
    target_name = (target_host or "").split(".")[0]
    sources = []
    for infra in infras:
        for retired, hosts in (
            (False, read_hosts(infra.path)),
            (True, read_retired_hosts(infra.path)),
        ):
            for host in hosts:
                if host.name == target_name:
                    continue
                sources.append(
                    Source(
                        infra=infra.name,
                        name=host.name,
                        address=host.address or host.name,
                        key_file=(
                            Path(host.key_file).expanduser() if host.key_file else None
                        ),
                        age_key=secret_path(infra.path, host, "secrets_age_key_source"),
                        retired=retired,
                    )
                )
    return sources


def dump_remote_path(project: str, environment: str, stamp: str) -> str:
    return f"{BACKUPS_ROOT}/{project}/{environment}/{stamp}{DUMP_SUFFIX}"


def fetch_command(remote_path: str) -> str:
    """Reads the dump on the source host; ops may, through sudo."""
    return shlex.join(["sudo", "-n", "cat", remote_path])


def import_command(
    project: str,
    environment: str,
    stamp: str,
    source: Source,
    source_project: str,
    source_environment: str,
) -> str:
    """The target host's import line; the re-keyed dump follows on standard input."""
    return shlex.join(
        [
            "sudo",
            "-n",
            "platform",
            "backup-import",
            "--project",
            project,
            "--environment",
            environment,
            "--stamp",
            stamp,
            "--source-host",
            source.name,
            "--source-project",
            source_project,
            "--source-environment",
            source_environment,
            "--json",
        ]
    )


def rekey_commands(source_key: Path, recipients: list) -> tuple:
    """The two halves of the pipe: decrypt with the source identity, encrypt to the target's."""
    decrypt = [AGE_EXECUTABLE, "--decrypt", "--identity", str(source_key)]
    encrypt = [AGE_EXECUTABLE]
    for recipient in recipients:
        encrypt += ["--recipient", recipient]
    return decrypt, encrypt


def rekey(
    dump: Path,
    output: Path,
    source_key: Path,
    recipients: list,
    popen=subprocess.Popen,
) -> None:
    """``age -d | age -r … -r …`` — the plaintext lives in the pipe and nowhere else."""
    decrypt, encrypt = rekey_commands(source_key, recipients)
    try:
        with dump.open("rb") as source, output.open("wb") as target:
            reader = popen(
                decrypt, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            writer = popen(
                encrypt, stdin=reader.stdout, stdout=target, stderr=subprocess.PIPE
            )
            reader.stdout.close()
            _, writer_errors = writer.communicate()
            _, reader_errors = reader.communicate()
    except OSError as error:
        raise TransferError(f"age could not run on this machine: {error}") from error
    if reader.returncode != 0:
        detail = (reader_errors or b"").decode("utf-8", "replace").strip()
        raise TransferError(
            "the dump did not decrypt with the source host's key"
            + (f": {detail}" if detail else "")
        )
    if writer.returncode != 0:
        detail = (writer_errors or b"").decode("utf-8", "replace").strip()
        raise TransferError(
            "re-encryption to the target recipients failed"
            + (f": {detail}" if detail else "")
        )
    if output.stat().st_size == 0:
        raise TransferError("re-encryption produced an empty file")


def describe_entry(entry: dict) -> str:
    """One line of the source host's backup list, for the choice."""
    stamp = str(entry.get("stamp", "?"))
    release = entry.get("release_tag") or "?"
    verified = "proven" if entry.get("verified") else "unproven"
    return f"{stamp}  {release}  {verified}"


def backup_entries(document: Any) -> list:
    entries = document.get("backups") if isinstance(document, dict) else None
    return [e for e in (entries or []) if isinstance(e, dict) and e.get("stamp")]
