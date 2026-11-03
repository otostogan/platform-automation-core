"""``platform exec --stdin``: the input is bounded and the flag is known."""

import io
import unittest

from platform_automation import platform_cli
from platform_automation.compose_runtime import ComposeRuntimeError


class ExecStdinTest(unittest.TestCase):
    def test_the_parser_knows_the_flag_and_keeps_the_command_after_it(self) -> None:
        parsed = platform_cli.parse_arguments(
            [
                "exec",
                "--project",
                "p",
                "--environment",
                "lab",
                "--stdin",
                "--",
                "node",
                "dist/x.js",
            ]
        )
        self.assertTrue(parsed.stdin)
        self.assertEqual(
            parsed.argv,
            (
                ["--", "node", "dist/x.js"]
                if parsed.argv[0] == "--"
                else ["node", "dist/x.js"]
            ),
        )
        self.assertFalse(
            platform_cli.parse_arguments(
                ["exec", "--project", "p", "--environment", "lab", "--", "true"]
            ).stdin
        )

    def test_input_is_read_whole_up_to_the_limit(self) -> None:
        self.assertEqual(
            platform_cli.read_exec_input(io.BytesIO(b"abc"), limit=8), b"abc"
        )
        self.assertEqual(
            platform_cli.read_exec_input(io.BytesIO(b"12345678"), limit=8), b"12345678"
        )

    def test_more_than_the_limit_is_refused_before_anything_runs(self) -> None:
        with self.assertRaisesRegex(ComposeRuntimeError, "exceeds"):
            platform_cli.read_exec_input(io.BytesIO(b"123456789"), limit=8)

    def test_the_default_limit_is_one_mebibyte(self) -> None:
        self.assertEqual(platform_cli.MAX_EXEC_STDIN_BYTES, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
