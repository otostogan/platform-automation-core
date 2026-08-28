"""Root-only background reconciliation; shares the deploy activation lock.

Docker events and certificate changes must not reload nginx behind the CLI's back.
This bounded oneshot is called by a systemd timer and once at proxy startup.
"""

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Optional

from .nginx_transaction import (
    MAX_DEFAULT_CONFIG_BYTES,
    NginxTransactionBusyError,
    NginxTransactionError,
    NginxTransactionManager,
    _atomic_write,
    _ensure_owned_directory,
    _read_regular_file,
    _unlink_file,
    nginx_transaction_lock,
)


def certificate_fingerprint(root: Path) -> str:
    """Inspect metadata, including ACME symlink targets; never read private keys."""
    _ensure_owned_directory(root, 0o755)
    entries = []
    for directory, subdirs, files in os.walk(root, followlinks=False):
        for name in sorted(subdirs + files):
            path = Path(directory) / name
            info = path.lstat()
            target = None
            target_info = None
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                resolved = path.resolve()
                if not resolved.is_relative_to(root.resolve()):
                    raise NginxTransactionError(
                        "certificate link escapes certificate root"
                    )
                try:
                    linked = path.stat()
                    target_info = (
                        linked.st_ino,
                        linked.st_size,
                        linked.st_mtime_ns,
                        linked.st_ctime_ns,
                    )
                except FileNotFoundError:
                    pass
            entries.append(
                (
                    str(path.relative_to(root)),
                    info.st_mode,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    target,
                    target_info,
                )
            )
            if len(entries) > 10000:
                raise NginxTransactionError("too many certificate entries")
    return hashlib.sha256(json.dumps(sorted(entries)).encode()).hexdigest()


def reconcile(manager: NginxTransactionManager, certificates: Path) -> bool:
    """Return True after reload; False for unchanged state or an active deploy."""
    try:
        with nginx_transaction_lock(manager.lock_root):
            manager.check_no_pending_activation()
            before = certificate_fingerprint(certificates)
            candidate = manager.render_config()
            after = certificate_fingerprint(certificates)
            if before != after:
                raise NginxTransactionError(
                    "certificates changed while generating config; retry"
                )
            digest = hashlib.sha256(candidate + after.encode()).hexdigest().encode()
            state_path = manager.ownership_root / ".reconciled-state"
            previous_state = _read_regular_file(state_path, 128, 0o600)
            previous_config = _read_regular_file(
                manager.default_config, MAX_DEFAULT_CONFIG_BYTES, 0o644
            )
            if candidate == previous_config and previous_state == digest:
                return False

            _atomic_write(manager.default_config, candidate, 0o644)
            reloading = False
            try:
                manager.test_configuration()
                reloading = True
                manager.reload_nginx()
                _atomic_write(state_path, digest, 0o600)
            except (OSError, NginxTransactionError):
                if previous_config is None:
                    _unlink_file(manager.default_config)
                else:
                    _atomic_write(manager.default_config, previous_config, 0o644)
                if reloading:
                    manager.test_configuration()
                    manager.reload_nginx()
                raise
            return True
    except NginxTransactionBusyError:
        # Do not queue a stale candidate; the next tick generates a NEW snapshot.
        return False


def main(arguments: Optional[list[str]] = None) -> int:
    if arguments is None:
        arguments = sys.argv[1:]
    if arguments or os.geteuid() != 0:
        print(
            "nginx reconciliation requires root and accepts no arguments",
            file=sys.stderr,
        )
        return 2
    root = Path("/var/lib/platform/proxy")
    manager = NginxTransactionManager(
        vhost_root=root / "vhost.d",
        ownership_root=root / "managed-vhosts",
        default_config=root / "conf.d/default.conf",
        lock_root=Path("/run/platform/locks"),
        raw_allowlist=Path("/etc/platform/nginx-raw-projects.json"),
        docker_executable=Path("/usr/bin/docker"),
        nginx_container="platform-nginx",
    )
    try:
        changed = reconcile(manager, root / "certs")
    except (OSError, NginxTransactionError) as error:
        print(f"nginx reconciliation failed: {error}", file=sys.stderr)
        return 1
    print(
        "nginx reconciled" if changed else "nginx unchanged or deployment in progress"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
