import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from platform_automation.database_session import (
    DatabaseSessionError,
    close_session,
    container_address,
    open_session,
)


class Runner:
    """docker inspect, sops decrypt and psql, without the binaries."""

    def __init__(self) -> None:
        self.calls = []
        self.inputs = []
        self.address = "172.20.0.5"

    def __call__(self, command, **options):
        self.calls.append(list(command))
        self.inputs.append(options.get("input"))
        if "decrypt" in command:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"password": "app-secret"}).encode(), b""
            )
        if "inspect" in command:
            return subprocess.CompletedProcess(
                command, 0, (self.address + "\n").encode(), b""
            )
        if "psql" in command:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        raise AssertionError(command)


class DatabaseSessionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.databases = root / "databases"
        (self.databases / "example/lab").mkdir(parents=True)
        (self.databases / "example/lab/credentials.sops.json").write_text("{}")
        (root / "age.key").write_text("synthetic private half\n")
        (root / "age.key").chmod(0o600)
        self.runner = Runner()
        self.common = dict(
            databases_root=self.databases,
            age_key_file=root / "age.key",
            sops_executable=root / "sops",
            docker_executable=Path("/usr/bin/docker"),
            runner=self.runner,
        )

    def test_open_creates_a_member_of_app_that_expires_and_never_puts_secrets_in_argv(
        self,
    ) -> None:
        session = open_session("example", "lab", 30, **self.common)

        self.assertRegex(session["user"], r"^tunnel_[0-9a-f]{8}$")
        self.assertEqual(
            (session["database"], session["port"], session["address"]),
            ("app", 5432, "172.20.0.5"),
        )
        self.assertTrue(session["expires_at"].endswith("Z"))
        psql_call = next(c for c in self.runner.calls if "psql" in c)
        script = next(i for i in self.runner.inputs if i).decode()
        self.assertIn(
            f"CREATE ROLE {session['user']} WITH LOGIN INHERIT IN ROLE app PASSWORD :'p' VALID UNTIL :'until'",
            script,
        )
        self.assertIn("DROP ROLE %I", script, "expired session roles are swept on open")
        self.assertNotIn(session["password"], " ".join(psql_call))
        self.assertIn("PGPASSWORD=app-secret", " ".join(psql_call))
        self.assertIn(session["user"], session["close_with"])

    def test_close_terminates_and_drops_only_a_session_role(self) -> None:
        with self.assertRaises(DatabaseSessionError):
            close_session("example", "lab", "app", **self.common)
        with self.assertRaises(DatabaseSessionError):
            close_session("example", "lab", "tunnel_zz; DROP", **self.common)

        result = close_session("example", "lab", "tunnel_0123abcd", **self.common)

        self.assertEqual(result["closed"], "tunnel_0123abcd")
        script = next(i for i in self.runner.inputs if i).decode()
        self.assertIn("pg_terminate_backend", script)
        self.assertIn("DROP ROLE IF EXISTS tunnel_0123abcd;", script)

    def test_minutes_are_bounded_and_a_stopped_container_is_named(self) -> None:
        with self.assertRaises(DatabaseSessionError):
            open_session("example", "lab", 0, **self.common)
        with self.assertRaises(DatabaseSessionError):
            open_session("example", "lab", 999, **self.common)
        self.runner.address = ""
        with self.assertRaisesRegex(DatabaseSessionError, "not running"):
            container_address("example", "lab", Path("/usr/bin/docker"), self.runner)
