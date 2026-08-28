#!/usr/bin/env python3

import fcntl
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


PROJECT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
OPERATION_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
ALLOWED_ENVIRONMENTS = {"lab", "staging", "production"}


class OperationLockError(ValueError):
    pass


class OperationAlreadyRunningError(OperationLockError):
    pass


def validate_lock_identity(
    project: str,
    environment: str,
    operation: str,
) -> None:
    if not PROJECT_PATTERN.fullmatch(project):
        raise OperationLockError(f"invalid project for operation lock: {project}")

    if environment not in ALLOWED_ENVIRONMENTS:
        raise OperationLockError(
            f"invalid environment for operation lock: {environment}"
        )

    if not OPERATION_PATTERN.fullmatch(operation):
        raise OperationLockError(f"invalid operation name: {operation}")


@contextmanager
def project_environment_lock(
    lock_root: Path,
    project: str,
    environment: str,
    operation: str,
) -> Iterator[Path]:
    validate_lock_identity(
        project,
        environment,
        operation,
    )

    if lock_root.is_symlink():
        raise OperationLockError(f"lock root cannot be a symbolic link: {lock_root}")

    lock_root.mkdir(
        mode=0o700,
        parents=True,
        exist_ok=True,
    )
    lock_root.chmod(0o700)
    lock_root = lock_root.resolve()

    lock_path = lock_root / f"{project}-{environment}.lock"
    flags = os.O_RDWR | os.O_CREAT

    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        descriptor = os.open(
            lock_path,
            flags,
            0o600,
        )
    except OSError as error:
        raise OperationLockError(f"cannot open operation lock: {lock_path}") from error

    acquired = False

    try:
        try:
            fcntl.flock(
                descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
            acquired = True
        except BlockingIOError as error:
            raise OperationAlreadyRunningError(
                f"another operation is already running for " f"{project}/{environment}"
            ) from error

        metadata = {
            "environment": environment,
            "operation": operation,
            "pid": os.getpid(),
            "project": project,
        }
        content = (
            json.dumps(
                metadata,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, content)
        os.fsync(descriptor)

        yield lock_path
    finally:
        if acquired:
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_UN)

        os.close(descriptor)
