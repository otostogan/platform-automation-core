import fcntl
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Optional

from .nginx_config import NginxConfigError, generate_vhost_fragments


IDENTITY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
HOST_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
SERVER_NAME_PATTERN = re.compile(r"\bserver_name\s+([^;]+);")
RELEASE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ENVIRONMENTS = {"lab", "staging", "production"}
OWNERSHIP_API_VERSION = "platform.nginx-ownership/v1"
MAX_FRAGMENT_BYTES = 64 * 1024
MAX_METADATA_BYTES = 256 * 1024
MAX_DEFAULT_CONFIG_BYTES = 8 * 1024 * 1024


class NginxTransactionError(RuntimeError):
    pass


class NginxTransactionBusyError(NginxTransactionError):
    pass


@dataclass(frozen=True)
class NginxFragmentPlan:
    project: str
    environment: str
    release_id: str
    fragments: Mapping[str, str]

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset(
            name[: -len("_location")] if name.endswith("_location") else name
            for name in self.fragments
        )


@dataclass(frozen=True)
class NginxOwnershipRecord:
    project: str
    environment: str
    release_id: str
    fragments: tuple[str, ...]


def _validate_identity(project: str, environment: str, release_id: str) -> None:
    if not isinstance(project, str) or not IDENTITY_PATTERN.fullmatch(project):
        raise NginxTransactionError(f"invalid project identity: {project!r}")
    if environment not in ENVIRONMENTS:
        raise NginxTransactionError(f"invalid environment: {environment!r}")
    if not isinstance(release_id, str) or not RELEASE_PATTERN.fullmatch(release_id):
        raise NginxTransactionError(f"invalid release identity: {release_id!r}")


def _validate_fragment(name: str, content: str) -> None:
    host = name[: -len("_location")] if name.endswith("_location") else name
    if not HOST_PATTERN.fullmatch(host):
        raise NginxTransactionError(f"invalid nginx fragment name: {name!r}")
    if not isinstance(content, str):
        raise NginxTransactionError(f"nginx fragment {name!r} must be text")
    if "\x00" in content:
        raise NginxTransactionError(f"nginx fragment {name!r} contains a NUL byte")
    if len(content.encode("utf-8")) > MAX_FRAGMENT_BYTES:
        raise NginxTransactionError(f"nginx fragment {name!r} is too large")


def build_fragment_plan(
    project: str,
    environment: str,
    release_id: str,
    fragments: Mapping[str, str],
) -> NginxFragmentPlan:
    _validate_identity(project, environment, release_id)
    if not isinstance(fragments, Mapping):
        raise NginxTransactionError("nginx fragments must be a mapping")
    copied: dict[str, str] = {}
    for name, content in fragments.items():
        _validate_fragment(name, content)
        copied[name] = content
    return NginxFragmentPlan(
        project=project,
        environment=environment,
        release_id=release_id,
        fragments=MappingProxyType(copied),
    )


def build_release_fragment_plan(
    manifest: dict[str, Any],
    release_id: str,
    allowed_raw_projects: set[str],
) -> NginxFragmentPlan:
    try:
        fragments = generate_vhost_fragments(manifest, allowed_raw_projects)
    except (KeyError, TypeError, NginxConfigError) as exc:
        raise NginxTransactionError(f"cannot render nginx fragments: {exc}") from exc
    return build_fragment_plan(
        manifest["project"], manifest["environment"], release_id, fragments
    )


def _ensure_owned_directory(path: Path, mode: int) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise NginxTransactionError(
            f"required directory does not exist: {path}"
        ) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise NginxTransactionError(f"path is not a directory: {path}")
    if info.st_uid != os.geteuid() or info.st_gid != os.getegid():
        raise NginxTransactionError(f"directory has unexpected ownership: {path}")
    if stat.S_IMODE(info.st_mode) != mode:
        raise NginxTransactionError(f"directory has unexpected mode: {path}")


def _read_regular_file(
    path: Path, limit: int, expected_mode: Optional[int] = None
) -> Optional[bytes]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise NginxTransactionError(f"refusing unsafe file: {path}")
    if info.st_uid != os.geteuid() or info.st_gid != os.getegid():
        raise NginxTransactionError(f"file has unexpected ownership: {path}")
    if expected_mode is not None and stat.S_IMODE(info.st_mode) != expected_mode:
        raise NginxTransactionError(f"file has unexpected mode: {path}")
    if info.st_size > limit:
        raise NginxTransactionError(f"file is too large: {path}")
    return path.read_bytes()


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _unlink_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _scope_name(project: str, environment: str) -> str:
    return f"{project}--{environment}.json"


def _configured_server_names(configuration: str) -> set[str]:
    names = set()
    uncommented = "\n".join(
        line.split("#", 1)[0] for line in configuration.splitlines()
    )
    for match in SERVER_NAME_PATTERN.finditer(uncommented):
        names.update(match.group(1).split())
    return names


def _parse_ownership(content: bytes, source: Path) -> NginxOwnershipRecord:
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NginxTransactionError(f"invalid ownership metadata: {source}") from exc
    if not isinstance(data, dict) or data.get("api_version") != OWNERSHIP_API_VERSION:
        raise NginxTransactionError(f"unsupported ownership metadata: {source}")
    project = data.get("project")
    environment = data.get("environment")
    release_id = data.get("release_id")
    fragments = data.get("fragments")
    _validate_identity(project, environment, release_id)
    if not isinstance(fragments, list) or any(
        not isinstance(item, str) for item in fragments
    ):
        raise NginxTransactionError(f"invalid fragment inventory: {source}")
    if fragments != sorted(set(fragments)):
        raise NginxTransactionError(f"fragment inventory is not canonical: {source}")
    for name in fragments:
        _validate_fragment(name, "")
    return NginxOwnershipRecord(project, environment, release_id, tuple(fragments))


def load_raw_project_allowlist(path: Path) -> set[str]:
    content = _read_regular_file(path, MAX_METADATA_BYTES, 0o600)
    if content is None:
        raise NginxTransactionError(f"raw nginx allowlist does not exist: {path}")
    try:
        values = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NginxTransactionError(f"invalid raw nginx allowlist: {path}") from exc
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not IDENTITY_PATTERN.fullmatch(value)
        for value in values
    ):
        raise NginxTransactionError(
            "raw nginx allowlist must be a list of project names"
        )
    if values != sorted(set(values)):
        raise NginxTransactionError("raw nginx allowlist must be sorted and unique")
    return set(values)


@contextmanager
def nginx_transaction_lock(lock_root: Path) -> Iterator[None]:
    _ensure_owned_directory(lock_root, 0o700)
    path = lock_root / "nginx-fragments.lock"
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise NginxTransactionError(
            f"cannot open nginx transaction lock: {path}"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise NginxTransactionError(f"refusing unsafe lock file: {path}")
        if info.st_uid != os.geteuid() or info.st_gid != os.getegid():
            raise NginxTransactionError(f"lock has unexpected ownership: {path}")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise NginxTransactionBusyError(
                "another nginx fragment transaction is in progress"
            ) from exc
        yield
    finally:
        os.close(descriptor)


class NginxFragmentTransaction:
    def __init__(
        self,
        manager: "NginxTransactionManager",
        plan: NginxFragmentPlan,
        previous: Optional[NginxOwnershipRecord],
        snapshots: Mapping[str, Optional[bytes]],
        metadata_snapshot: Optional[bytes],
    ) -> None:
        self.manager = manager
        self.plan = plan
        self.previous = previous
        self.snapshots = dict(snapshots)
        self.metadata_snapshot = metadata_snapshot
        self.staged = False
        self.activated = False
        self.restored = False
        self.config_snapshot = _read_regular_file(
            manager.default_config, MAX_DEFAULT_CONFIG_BYTES, 0o644
        )

    @property
    def previous_hosts(self) -> frozenset[str]:
        if self.previous is None:
            return frozenset()
        return frozenset(
            name[: -len("_location")] if name.endswith("_location") else name
            for name in self.previous.fragments
        )

    def stage(self) -> None:
        if self.staged:
            return
        # Persist intent before any live files change. A killed deploy must not
        # let the background reconciler activate an uncommitted candidate.
        _atomic_write(
            self.manager.pending_path,
            (
                json.dumps(
                    {
                        "project": self.plan.project,
                        "environment": self.plan.environment,
                        "release_id": self.plan.release_id,
                    }
                )
                + "\n"
            ).encode(),
            0o600,
        )
        self.staged = True

    def _install_fragments(self) -> None:
        try:
            for name in sorted(self.snapshots):
                path = self.manager.vhost_root / name
                if name in self.plan.fragments:
                    _atomic_write(
                        path, self.plan.fragments[name].encode("utf-8"), 0o644
                    )
                elif path.exists():
                    _unlink_file(path)
        except (OSError, NginxTransactionError) as exc:
            try:
                self._restore_files()
            except (OSError, NginxTransactionError) as restore_error:
                raise NginxTransactionError(
                    f"cannot stage nginx fragments: {exc}; restore failed: "
                    f"{restore_error}"
                ) from exc
            raise NginxTransactionError(f"cannot stage nginx fragments: {exc}") from exc

    def activate(self) -> None:
        if self.activated:
            return
        if not self.staged:
            raise NginxTransactionError(
                "nginx fragments must be staged before activation"
            )
        try:
            # Called only after Compose healthchecks. The watch process cannot
            # write this tree or signal nginx; it writes a preview in /tmp only.
            self._install_fragments()
            self.manager.regenerate_config(self.plan.hosts, self.previous_hosts)
            self.manager.test_configuration()
            self.manager.write_ownership(self.plan)
            self.manager.reload_nginx()
        except (OSError, NginxTransactionError) as error:
            try:
                self.manager.restore_metadata(self.plan, self.metadata_snapshot)
            except (OSError, NginxTransactionError) as restore_error:
                raise NginxTransactionError(
                    f"nginx activation failed: {error}; metadata restore failed: "
                    f"{restore_error}"
                ) from error
            raise NginxTransactionError(f"nginx activation failed: {error}") from error
        self.activated = True

    def rollback(self) -> None:
        if not self.staged:
            return
        try:
            self._restore_files()
            self.manager.restore_metadata(self.plan, self.metadata_snapshot)
            # The previous containers can have NEW IPs after Compose restores
            # them. Re-render from Docker, never reload a saved stale upstream.
            self.manager.regenerate_config(self.previous_hosts, self.plan.hosts)
            self.manager.test_configuration()
            self.manager.reload_nginx()
        except (OSError, NginxTransactionError) as error:
            raise NginxTransactionError(
                f"cannot restore nginx transaction: {error}"
            ) from error
        self.staged = False
        self.activated = False
        self.restored = True

    def _restore_files(self) -> None:
        for name, content in sorted(self.snapshots.items()):
            path = self.manager.vhost_root / name
            if content is None:
                if path.exists():
                    _unlink_file(path)
            else:
                _atomic_write(path, content, 0o644)
        if self.config_snapshot is None:
            if self.manager.default_config.exists():
                _unlink_file(self.manager.default_config)
        else:
            _atomic_write(self.manager.default_config, self.config_snapshot, 0o644)


class NginxTransactionManager:
    def __init__(
        self,
        vhost_root: Path,
        ownership_root: Path,
        default_config: Path,
        lock_root: Path,
        raw_allowlist: Path,
        docker_executable: Path,
        nginx_container: str,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
        convergence_timeout: float = 15.0,
        convergence_interval: float = 0.25,
        docker_gen_container: str = "platform-docker-gen",
        command_timeout: float = 30.0,
    ) -> None:
        self.vhost_root = Path(vhost_root)
        self.ownership_root = Path(ownership_root)
        self.default_config = Path(default_config)
        self.lock_root = Path(lock_root)
        self.raw_allowlist = Path(raw_allowlist)
        self.docker_executable = Path(docker_executable)
        self.nginx_container = nginx_container
        self.runner = runner
        self.sleeper = sleeper
        self.convergence_timeout = convergence_timeout
        self.convergence_interval = convergence_interval
        self.docker_gen_container = docker_gen_container
        self.command_timeout = command_timeout

    @property
    def pending_path(self) -> Path:
        return self.ownership_root / ".activation-pending"

    def check_no_pending_activation(self) -> None:
        _ensure_owned_directory(self.ownership_root, 0o700)
        if _read_regular_file(self.pending_path, MAX_METADATA_BYTES, 0o600) is not None:
            raise NginxTransactionError(
                "unfinished nginx activation requires operator review"
            )

    def build_plan(
        self, manifest: dict[str, Any], release_id: str
    ) -> NginxFragmentPlan:
        return build_release_fragment_plan(
            manifest, release_id, load_raw_project_allowlist(self.raw_allowlist)
        )

    @contextmanager
    def prepare(self, plan: NginxFragmentPlan) -> Iterator[NginxFragmentTransaction]:
        with nginx_transaction_lock(self.lock_root):
            self.check_no_pending_activation()
            transaction = self._prepare_locked(plan)
            try:
                yield transaction
            except BaseException:
                if transaction.restored:
                    _unlink_file(self.pending_path)
                raise
            else:
                if transaction.activated or transaction.restored:
                    _unlink_file(self.pending_path)

    def activate_current(self, manifest: dict[str, Any], release_id: str) -> None:
        plan = self.build_plan(manifest, release_id)
        with self.prepare(plan) as transaction:
            transaction.stage()
            transaction.activate()

    def _prepare_locked(self, plan: NginxFragmentPlan) -> NginxFragmentTransaction:
        _ensure_owned_directory(self.vhost_root, 0o755)
        _ensure_owned_directory(self.ownership_root, 0o700)
        scope_path = self.ownership_root / _scope_name(plan.project, plan.environment)
        metadata_snapshot = _read_regular_file(scope_path, MAX_METADATA_BYTES, 0o600)
        previous = (
            _parse_ownership(metadata_snapshot, scope_path)
            if metadata_snapshot is not None
            else None
        )
        if previous is not None and (
            previous.project != plan.project or previous.environment != plan.environment
        ):
            raise NginxTransactionError(
                f"ownership metadata scope does not match filename: {scope_path}"
            )
        owners: dict[str, tuple[str, str]] = {}
        for metadata_path in sorted(self.ownership_root.glob("*.json")):
            content = _read_regular_file(metadata_path, MAX_METADATA_BYTES, 0o600)
            if content is None:
                continue
            record = _parse_ownership(content, metadata_path)
            if metadata_path.name != _scope_name(record.project, record.environment):
                raise NginxTransactionError(
                    f"ownership metadata scope does not match filename: {metadata_path}"
                )
            for name in record.fragments:
                if (
                    _read_regular_file(
                        self.vhost_root / name, MAX_FRAGMENT_BYTES, 0o644
                    )
                    is None
                ):
                    raise NginxTransactionError(
                        f"owned nginx fragment is missing: {name}"
                    )
                owner = owners.setdefault(name, (record.project, record.environment))
                if owner != (record.project, record.environment):
                    raise NginxTransactionError(
                        f"duplicate ownership for fragment: {name}"
                    )
        scope = (plan.project, plan.environment)
        for name in plan.fragments:
            owner = owners.get(name)
            if owner is not None and owner != scope:
                raise NginxTransactionError(
                    f"nginx fragment {name!r} is owned by {owner[0]}/{owner[1]}"
                )
            if owner is None and (self.vhost_root / name).exists():
                raise NginxTransactionError(
                    f"refusing to overwrite unmanaged nginx fragment: {name}"
                )
        affected = set(plan.fragments)
        if previous is not None:
            affected.update(previous.fragments)
        snapshots = {
            name: _read_regular_file(self.vhost_root / name, MAX_FRAGMENT_BYTES, 0o644)
            for name in affected
        }
        return NginxFragmentTransaction(
            self, plan, previous, snapshots, metadata_snapshot
        )

    def write_ownership(self, plan: NginxFragmentPlan) -> None:
        payload = {
            "api_version": OWNERSHIP_API_VERSION,
            "environment": plan.environment,
            "fragments": sorted(plan.fragments),
            "project": plan.project,
            "release_id": plan.release_id,
        }
        content = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
        _atomic_write(
            self.ownership_root / _scope_name(plan.project, plan.environment),
            content,
            0o600,
        )

    def restore_metadata(
        self, plan: NginxFragmentPlan, snapshot: Optional[bytes]
    ) -> None:
        path = self.ownership_root / _scope_name(plan.project, plan.environment)
        if snapshot is None:
            if path.exists():
                _unlink_file(path)
        else:
            _atomic_write(path, snapshot, 0o600)

    def render_config(self) -> bytes:
        # No -watch, -notify or destination: a fresh, synchronous Docker snapshot
        # goes to stdout. Never use the watcher's old default.conf as readiness.
        result = self._run_docker(
            "exec",
            self.docker_gen_container,
            "docker-gen",
            "-endpoint",
            "tcp://docker-socket-read:2375",
            "/app/nginx.tmpl",
        )
        content = result.stdout.encode("utf-8")
        if not content.strip() or len(content) > MAX_DEFAULT_CONFIG_BYTES:
            raise NginxTransactionError("docker-gen returned empty or oversized config")
        return content

    def regenerate_config(
        self, expected_hosts: frozenset[str], previous_hosts: frozenset[str]
    ) -> None:
        removed_hosts = previous_hosts - expected_hosts
        deadline = time.monotonic() + self.convergence_timeout
        while True:
            content = self.render_config()
            text = content.decode("utf-8")
            configured_hosts = _configured_server_names(text)
            expected_ready = expected_hosts <= configured_hosts
            removed_ready = configured_hosts.isdisjoint(removed_hosts)
            if expected_ready and removed_ready:
                _read_regular_file(self.default_config, MAX_DEFAULT_CONFIG_BYTES, 0o644)
                _atomic_write(self.default_config, content, 0o644)
                return
            if time.monotonic() >= deadline:
                raise NginxTransactionError(
                    "docker-gen did not converge to the candidate nginx hosts"
                )
            self.sleeper(self.convergence_interval)

    def _run_docker(self, *arguments: str) -> subprocess.CompletedProcess:
        command = [str(self.docker_executable), *arguments]
        try:
            result = self.runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise NginxTransactionError(f"cannot execute Docker: {exc}") from exc
        if result.returncode != 0:
            # Generator output and nginx diagnostics can contain user-supplied
            # directives. Keep them out of release ledger and shared CI logs.
            operation = (
                "generation"
                if self.docker_gen_container in arguments
                else (
                    "reload"
                    if arguments[-2:] == ("-s", "reload")
                    else "configuration test"
                )
            )
            raise NginxTransactionError(
                f"nginx {operation} failed with exit code {result.returncode}"
            )
        return result

    def _docker_exec(self, *arguments: str) -> None:
        self._run_docker("exec", self.nginx_container, *arguments)

    def test_configuration(self) -> None:
        self._docker_exec("nginx", "-t")

    def reload_nginx(self) -> None:
        self._docker_exec("nginx", "-s", "reload")
