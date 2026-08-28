import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


EXAMPLE_MANIFEST = (
    Path(__file__).parent / "fixtures" / "app-contract" / "deploy" / "platform.yml"
)


from platform_automation.build_bundle import create_bundle  # noqa: E402
from platform_automation.compose_runtime import (  # noqa: E402
    ComposeRuntimeError,
    build_compose_environment,
    load_staged_manifest,
    run_release_migration,
    start_release,
    validate_release_compose,
)
from platform_automation.deployment_request import load_deployment_request  # noqa: E402
from platform_automation.stage_bundle import stage_verified_bundle  # noqa: E402


IMAGE = "ghcr.io/example/platform-example@sha256:" + ("a" * 64)


class RecordingRunner:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[dict] = []
        self.stdout = json.dumps(
            [
                {"Service": "app", "State": "running", "Health": "healthy"},
                {"Service": "worker", "State": "running", "Health": ""},
            ]
        )

    def __call__(self, command: list[str], **kwargs):
        self.calls.append({"command": command, **kwargs})
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout)


class ComposeRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.bundle_path = self.base / "bundle.tar.gz"
        self.releases_root = self.base / "releases"
        self.runtime_secrets_path = self.base / "runtime" / "app.env"
        self.runtime_secrets_path.parent.mkdir(parents=True)
        self.runtime_secrets_path.write_text(
            'APP_SECRET="test-only"\n',
            encoding="utf-8",
        )
        self.runtime_secrets_path.chmod(0o600)

        create_bundle(EXAMPLE_MANIFEST, self.bundle_path)
        self.request = load_deployment_request(
            bundle_path=self.bundle_path,
            project="example",
            environment="lab",
            image=IMAGE,
            release_tag="v1",
        )
        self.staged_bundle = stage_verified_bundle(
            self.request.bundle,
            self.releases_root,
        )
        self.runner = RecordingRunner()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def runtime_arguments(self) -> dict:
        return {
            "manifest": self.request.bundle.manifest,
            "staged_bundle_path": self.staged_bundle,
            "image": IMAGE,
            "runtime_secrets_path": self.runtime_secrets_path,
            "docker_executable": Path("/usr/bin/docker"),
            "runner": self.runner,
        }

    def test_builds_runtime_environment_without_secret_values(self) -> None:
        environment = build_compose_environment(
            self.request.bundle.manifest,
            IMAGE,
            self.runtime_secrets_path,
            base_environment={},
        )

        self.assertEqual(environment["PLATFORM_IMAGE"], IMAGE)
        self.assertEqual(environment["PLATFORM_COMPOSE_PROJECT_NAME"], "example-lab")
        self.assertEqual(environment["PLATFORM_VIRTUAL_HOSTS"], "app.example.invalid")
        self.assertEqual(environment["PLATFORM_INTERNAL_PORT"], "3000")
        self.assertEqual(environment["PLATFORM_TLS_HOSTS"], "")
        self.assertNotIn("test-only", str(environment))

    def test_validates_compose_before_deployment(self) -> None:
        validate_release_compose(**self.runtime_arguments())

        command = self.runner.calls[0]["command"]
        self.assertEqual(command[-2:], ["config", "--quiet"])
        self.assertIn(str(self.staged_bundle / "deploy" / "compose.yml"), command)

    def test_runs_manifest_migration_command(self) -> None:
        run_release_migration(**self.runtime_arguments())

        self.assertEqual(
            self.runner.calls[0]["command"][-7:],
            [
                "run",
                "--rm",
                "--entrypoint",
                "",
                "app",
                "bin/app",
                "migrate",
            ],
        )

    def test_starts_and_waits_for_healthy_services(self) -> None:
        start_release(
            **self.runtime_arguments(),
            sleeper=lambda seconds: None,
        )

        command = self.runner.calls[0]["command"]
        self.assertEqual(
            command[-6:],
            [
                "up",
                "--detach",
                "--remove-orphans",
                "--wait",
                "--wait-timeout",
                "90",
            ],
        )
        self.assertEqual(len(self.runner.calls), 4)
        self.assertTrue(
            all(
                call["command"][-4:] == ["ps", "--all", "--format", "json"]
                for call in self.runner.calls[1:]
            )
        )

    def test_rejects_service_restart_loop_after_compose_wait(self) -> None:
        self.runner.stdout = json.dumps(
            [
                {"Service": "app", "State": "running", "Health": "healthy"},
                {"Service": "worker", "State": "restarting", "Health": ""},
            ]
        )

        with self.assertRaisesRegex(
            ComposeRuntimeError,
            "Docker Compose service is not running: worker",
        ):
            start_release(
                **self.runtime_arguments(),
                sleeper=lambda seconds: None,
            )

    def test_reports_failed_healthcheck_without_command_output(self) -> None:
        self.runner.returncode = 1

        with self.assertRaisesRegex(
            ComposeRuntimeError,
            "application healthcheck failed with exit code 1",
        ):
            start_release(**self.runtime_arguments())

    def test_loads_manifest_from_verified_staged_bundle(self) -> None:
        manifest = load_staged_manifest(self.staged_bundle)

        self.assertEqual(manifest["project"], "example")
        self.assertEqual(manifest["environment"], "lab")


if __name__ == "__main__":
    unittest.main()
