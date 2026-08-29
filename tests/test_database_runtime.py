import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from platform_automation.database_runtime import (  # noqa: E402
    DatabaseRuntimeError,
    build_database_compose,
    database_resource_name,
    database_url,
    ensure_project_database,
    inject_database_url,
    resolve_postgres_image,
    restore_database_environment,
)

PINNED_IMAGE = "postgres:17@sha256:" + ("d" * 64)
RECIPIENTS = {
    "age1hostexample",
    "age1recoveryexample",
}


def secrets_document(recipients=RECIPIENTS) -> dict:
    return {
        "sops": {
            "age": [
                {"recipient": recipient, "enc": "ENC[data]"}
                for recipient in sorted(recipients)
            ],
            "mac": "ENC[mac]",
            "version": "3.8.1",
        },
    }


def docker_manifest() -> dict:
    return {
        "database": {
            "mode": "docker",
            "postgres_major": 17,
            "backup_enabled": False,
        },
    }


class FakeRunner:
    """Answer docker and sops invocations without either binary."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_actions: set[str] = set()

    def action(self, command: list[str]) -> str:
        joined = " ".join(command)

        if " encrypt " in f" {joined} ":
            return "encrypt"
        if "decrypt" in command:
            return "decrypt"
        if "pull" in command:
            return "pull"
        if "inspect" in command:
            return "inspect"
        if "up" in command:
            return "up"

        raise AssertionError(f"unexpected command: {command}")

    def __call__(self, command, **options):
        self.calls.append(list(command))
        action = self.action(command)
        returncode = 1 if action in self.fail_actions else 0
        stdout = b""

        if action == "encrypt":
            plaintext = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
            stored = dict(secrets_document())
            stored["ciphertext"] = "ENC[" + plaintext["password"] + "]"
            stdout = json.dumps(stored).encode("utf-8")
        elif action == "decrypt":
            encrypted = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
            password = encrypted["ciphertext"][4:-1]
            stdout = json.dumps({"password": password}).encode("utf-8")
        elif action == "inspect":
            stdout = f"postgres@sha256:{'e' * 64}\n".encode("utf-8")

        return subprocess.CompletedProcess(
            args=command,
            returncode=returncode,
            stdout=stdout,
            stderr=b"",
        )


class DatabaseComposeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.compose = build_database_compose(
            "example",
            "lab",
            PINNED_IMAGE,
            Path("/run/platform/secrets/example/lab/database.env"),
            postgres_major=17,
        )
        self.service = self.compose["services"]["postgres"]

    def test_database_is_never_published(self) -> None:
        self.assertNotIn("ports", self.service)
        self.assertNotIn("network_mode", self.service)

    def test_resources_share_one_deterministic_name(self) -> None:
        name = database_resource_name("example", "lab")

        self.assertEqual(name, "platform-db-example-lab")
        self.assertEqual(self.compose["name"], name)
        self.assertEqual(self.compose["networks"]["db"]["name"], name)
        self.assertEqual(self.compose["volumes"]["data"]["name"], name)

    def test_container_restarts_without_recovery_logic(self) -> None:
        self.assertEqual(self.service["restart"], "unless-stopped")

    def test_healthcheck_talks_to_the_declared_database(self) -> None:
        self.assertIn("pg_isready", self.service["healthcheck"]["test"])

    def test_network_is_internal(self) -> None:
        """No published ports stops inbound; internal stops outbound too."""
        self.assertIs(self.compose["networks"]["db"]["internal"], True)

    def test_pre_18_cluster_mounts_the_data_directory(self) -> None:
        self.assertEqual(
            self.service["volumes"],
            ["data:/var/lib/postgresql/data"],
        )

    def test_postgres_18_mounts_the_image_data_root(self) -> None:
        compose = build_database_compose(
            "example",
            "lab",
            "postgres:18@sha256:" + ("d" * 64),
            Path("/run/platform/secrets/example/lab/database.env"),
            postgres_major=18,
        )

        self.assertEqual(
            compose["services"]["postgres"]["volumes"],
            ["data:/var/lib/postgresql"],
        )

    def test_compose_round_trips_through_yaml(self) -> None:
        self.assertEqual(
            yaml.safe_load(yaml.safe_dump(self.compose)),
            self.compose,
        )


class ResolvePostgresImageTest(unittest.TestCase):
    def test_existing_pin_for_the_same_major_is_kept(self) -> None:
        runner = FakeRunner()
        existing = {"services": {"postgres": {"image": PINNED_IMAGE}}}

        image = resolve_postgres_image(17, existing, Path("docker"), runner)

        self.assertEqual(image, PINNED_IMAGE)
        self.assertEqual(runner.calls, [])

    def test_major_change_resolves_a_fresh_digest(self) -> None:
        runner = FakeRunner()
        existing = {"services": {"postgres": {"image": PINNED_IMAGE}}}

        image = resolve_postgres_image(18, existing, Path("docker"), runner)

        self.assertEqual(image, f"postgres:18@sha256:{'e' * 64}")
        self.assertEqual(
            [runner.action(call) for call in runner.calls],
            ["pull", "inspect"],
        )

    def test_unsupported_major_is_refused(self) -> None:
        with self.assertRaises(DatabaseRuntimeError):
            resolve_postgres_image(15, None, Path("docker"), FakeRunner())


class InjectDatabaseUrlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.env_path = Path(self.temporary_directory.name) / "app.env"
        self.env_path.write_text('APP_SECRET="x"\n', encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_external_database_injects_nothing(self) -> None:
        inject_database_url(self.env_path, None)

        self.assertEqual(
            self.env_path.read_text(encoding="utf-8"),
            'APP_SECRET="x"\n',
        )

    def test_platform_database_appends_its_url(self) -> None:
        inject_database_url(self.env_path, "pw-123")

        self.assertIn(
            f'DATABASE_URL="{database_url("pw-123")}"',
            self.env_path.read_text(encoding="utf-8"),
        )

    def test_application_supplied_database_url_is_a_contradiction(self) -> None:
        self.env_path.write_text(
            'DATABASE_URL="postgresql://other"\n',
            encoding="utf-8",
        )

        with self.assertRaises(DatabaseRuntimeError):
            inject_database_url(self.env_path, "pw-123")


class EnsureProjectDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(self.temporary_directory.name)
        self.databases_root = base / "databases"
        self.runtime_root = base / "runtime"
        self.runtime_root.mkdir(mode=0o700)
        self.age_key_file = base / "age.key"
        self.age_key_file.write_text("AGE-SECRET-KEY-TEST\n", encoding="utf-8")
        self.runner = FakeRunner()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def ensure(self, manifest=None, secrets=None) -> str:
        return ensure_project_database(
            manifest=manifest or docker_manifest(),
            project="example",
            environment="lab",
            secrets_document=(secrets_document() if secrets is None else secrets),
            databases_root=self.databases_root,
            runtime_secrets_root=self.runtime_root,
            age_key_file=self.age_key_file,
            sops_executable=Path("sops"),
            docker_executable=Path("docker"),
            runner=self.runner,
        )

    def test_external_mode_owns_nothing(self) -> None:
        manifest = {"database": {"mode": "external"}}

        self.assertIsNone(self.ensure(manifest=manifest))
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.databases_root.exists())

    def test_first_deploy_creates_everything_and_starts_the_database(
        self,
    ) -> None:
        password = self.ensure()

        directory = self.databases_root / "example" / "lab"
        compose = yaml.safe_load(
            (directory / "compose.yml").read_text(encoding="utf-8")
        )

        self.assertTrue(password)
        self.assertTrue((directory / "credentials.sops.json").is_file())
        self.assertEqual(
            compose["services"]["postgres"]["image"],
            f"postgres:17@sha256:{'e' * 64}",
        )
        self.assertEqual(
            [self.runner.action(call) for call in self.runner.calls],
            ["encrypt", "pull", "inspect", "up"],
        )

        env_content = (
            self.runtime_root / "example" / "lab" / "database.env"
        ).read_text(encoding="utf-8")
        self.assertIn(f"POSTGRES_PASSWORD={password}", env_content)

    def test_second_deploy_reuses_password_and_image_pin(self) -> None:
        first = self.ensure()
        self.runner.calls.clear()

        second = self.ensure()

        self.assertEqual(first, second)
        self.assertEqual(
            [self.runner.action(call) for call in self.runner.calls],
            ["decrypt", "up"],
        )

    def test_recipient_change_re_envelopes_without_rotating(self) -> None:
        first = self.ensure()
        self.runner.calls.clear()

        widened = RECIPIENTS | {"age1escrowexample"}
        second = self.ensure(secrets=secrets_document(widened))

        self.assertEqual(first, second)
        self.assertIn(
            "encrypt",
            [self.runner.action(call) for call in self.runner.calls],
        )

    def test_unhealthy_database_fails_the_deploy(self) -> None:
        self.runner.fail_actions.add("up")

        with self.assertRaises(DatabaseRuntimeError):
            self.ensure()

    def test_secrets_without_recipients_are_refused(self) -> None:
        with self.assertRaises(DatabaseRuntimeError):
            self.ensure(secrets={})

    def test_files_are_private(self) -> None:
        self.ensure()
        directory = self.databases_root / "example" / "lab"

        for name in ("compose.yml", "credentials.sops.json"):
            mode = (directory / name).stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, name)


class RestoreDatabaseEnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(self.temporary_directory.name)
        self.databases_root = base / "databases"
        self.runtime_root = base / "runtime"
        self.runtime_root.mkdir(mode=0o700)
        self.age_key_file = base / "age.key"
        self.age_key_file.write_text("AGE-SECRET-KEY-TEST\n", encoding="utf-8")
        self.env_path = base / "app.env"
        self.env_path.write_text('APP_SECRET="x"\n', encoding="utf-8")
        self.runner = FakeRunner()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def seed_credentials(self) -> None:
        ensure_project_database(
            manifest=docker_manifest(),
            project="example",
            environment="lab",
            secrets_document=secrets_document(),
            databases_root=self.databases_root,
            runtime_secrets_root=self.runtime_root,
            age_key_file=self.age_key_file,
            sops_executable=Path("sops"),
            docker_executable=Path("docker"),
            runner=self.runner,
        )

    def test_restores_tmpfs_material_without_docker(self) -> None:
        self.seed_credentials()

        database_env = self.runtime_root / "example" / "lab" / "database.env"
        database_env.unlink()

        restore_database_environment(
            manifest=docker_manifest(),
            project="example",
            environment="lab",
            runtime_secrets_path=self.env_path,
            databases_root=self.databases_root,
            runtime_secrets_root=self.runtime_root,
            age_key_file=self.age_key_file,
            sops_executable=Path("sops"),
            runner=self.runner,
        )

        self.assertTrue(database_env.is_file())
        self.assertIn(
            "DATABASE_URL=",
            self.env_path.read_text(encoding="utf-8"),
        )

    def test_external_mode_restores_nothing(self) -> None:
        restore_database_environment(
            manifest={"database": {"mode": "external"}},
            project="example",
            environment="lab",
            runtime_secrets_path=self.env_path,
            databases_root=self.databases_root,
            runtime_secrets_root=self.runtime_root,
            age_key_file=self.age_key_file,
            sops_executable=Path("sops"),
        )

        self.assertEqual(
            self.env_path.read_text(encoding="utf-8"),
            'APP_SECRET="x"\n',
        )

    def test_missing_credential_is_loud(self) -> None:
        with self.assertRaises(DatabaseRuntimeError):
            restore_database_environment(
                manifest=docker_manifest(),
                project="example",
                environment="lab",
                runtime_secrets_path=self.env_path,
                databases_root=self.databases_root,
                runtime_secrets_root=self.runtime_root,
                age_key_file=self.age_key_file,
                sops_executable=Path("sops"),
            )


if __name__ == "__main__":
    unittest.main()
