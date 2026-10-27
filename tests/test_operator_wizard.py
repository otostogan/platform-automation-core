import unittest
from pathlib import Path

from platform_automation.operator.wizard import (
    BACK,
    CANCEL,
    Cancelled,
    Step,
    is_back,
    run_wizard,
)


def scripted(*answers):
    """A step that returns the scripted answers in order, then repeats the last."""
    queue = list(answers)

    def ask(state):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return ask


class RunWizardTest(unittest.TestCase):
    def test_back_returns_to_the_previous_step_keeping_later_defaults(self) -> None:
        asked = []

        def recording(key, *answers):
            inner = scripted(*answers)

            def ask(state):
                asked.append(key)
                return inner(state)

            return ask

        steps = [
            Step("a", recording("a", 1, 10)),
            Step("b", recording("b", BACK, 2)),
            Step("c", recording("c", 3)),
        ]

        state = run_wizard(steps)

        self.assertEqual(asked, ["a", "b", "a", "b", "c"])
        self.assertEqual(state, {"a": 10, "b": 2, "c": 3})

    def test_back_skips_steps_that_do_not_apply(self) -> None:
        asked = []
        steps = [
            Step("mode", lambda s: (asked.append("mode"), "external")[1]),
            Step(
                "interval",
                lambda s: (asked.append("interval"), 15)[1],
                lambda s: s.get("mode") == "docker",
            ),
            Step("query", scripted(BACK, "SELECT 1")),
        ]

        run_wizard(steps)

        self.assertEqual(asked, ["mode", "mode"])

    def test_back_on_the_first_step_asks_it_again(self) -> None:
        steps = [Step("a", scripted(BACK, "ok"))]

        self.assertEqual(run_wizard(steps), {"a": "ok"})

    def test_review_can_change_one_answer_and_return(self) -> None:
        reviews = iter(["a", None])
        steps = [Step("a", scripted(1, 99)), Step("b", scripted(2))]

        state = run_wizard(steps, review=lambda s: next(reviews))

        self.assertEqual(state, {"a": 99, "b": 2})

    def test_cancel_raises_from_a_step_and_from_the_review(self) -> None:
        with self.assertRaises(Cancelled):
            run_wizard([Step("a", scripted(CANCEL))])
        with self.assertRaises(Cancelled):
            run_wizard([Step("a", scripted(1))], review=lambda s: CANCEL)

    def test_back_tokens(self) -> None:
        self.assertTrue(is_back("<"))
        self.assertTrue(is_back(" .. "))
        self.assertTrue(is_back("Back"))
        self.assertFalse(is_back("lab.example.com"))
        self.assertFalse(is_back(None))


class ScriptedQuestionary:
    """Answers each prompt by its message; records the messages asked."""

    class Choice:
        def __init__(self, title, value=None, checked=False):
            self.title = title
            self.value = value
            self.checked = checked

    def __init__(self, answers: dict):
        self.answers = answers
        self.asked = []

    def _prompt(self, message, choices=None):
        self.asked.append(message)
        answer = self.answers[message]
        if choices is not None and callable(answer):
            answer = answer(choices)

        class Prompt:
            def ask(_):
                return answer

        return Prompt()

    def text(self, message, **options):
        return self._prompt(message)

    def select(self, message, choices, **options):
        return self._prompt(message, choices)

    def checkbox(self, message, choices, **options):
        return self._prompt(message, choices)


class NewAppQuestionnaireTest(unittest.TestCase):
    """The questions that only make sense with a database are not asked without one."""

    def answers(self, database_mode: str) -> dict:
        return {
            "Project": "site-checker",
            "ghcr.io owner (org)": "example",
            "Environments": ["production"],
            "Domain for production": "panel.example.invalid",
            "Target host (MagicDNS name)": "host-1.tail.example.invalid",
            "Host age recipient (age1…)": "age1hostfixture",
            "Recovery age recipient (age1…)": "age1recoveryfixture",
            "Internal port": "8080",
            "Healthcheck path": "/",
            "Healthcheck timeout, seconds": "60",
            "Database": database_mode,
            "PostgreSQL major": 17,
            "Scheduled backups": False,
            "Restore validation query": "SELECT count(*) FROM checks",
            "Secret names, comma-separated (values come later via sops)": "API_TOKEN",
            "Write these files?": lambda choices: choices[0].value,
        }

    def ask(self, database_mode: str):
        from types import SimpleNamespace
        from unittest import mock

        from platform_automation.operator import console

        questionary = ScriptedQuestionary(self.answers(database_mode))
        context = SimpleNamespace(root=Path("/nonexistent/site-checker"))
        with mock.patch("platform_automation.operator.config.infras", lambda: []):
            answers = console.ask_app(context, questionary, style=None)
        return answers, questionary.asked

    def test_without_a_database_neither_major_nor_query_is_asked(self) -> None:
        answers, asked = self.ask("none")

        self.assertEqual(answers.database_mode, "none")
        self.assertNotIn("PostgreSQL major", asked)
        self.assertNotIn("Scheduled backups", asked)
        self.assertNotIn("Restore validation query", asked)
        self.assertEqual(answers.project, "site-checker")
        self.assertEqual(answers.secret_names, ("API_TOKEN",))

    def test_an_external_database_still_asks_for_major_and_query(self) -> None:
        answers, asked = self.ask("external")

        self.assertEqual(answers.database_mode, "external")
        self.assertIn("PostgreSQL major", asked)
        self.assertNotIn("Scheduled backups", asked)
        self.assertIn("Restore validation query", asked)
        self.assertEqual(answers.postgres_major, 17)
        self.assertEqual(answers.restore_query, "SELECT count(*) FROM checks")

    def test_the_database_question_offers_none(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from platform_automation.operator import console

        seen = {}

        def record(choices):
            seen["titles"] = [c.title for c in choices]
            return "none"

        answers = {**self.answers("none"), "Database": record}
        questionary = ScriptedQuestionary(answers)
        context = SimpleNamespace(root=Path("/nonexistent/site-checker"))
        with mock.patch("platform_automation.operator.config.infras", lambda: []):
            console.ask_app(context, questionary, style=None)

        self.assertTrue(
            any(
                t.startswith("none — a static site or an app without a database")
                for t in seen["titles"]
            ),
            seen,
        )


if __name__ == "__main__":
    unittest.main()
