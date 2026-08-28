import io
import subprocess
import tempfile
import unittest
from pathlib import Path


from platform_automation.registry_pull import (  # noqa: E402
    RegistryPullError,
    pull_immutable_image,
    read_registry_token,
)


IMAGE = "ghcr.io/example/platform-example@sha256:" + ("a" * 64)


class RecordingRunner:
    def __init__(self, return_codes: list[int] = None) -> None:
        self.calls: list[dict] = []
        self.return_codes = list(return_codes or [])

    def __call__(self, command: list[str], **options):
        self.calls.append(
            {
                "command": command,
                "options": options,
            }
        )
        return_code = self.return_codes.pop(0) if self.return_codes else 0
        return subprocess.CompletedProcess(
            args=command,
            returncode=return_code,
            stdout=b"",
            stderr=b"registry output must stay private",
        )


class RegistryPullTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temporary_directory.name) / "registry"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_reads_token_from_stdin(self) -> None:
        token = read_registry_token(io.BytesIO(b"temporary-token\n"))

        self.assertEqual(token, b"temporary-token")

    def test_authenticated_pull_uses_temporary_config(self) -> None:
        runner = RecordingRunner()
        token = b"temporary-secret-token"

        pull_immutable_image(
            IMAGE,
            registry_username="github-actions",
            registry_token=token,
            runtime_root=self.runtime_root,
            docker_executable=Path("/fake/docker"),
            runner=runner,
        )

        self.assertEqual(len(runner.calls), 2)
        login = runner.calls[0]
        pull = runner.calls[1]

        self.assertIn("login", login["command"])
        self.assertIn("--password-stdin", login["command"])
        self.assertEqual(login["options"]["input"], token)
        self.assertNotIn(token.decode("utf-8"), " ".join(login["command"]))
        self.assertIn("pull", pull["command"])
        self.assertIn(IMAGE, pull["command"])

        docker_config = Path(login["command"][2])
        self.assertFalse(docker_config.exists())

    def test_anonymous_pull_skips_login(self) -> None:
        runner = RecordingRunner()

        pull_immutable_image(
            IMAGE,
            runtime_root=self.runtime_root,
            docker_executable=Path("/fake/docker"),
            runner=runner,
        )

        self.assertEqual(len(runner.calls), 1)
        self.assertIn("pull", runner.calls[0]["command"])

    def test_rejects_partial_credentials(self) -> None:
        with self.assertRaisesRegex(
            RegistryPullError,
            "must be provided together",
        ):
            pull_immutable_image(
                IMAGE,
                registry_username="github-actions",
                runtime_root=self.runtime_root,
            )

    def test_hides_docker_error_output(self) -> None:
        runner = RecordingRunner(return_codes=[1])

        with self.assertRaisesRegex(
            RegistryPullError,
            "^Docker registry image pull failed$",
        ) as context:
            pull_immutable_image(
                IMAGE,
                runtime_root=self.runtime_root,
                docker_executable=Path("/fake/docker"),
                runner=runner,
            )

        self.assertNotIn(
            "registry output",
            str(context.exception),
        )

    def test_distinguishes_login_failure_from_pull_failure(self) -> None:
        runner = RecordingRunner(return_codes=[1])

        with self.assertRaisesRegex(
            RegistryPullError,
            "^Docker registry login failed$",
        ):
            pull_immutable_image(
                IMAGE,
                registry_username="github-actions",
                registry_token=b"temporary-token",
                runtime_root=self.runtime_root,
                docker_executable=Path("/fake/docker"),
                runner=runner,
            )


if __name__ == "__main__":
    unittest.main()
