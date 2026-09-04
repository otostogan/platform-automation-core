import unittest

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


if __name__ == "__main__":
    unittest.main()
