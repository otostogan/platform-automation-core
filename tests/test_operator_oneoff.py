import tempfile
import unittest
from pathlib import Path

from platform_automation.operator.oneoff import (
    OneOffError,
    compose_services,
    remote_command,
    split_command,
)


class SplitCommandTest(unittest.TestCase):
    def test_words_only(self) -> None:
        self.assertEqual(
            split_command("node dist/generate-quarter.js 10000"),
            ["node", "dist/generate-quarter.js", "10000"],
        )
        self.assertEqual(
            split_command("node -e 'console.log(1)'"), ["node", "-e", "console.log(1)"]
        )

    def test_shell_operators_are_refused_not_passed_along(self) -> None:
        for text in ("node a.js | tee out", "a && b", "node a.js > /tmp/x", ""):
            with self.subTest(text=text):
                with self.assertRaises(OneOffError):
                    split_command(text)


class RemoteCommandTest(unittest.TestCase):
    def test_the_host_line_is_quoted_for_a_shell_and_ends_with_the_argv(self) -> None:
        line = remote_command("example", "lab", "web", ["node", "a.js", "it's"])
        self.assertTrue(
            line.startswith(
                "sudo -n platform exec --project example --environment lab --service web -- "
            )
        )
        self.assertTrue(line.endswith("node a.js 'it'\"'\"'s'"))


class ComposeServicesTest(unittest.TestCase):
    def test_web_first_then_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "deploy").mkdir()
            (root / "deploy" / "platform.lab.yml").write_text(
                "service:\n    web: web\ncompose_file: deploy/compose.yml\n"
            )
            (root / "deploy" / "compose.yml").write_text(
                "services:\n    mailpit: {}\n    worker: {}\n    web: {}\n"
            )
            self.assertEqual(
                compose_services(root, Path("deploy/platform.lab.yml")),
                ["web", "mailpit", "worker"],
            )


if __name__ == "__main__":
    unittest.main()
