"""The console's backup transfer, driven end to end with fake hosts and a fake age."""

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from platform_automation.operator import console, transfer
from platform_automation.operator.context import Host
from platform_automation.operator.remote import RemoteResult

HOST_RECIPIENT = "age1" + "h" * 58
RECOVERY = "age1" + "r" * 58
STAMP = "20260828T120000Z-schedule"


class Answer:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value


class FakeQuestionary:
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
        self.asked.append(("choices", [c.title for c in choices]))
        return self._next("select", message)

    def confirm(self, message, **kwargs):
        return self._next("confirm", message)


class TransferActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        (root / ".sops.yaml").write_text(
            "creation_rules:\n    - key_groups:\n          - age:\n"
            f"                - {HOST_RECIPIENT}\n                - {RECOVERY}\n"
        )
        self.source_key = root / "platform-host-2.agekey"
        self.source_key.write_text("synthetic")
        self.context = SimpleNamespace(
            root=root,
            target_host="platform-host-1.tailnet.example.invalid",
            core_pin=None,
        )
        self.scope = SimpleNamespace(
            project="app",
            environment="production",
            manifest=Path("deploy/platform.production.yml"),
        )
        self.running = SimpleNamespace(
            available=True, backend_state="Running", error=None
        )
        self.infra = SimpleNamespace(name="other-infra", path=Path("/l"))
        self.stand = Host(
            "platform-host-2",
            "platform-host-2.tailnet.example.invalid",
            "ops",
            "~/.ssh/other-ops",
        )
        self.calls = []

    def fake_run_platform(self, host, user, arguments, identity=None, **kwargs):
        self.calls.append(("platform", host, arguments, identity))
        return RemoteResult(
            True,
            0,
            {
                "backups": [
                    {"stamp": STAMP, "release_tag": "lab-v0.1.9", "verified": True}
                ]
            },
            None,
            "ssh …",
        )

    def fake_fetch(
        self, command, stdin=None, stdout=None, stderr=None, check=False, timeout=None
    ):
        self.calls.append(("fetch", command[-1], command))
        stdout.write(b"ciphertext-from-source")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def fake_rekey(self, dump, output, source_key, recipients, popen=None):
        self.calls.append(("rekey", dump.read_bytes(), source_key, recipients))
        output.write_bytes(b"re-keyed")

    def fake_import(self, command, stdin=None, **kwargs):
        self.calls.append(("import", command[-1], stdin.read()))
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"stamp": STAMP, "path": "/x"}).encode(), b""
        )

    def drive(self, questionary):
        restore = SimpleNamespace(run=lambda: 0)
        output = io.StringIO()
        with (
            mock.patch.object(console, "read_tailnet", return_value=self.running),
            mock.patch(
                "platform_automation.operator.config.infras", return_value=[self.infra]
            ),
            mock.patch(
                "platform_automation.operator.context.read_hosts",
                return_value=(self.stand,),
            ),
            mock.patch(
                "platform_automation.operator.context.read_retired_hosts",
                return_value=(),
            ),
            mock.patch(
                "platform_automation.operator.doctor.host_secret_path",
                return_value=self.source_key,
            ),
            mock.patch.object(
                console, "run_platform", side_effect=self.fake_run_platform
            ),
            mock.patch.object(console.subprocess, "run", side_effect=self.fake_fetch),
            mock.patch.object(transfer, "rekey", side_effect=self.fake_rekey),
            mock.patch(
                "platform_automation.operator.remote.checked_run",
                side_effect=self.fake_import,
            ),
            mock.patch.object(console, "restore_action", return_value=restore),
            redirect_stdout(output),
        ):
            action = console.transfer_action(
                self.context, self.scope, (questionary, None)
            )
            code = action.run()
        return code, output.getvalue()

    def test_fetches_rekeys_imports_and_offers_the_restore(self) -> None:
        questionary = FakeQuestionary(
            [self.first_source, "app", "lab", self.first_entry, True, False]
        )
        code, shown = self.drive(questionary)

        self.assertEqual(code, 0, shown)
        kinds = [c[0] for c in self.calls]
        self.assertEqual(kinds, ["platform", "fetch", "rekey", "import"])

        listing = self.calls[0]
        self.assertEqual(listing[1], "platform-host-2.tailnet.example.invalid")
        self.assertEqual(
            listing[2], ["backups", "--project", "app", "--environment", "lab"]
        )
        self.assertEqual(listing[3], Path("~/.ssh/other-ops").expanduser())

        fetch = self.calls[1]
        self.assertEqual(
            fetch[1], f"sudo -n cat /var/backups/platform/app/lab/{STAMP}.dump.age"
        )
        self.assertIn("platform-host-2.tailnet.example.invalid", " ".join(fetch[2]))

        rekey = self.calls[2]
        self.assertEqual(rekey[1], b"ciphertext-from-source")
        self.assertEqual(rekey[2], self.source_key)
        self.assertEqual(rekey[3], [HOST_RECIPIENT, RECOVERY])

        imported = self.calls[3]
        self.assertIn(
            "sudo -n platform backup-import --project app --environment production",
            imported[1],
        )
        self.assertIn(
            f"--stamp {STAMP} --source-host platform-host-2 --source-project app --source-environment lab",
            imported[1],
        )
        self.assertEqual(imported[2], b"re-keyed")
        self.assertIn("imported", shown)
        self.assertIn(f"later: Backups: restore → {STAMP}", shown)

    def test_restoring_right_away_hands_over_to_the_restore_action(self) -> None:
        questionary = FakeQuestionary(
            [self.first_source, "app", "lab", self.first_entry, True, True]
        )
        code, shown = self.drive(questionary)
        self.assertEqual(code, 0, shown)
        self.assertNotIn("later:", shown)

    def test_declining_fetches_nothing(self) -> None:
        questionary = FakeQuestionary(
            [self.first_source, "app", "lab", self.first_entry, False]
        )
        code, shown = self.drive(questionary)
        self.assertEqual(code, 130)
        self.assertEqual([c[0] for c in self.calls], ["platform"])
        self.assertIn("nothing fetched", shown)

    def test_without_the_source_key_on_this_machine_it_stops(self) -> None:
        self.source_key.unlink()
        questionary = FakeQuestionary([self.first_source])
        code, shown = self.drive(questionary)
        self.assertEqual(code, 1)
        self.assertIn("not on this machine", shown)
        self.assertEqual(self.calls, [])

    # The select answers are the Source and the entry objects the action built;
    # the fake questionary hands back whatever value it is given, so these
    # stand-ins are matched by what the action does with them.
    @property
    def first_source(self):
        return transfer.Source(
            "other-infra",
            "platform-host-2",
            "platform-host-2.tailnet.example.invalid",
            Path("~/.ssh/other-ops").expanduser(),
            self.source_key,
        )

    @property
    def first_entry(self):
        return {"stamp": STAMP, "release_tag": "lab-v0.1.9", "verified": True}


if __name__ == "__main__":
    unittest.main()
