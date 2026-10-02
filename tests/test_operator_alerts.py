import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import yaml

from platform_automation.operator import alerts
from platform_automation.operator.console import alerts_action
from platform_automation.operator.context import Host

TOKEN = "1234567890:" + "A" * 35
UPDATES = [
    {"message": {"chat": {"id": 42, "type": "private", "first_name": "Op"}}},
    {"message": {"chat": {"id": -100123, "type": "supergroup", "title": "Alerts"}}},
    {"message": {"chat": {"id": 42, "type": "private", "first_name": "Op"}}},
]


class Response:
    def __init__(self, document):
        self.body = json.dumps(document).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def fake_api(calls, updates=UPDATES):
    def opener(request, timeout=None):
        method = request.full_url.rsplit("/", 1)[1]
        calls.append((method, request.data))
        if f"/bot{TOKEN}/" not in request.full_url:
            raise urllib.error.HTTPError(
                "hidden",
                401,
                "Unauthorized",
                None,
                io.BytesIO(b'{"ok":false,"description":"Unauthorized"}'),
            )
        result = {
            "getMe": {"username": "example_alerts_bot"},
            "getUpdates": updates,
            "sendMessage": {"message_id": 1},
        }[method]
        return Response({"ok": True, "result": result})

    return opener


class TelegramApiTest(unittest.TestCase):
    def test_token_and_chat_shapes(self) -> None:
        self.assertTrue(alerts.valid_token(TOKEN))
        self.assertFalse(alerts.valid_token("not a token"))
        self.assertTrue(alerts.valid_chat("-1001234567890"))
        self.assertFalse(alerts.valid_chat("@channel"))

    def test_chats_are_listed_once_newest_first(self) -> None:
        chats = alerts.recent_chats(TOKEN, opener=fake_api([]))

        self.assertEqual([chat.id for chat in chats], ["42", "-100123"])
        self.assertEqual(chats[1].title, "Alerts")
        self.assertEqual(
            alerts.bot_name(TOKEN, opener=fake_api([])), "@example_alerts_bot"
        )

    def test_a_refusal_is_reported_without_the_token(self) -> None:
        wrong = "9999999999:" + "B" * 35
        with self.assertRaises(alerts.AlertsError) as raised:
            alerts.bot_name(wrong, opener=fake_api([]))

        self.assertIn("Unauthorized", str(raised.exception))
        self.assertNotIn(wrong, str(raised.exception))
        self.assertNotIn(wrong, alerts.masked("getMe"))

    def test_an_unreachable_api_is_reported_without_the_token(self) -> None:
        def opener(request, timeout=None):
            raise urllib.error.URLError(f"cannot reach {request.full_url}"[:20])

        with self.assertRaises(alerts.AlertsError) as raised:
            alerts.bot_name(TOKEN, opener=opener)
        self.assertNotIn(TOKEN, str(raised.exception))


class LocalSecretsTest(unittest.TestCase):
    def test_keys_are_appended_once_and_other_lines_stay(self) -> None:
        text = "# age\nsecrets_age_key_source: ~/keys/h.agekey\n"
        first = alerts.set_values(text, "~/keys/telegram-alerts.token", "-100123")

        self.assertTrue(first.startswith(text))
        document = yaml.safe_load(first)
        self.assertEqual(document[alerts.CHAT_KEY], "-100123")
        self.assertEqual(document[alerts.TOKEN_KEY], "~/keys/telegram-alerts.token")

        second = alerts.set_values(first, "~/keys/telegram-alerts.token", "42")
        self.assertEqual(second.count(alerts.CHAT_KEY), 1)
        self.assertEqual(second.count(alerts.TOKEN_KEY), 1)
        self.assertEqual(yaml.safe_load(second)[alerts.CHAT_KEY], "42")
        self.assertIn("# age", second)

    def test_the_token_file_sits_beside_the_age_key(self) -> None:
        self.assertEqual(
            alerts.token_location({"secrets_age_key_source": "~/keys/h.agekey"}),
            "~/keys/telegram-alerts.token",
        )
        self.assertEqual(
            alerts.token_location({alerts.TOKEN_KEY: "/elsewhere/t"}), "/elsewhere/t"
        )
        self.assertIsNone(alerts.token_location(None))

    def test_the_token_is_written_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keys/telegram-alerts.token"
            alerts.write_token(path, TOKEN)

            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(alerts.read_token(path), TOKEN)


class Prompts:
    """Answers in order; a wrong kind of prompt fails the test loudly."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def _next(self, kind, message):
        self.asked.append((kind, message))
        expected, value = self.answers.pop(0)
        assert expected == kind, f"asked {kind} {message!r}, script had {expected}"
        return SimpleNamespace(ask=lambda: value)

    def password(self, message, **_):
        return self._next("password", message)

    def confirm(self, message, **_):
        return self._next("confirm", message)

    def text(self, message, **_):
        return self._next("text", message)

    def select(self, message, choices=(), **_):
        self.asked.append(("select", message))
        expected, pick = self.answers.pop(0)
        assert expected == "select"
        return SimpleNamespace(ask=lambda: pick([choice.value for choice in choices]))

    @staticmethod
    def Choice(title, value):
        return SimpleNamespace(title=title, value=value)


class ConsoleFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root = base / "infra"
        self.keys = base / "keys"
        self.secrets = (
            self.root / "inventory/host_vars/platform-host-1/local-secrets.yml"
        )
        self.secrets.parent.mkdir(parents=True)
        self.secrets.write_text(
            f"secrets_age_key_source: {self.keys}/platform-host-1.agekey\n"
        )
        self.context = SimpleNamespace(root=self.root)
        self.host = Host(
            "platform-host-1", "platform-host-1.tailnet.example.net", "ops"
        )

    def run_action(self, answers, calls, updates=UPDATES):
        prompts = Prompts(answers)
        action = alerts_action(
            self.context, self.host, (prompts, None), opener=fake_api(calls, updates)
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = action.run()
        self.assertEqual(prompts.answers, [], "not every scripted answer was asked for")
        return code, output.getvalue()

    def test_bot_and_chat_are_proven_then_a_file_and_two_keys_are_written(self) -> None:
        calls = []
        code, output = self.run_action(
            [
                ("password", TOKEN),
                ("select", lambda options: options[1]),  # the group
                ("confirm", True),  # send a test message
            ],
            calls,
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            [method for method, _ in calls], ["getMe", "getUpdates", "sendMessage"]
        )
        self.assertIn(b"chat_id=-100123", calls[2][1])
        token_file = self.keys / "telegram-alerts.token"
        self.assertEqual(token_file.read_text().strip(), TOKEN)
        self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
        document = yaml.safe_load(self.secrets.read_text())
        self.assertEqual(document[alerts.TOKEN_KEY], str(token_file))
        self.assertEqual(document[alerts.CHAT_KEY], "-100123")
        self.assertIn("secrets_age_key_source", document)
        self.assertIn("@example_alerts_bot", output)
        self.assertNotIn(TOKEN, output)

    def test_backing_out_of_the_token_writes_nothing(self) -> None:
        before = self.secrets.read_text()
        code, _ = self.run_action([("password", None)], [])

        self.assertEqual(code, 130)
        self.assertEqual(self.secrets.read_text(), before)
        self.assertFalse((self.keys / "telegram-alerts.token").exists())

    def test_a_wrong_token_stops_before_anything_is_written(self) -> None:
        wrong = "9999999999:" + "B" * 35
        code, output = self.run_action([("password", wrong)], [])

        self.assertEqual(code, 1)
        self.assertIn("Unauthorized", output)
        self.assertNotIn(wrong, output)
        self.assertFalse((self.keys / "telegram-alerts.token").exists())

    def test_a_second_run_keeps_the_token_and_changes_only_the_chat(self) -> None:
        self.run_action(
            [
                ("password", TOKEN),
                ("select", lambda options: options[1]),
                ("confirm", False),
            ],
            [],
        )
        calls = []
        code, _ = self.run_action(
            [
                ("confirm", True),  # keep the existing token
                ("select", lambda options: options[0]),  # the private chat
                ("confirm", False),  # no test message
            ],
            calls,
        )

        self.assertEqual(code, 0)
        self.assertEqual([method for method, _ in calls], ["getMe", "getUpdates"])
        text = self.secrets.read_text()
        self.assertEqual(text.count(alerts.CHAT_KEY), 1)
        self.assertEqual(yaml.safe_load(text)[alerts.CHAT_KEY], "42")

    def test_with_no_chats_the_id_can_be_typed(self) -> None:
        code, output = self.run_action(
            [
                ("password", TOKEN),
                ("select", lambda options: options[-1]),  # type a chat id
                ("text", "-1009"),
                ("confirm", False),
            ],
            [],
            updates=[],
        )

        self.assertEqual(code, 0)
        self.assertIn("No chat has written to the bot yet", output)
        self.assertEqual(
            yaml.safe_load(self.secrets.read_text())[alerts.CHAT_KEY], "-1009"
        )


if __name__ == "__main__":
    unittest.main()
