"""The console's one-off command action, driven end to end with fake prompts.

The prompt helpers have signatures of their own; an action that calls one
wrongly fails only when a person reaches it. This drives the whole action.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from platform_automation.operator import console


class Answer:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value


class FakeQuestionary:
    """Answers in order; records what was asked."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    class Choice:
        def __init__(self, title, value):
            self.title, self.value = title, value

    def _next(self, kind, message):
        self.asked.append((kind, message))
        return Answer(self.answers.pop(0))

    def text(self, message, **kwargs):
        return self._next("text", message)

    def select(self, message, choices, **kwargs):
        self.asked.append(("select-choices", [c.value for c in choices]))
        return self._next("select", message)

    def confirm(self, message, **kwargs):
        return self._next("confirm", message)


class ExecActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / "deploy").mkdir()
        (root / "deploy" / "platform.lab.yml").write_text(
            "service:\n    web: web\ncompose_file: deploy/compose.yml\n"
        )
        (root / "deploy" / "compose.yml").write_text(
            "services:\n    worker: {}\n    web: {}\n"
        )
        self.context = SimpleNamespace(root=root, target_host="host.example.test")
        self.scope = SimpleNamespace(
            project="example",
            environment="lab",
            manifest=Path("deploy/platform.lab.yml"),
        )
        self.running = SimpleNamespace(
            available=True, backend_state="Running", error=None
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_asks_service_and_command_then_runs_the_host_line_with_a_tty(self) -> None:
        questionary = FakeQuestionary(["web", "node dist/task.js 10000", True])
        calls = []
        with (
            mock.patch.object(console, "read_tailnet", return_value=self.running),
            mock.patch.object(
                console.subprocess,
                "call",
                side_effect=lambda command: calls.append(command) or 0,
            ),
        ):
            code = console.exec_action(
                self.context, self.scope, (questionary, None)
            ).run()

        self.assertEqual(code, 0)
        kind, choices = questionary.asked[0]
        self.assertEqual((kind, choices[:2]), ("select-choices", ["web", "worker"]))
        self.assertEqual(len(calls), 1)
        command = calls[0]
        self.assertIn("-t", command)
        self.assertEqual(command[-2], "ops@host.example.test")
        self.assertTrue(
            command[-1].endswith(
                "--project example --environment lab --service web -- node dist/task.js 10000"
            )
        )

    def test_a_shell_operator_is_refused_before_anything_runs(self) -> None:
        questionary = FakeQuestionary(["web", "node a.js | tee out"])
        with (
            mock.patch.object(console, "read_tailnet", return_value=self.running),
            mock.patch.object(console.subprocess, "call") as call,
        ):
            code = console.exec_action(
                self.context, self.scope, (questionary, None)
            ).run()
        self.assertEqual(code, 1)
        call.assert_not_called()

    def test_declining_runs_nothing(self) -> None:
        questionary = FakeQuestionary(["web", "true", False])
        with (
            mock.patch.object(console, "read_tailnet", return_value=self.running),
            mock.patch.object(console.subprocess, "call") as call,
        ):
            code = console.exec_action(
                self.context, self.scope, (questionary, None)
            ).run()
        self.assertEqual(code, 130)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
