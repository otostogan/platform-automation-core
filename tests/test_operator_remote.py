import subprocess
import unittest
from pathlib import Path

from platform_automation.operator.remote import build_command, host_error, run_platform


def fake_runner(returncode=0, stdout=b"", stderr=b""):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    runner.calls = calls
    return runner


class BuildCommandTest(unittest.TestCase):
    def test_remote_side_is_one_quoted_platform_invocation(self) -> None:
        command = build_command(
            "platform-host-1",
            "ops",
            ["status", "--project", "my-app", "--environment", "lab"],
        )

        self.assertEqual(command[0], "ssh")
        self.assertIn("BatchMode=yes", command)
        self.assertEqual(command[-3], "ops@platform-host-1")
        self.assertEqual(command[-2], "--")
        self.assertEqual(
            command[-1],
            "sudo -n platform status --project my-app --environment lab --json",
        )

    def test_identity_is_passed_to_ssh(self) -> None:
        command = build_command("h", "ops", ["status"], identity=Path("/keys/h-ops"))

        self.assertIn("-i", command)
        self.assertEqual(command[command.index("-i") + 1], "/keys/h-ops")


class RunPlatformTest(unittest.TestCase):
    def test_json_answer_is_returned_as_a_document(self) -> None:
        runner = fake_runner(stdout=b'{"project": "my-app", "current": null}')

        result = run_platform("h", "ops", ["status"], runner=runner)

        self.assertTrue(result.ok)
        self.assertEqual(result.document["project"], "my-app")

    def test_host_refusal_is_kept_verbatim(self) -> None:
        runner = fake_runner(
            returncode=1,
            stderr=b"restore error: unfinished deployment requires operator review\n",
        )

        result = run_platform("h", "ops", ["restore"], runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, 1)
        self.assertEqual(
            result.error,
            "restore error: unfinished deployment requires operator review",
        )

    def test_non_json_success_is_an_error(self) -> None:
        runner = fake_runner(stdout=b"Platform status: my-app/lab\n")

        result = run_platform("h", "ops", ["status"], runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "host did not answer with JSON")

    def test_ssh_failure_is_reported_not_raised(self) -> None:
        def runner(command, **kwargs):
            raise OSError("no ssh")

        result = run_platform("h", "ops", ["status"], runner=runner)

        self.assertFalse(result.ok)
        self.assertEqual(result.code, -1)
        self.assertIn("no ssh", result.error)

    def test_last_error_line_wins_over_ssh_noise(self) -> None:
        self.assertEqual(
            host_error("Warning: Permanently added\nbackup error: dump failed\n"),
            "backup error: dump failed",
        )


if __name__ == "__main__":
    unittest.main()
