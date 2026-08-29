import os
import sys
from pathlib import Path
from typing import Optional

from .operation_lock import OperationLockError
from .database_runtime import DatabaseRuntimeError
from .reboot_recovery import (
    RebootRecoveryError,
    discover_recovery_scopes,
    recover_project_environment_secrets,
)
from .release_ledger import ReleaseLedgerError
from .runtime_secrets import RuntimeSecretsError


DEFAULT_PROJECTS_ROOT = Path("/var/lib/platform/projects")
DEFAULT_RELEASES_ROOT = Path("/var/lib/platform/releases")
DEFAULT_LOCK_ROOT = Path("/run/platform/locks")
DEFAULT_RUNTIME_ROOT = Path("/run/platform/secrets")
DEFAULT_AGE_KEY_FILE = Path("/etc/platform/keys/age.key")
DEFAULT_SOPS_EXECUTABLE = Path("/usr/local/bin/sops")
MINIMUM_AGE_RECIPIENTS_ENVIRONMENT = "PLATFORM_MINIMUM_AGE_RECIPIENTS"


class BootRecoveryError(RuntimeError):
    pass


def require_root() -> None:
    if os.geteuid() != 0:
        raise BootRecoveryError("boot recovery must run as root")


def configured_minimum_age_recipients() -> int:
    value = os.environ.get(MINIMUM_AGE_RECIPIENTS_ENVIRONMENT, "1")

    try:
        minimum = int(value)
    except ValueError as error:
        raise BootRecoveryError(
            f"{MINIMUM_AGE_RECIPIENTS_ENVIRONMENT} must be a positive integer"
        ) from error

    if minimum < 1:
        raise BootRecoveryError(
            f"{MINIMUM_AGE_RECIPIENTS_ENVIRONMENT} must be a positive integer"
        )

    return minimum


def run_boot_recovery(
    projects_root: Path,
    releases_root: Path,
    lock_root: Path,
    runtime_root: Path,
    age_key_file: Path,
    sops_executable: Path,
    minimum_age_recipients: int = 1,
) -> list[Path]:
    require_root()

    recovered_paths: list[Path] = []

    for project, environment in discover_recovery_scopes(projects_root):
        destination: Optional[Path] = recover_project_environment_secrets(
            project=project,
            environment=environment,
            projects_root=projects_root,
            releases_root=releases_root,
            lock_root=lock_root,
            runtime_root=runtime_root,
            age_key_file=age_key_file,
            sops_executable=sops_executable,
            minimum_age_recipients=minimum_age_recipients,
        )

        if destination is not None:
            recovered_paths.append(destination)

    return recovered_paths


def main(
    arguments: Optional[list[str]] = None,
) -> int:
    if arguments is None:
        arguments = sys.argv[1:]

    if arguments:
        print(
            "boot recovery error: command-line arguments are not supported",
            file=sys.stderr,
        )
        return 2

    try:
        recovered_paths = run_boot_recovery(
            projects_root=DEFAULT_PROJECTS_ROOT,
            releases_root=DEFAULT_RELEASES_ROOT,
            lock_root=DEFAULT_LOCK_ROOT,
            runtime_root=DEFAULT_RUNTIME_ROOT,
            age_key_file=DEFAULT_AGE_KEY_FILE,
            sops_executable=DEFAULT_SOPS_EXECUTABLE,
            minimum_age_recipients=configured_minimum_age_recipients(),
        )
    except (
        BootRecoveryError,
        DatabaseRuntimeError,
        OperationLockError,
        RebootRecoveryError,
        ReleaseLedgerError,
        RuntimeSecretsError,
        OSError,
    ) as error:
        print(
            f"boot recovery error: {error}",
            file=sys.stderr,
        )
        return 1

    print(
        f"boot recovery completed: " f"{len(recovered_paths)} secret file(s) restored"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
