import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path


from platform_automation.platform_cli import parse_arguments  # noqa: E402


class RollbackArgumentsTest(unittest.TestCase):
    def test_parses_rollback_target(self):
        arguments = parse_arguments(
            [
                "rollback",
                "--project",
                "example",
                "--environment",
                "lab",
                "--to",
                "runtime-lab-2",
                "--json",
            ]
        )

        self.assertEqual(arguments.command, "rollback")
        self.assertEqual(arguments.project, "example")
        self.assertEqual(arguments.environment, "lab")
        self.assertEqual(arguments.target_tag, "runtime-lab-2")
        self.assertTrue(arguments.json)
        self.assertFalse(arguments.registry_token_stdin)
        self.assertIsNone(arguments.registry_username)

    def test_requires_target_tag(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                parse_arguments(
                    [
                        "rollback",
                        "--project",
                        "example",
                        "--environment",
                        "lab",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)

    def test_parses_registry_options(self):
        arguments = parse_arguments(
            [
                "rollback",
                "--project",
                "example",
                "--environment",
                "lab",
                "--to",
                "v1",
                "--registry-username",
                "otostogan",
                "--registry-token-stdin",
            ]
        )

        self.assertEqual(arguments.registry_username, "otostogan")
        self.assertTrue(arguments.registry_token_stdin)
