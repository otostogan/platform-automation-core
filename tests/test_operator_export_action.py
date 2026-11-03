"""The console's "Exports: send one now", driven with a fake host and fake prompts."""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from platform_automation.operator import console
from platform_automation.operator.remote import RemoteResult


class Answer:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value


class FakeQuestionary:
    def __init__(self, answers):
        self.answers = list(answers)
        self.choices = []

    class Choice:
        def __init__(self, title, value):
            self.title, self.value = title, value

    def select(self, message, choices, **kwargs):
        self.choices.append([c.title for c in choices])
        return Answer(self.answers.pop(0))


class ExportActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = SimpleNamespace(
            target_host="platform-host-1.tailnet.example.invalid", core_pin=None
        )
        self.scope = SimpleNamespace(project="app", environment="production")
        self.calls = []
        self.listing = {
            "exports": [
                {
                    "name": "clients",
                    "schedule": "Mon 06:00",
                    "to": ["sales@example.invalid"],
                    "declared": True,
                    "last": None,
                },
                {"name": "old", "declared": False, "last": {"outcome": "failed"}},
            ]
        }

    def remote(self, host, user, arguments, **kwargs):
        self.calls.append(arguments)
        if arguments[0] == "exports":
            return RemoteResult(True, 0, self.listing, None, "ssh …")
        return RemoteResult(
            True,
            0,
            {
                "name": arguments[-1],
                "attachment": "clients-20260105.csv",
                "rows": 12,
                "to": ["sales@example.invalid"],
                "release_tag": "production-v1",
            },
            None,
            "ssh …",
        )

    def drive(self, answers):
        questionary = FakeQuestionary(answers)
        action = console.export_action(
            self.context, self.scope, (questionary, None), remote=self.remote
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = action.run()
        return code, output.getvalue(), questionary

    def test_only_declared_jobs_are_offered_and_the_chosen_one_is_sent(self) -> None:
        code, shown, questionary = self.drive([self.listing["exports"][0]])

        self.assertEqual(code, 0, shown)
        self.assertEqual(len(questionary.choices[0]), 1)
        self.assertIn("clients", questionary.choices[0][0])
        self.assertEqual(
            self.calls[1],
            [
                "export",
                "--project",
                "app",
                "--environment",
                "production",
                "--name",
                "clients",
            ],
        )
        self.assertIn("export sent", shown)
        self.assertIn("12 rows", shown)

    def test_nothing_declared_explains_what_to_add(self) -> None:
        self.listing = {"exports": []}
        code, shown, _ = self.drive([])
        self.assertEqual(code, 1)
        self.assertIn("declares no exports", shown)
        self.assertEqual(len(self.calls), 1)

    def test_the_action_is_grouped_with_backups(self) -> None:
        self.assertEqual(
            console.group_of("Exports: send one now"), "Database & backups"
        )


if __name__ == "__main__":
    unittest.main()
