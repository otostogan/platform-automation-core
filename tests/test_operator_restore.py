import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from platform_automation.operator.console import restore_action, rotate_action
from platform_automation.operator.remote import RemoteResult

OLDEST = "20251231T000000Z-schedule"
OLD = "20260101T000000Z-schedule"
NEW = "20260102T000000Z-schedule"
BACKUPS = {
    "backups": [
        {
            "stamp": OLDEST,
            "reason": "schedule",
            "release_tag": "lab-v1.1.0",
            "bytes": 9,
            "verified": True,
        },
        {"stamp": OLD, "reason": "schedule", "release_tag": "lab-v1.0.0", "bytes": 10},
        {
            "stamp": NEW,
            "reason": "schedule",
            "release_tag": "lab-v1.1.0",
            "bytes": 12,
            "verified": True,
        },
    ]
}
STATUS = {"current": {"release_tag": "lab-v1.1.0"}}
NAME = "example/lab"


class Prompts:
    """Answers in order; a prompt of another kind fails the test loudly."""

    def __init__(self, answers):
        self.answers = list(answers)

    def _next(self, kind, message):
        expected, value = self.answers.pop(0)
        assert expected == kind, f"asked {kind} {message!r}, script had {expected}"
        return SimpleNamespace(ask=lambda: value)

    def confirm(self, message, **_):
        return self._next("confirm", message)

    def text(self, message, **_):
        return self._next("text", message)

    def select(self, message, choices=(), **_):
        expected, pick = self.answers.pop(0)
        assert expected == "select", message
        return SimpleNamespace(ask=lambda: pick([choice.value for choice in choices]))

    @staticmethod
    def Choice(title, value):
        return SimpleNamespace(title=title, value=value)


class Host:
    """Records every command; answers from a table, failing where told to."""

    def __init__(self, failing=()):
        self.calls = []
        self.failing = set(failing)

    def __call__(self, host, user, arguments, **options):
        verb = arguments[0]
        self.calls.append(list(arguments))
        if verb in self.failing:
            return RemoteResult(False, 1, None, f"{verb} error: boom", "ssh …")
        document = {
            "backups": BACKUPS,
            "status": STATUS,
            "verify-backup": {"outcome": "succeeded", "stamp": OLD, "result": "1"},
            "backup": {"path": "/var/backups/x", "bytes": 1, "offsite": {}},
            "restore": (
                {"stamp": arguments[arguments.index("--from") + 1]}
                if "--from" in arguments
                else {}
            ),
            "rotate-database-password": {"rotated_at": "now", "recipients": 2},
        }[verb]
        return RemoteResult(True, 0, document, "", "ssh …")

    def verbs(self):
        return [call[0] for call in self.calls]


class RestoreFlowTest(unittest.TestCase):
    context = SimpleNamespace(
        target_host="platform-host-1.tailnet.example.net",
        core_pin="v0.0.0",
        root=Path("/nonexistent"),
    )
    scope = SimpleNamespace(project="example", environment="lab")

    def run_restore(self, answers, host):
        prompts = Prompts(answers)
        action = restore_action(self.context, self.scope, (prompts, None), remote=host)
        output = io.StringIO()
        with redirect_stdout(output):
            code = action.run()
        self.assertEqual(prompts.answers, [], "not every scripted answer was asked for")
        return code, output.getvalue()

    def test_a_proven_dump_is_restored_after_a_safety_dump_and_the_typed_name(
        self,
    ) -> None:
        host = Host()
        code, output = self.run_restore(
            [
                ("select", lambda options: options[0]),  # newest first
                ("confirm", True),  # safety dump
                ("text", NAME),
            ],
            host,
        )

        self.assertEqual(code, 0)
        self.assertEqual(host.verbs(), ["backups", "status", "backup", "restore"])
        restore = host.calls[-1]
        self.assertEqual(restore[restore.index("--from") + 1], NEW)
        self.assertIn("--confirm-destructive", restore)
        self.assertIn("restored", output)

    def test_an_unproven_dump_is_proven_first_and_the_gap_is_named(self) -> None:
        host = Host()
        code, output = self.run_restore(
            [
                ("select", lambda options: options[1]),  # the older, unproven one
                ("confirm", False),  # no safety dump
                ("text", NAME),
            ],
            host,
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            host.verbs(), ["backups", "status", "verify-backup", "restore"]
        )
        proof = host.calls[2]
        self.assertEqual(proof[proof.index("--from") + 1], OLD)
        self.assertIn("taken on lab-v1.0.0", output)
        self.assertIn("lab-v1.1.0 is deployed now", output)

    def test_a_dump_that_does_not_restore_stops_everything(self) -> None:
        host = Host(failing={"verify-backup"})
        code, output = self.run_restore([("select", lambda options: options[1])], host)

        self.assertEqual(code, 1)
        self.assertNotIn("restore", host.verbs())
        self.assertIn("the live database was not touched", output)

    def test_a_failed_safety_dump_stops_before_the_restore(self) -> None:
        host = Host(failing={"backup"})
        code, _ = self.run_restore(
            [("select", lambda options: options[0]), ("confirm", True)], host
        )

        self.assertEqual(code, 1)
        self.assertNotIn("restore", host.verbs())

    def test_the_oldest_dump_gets_no_safety_dump_that_could_prune_it(self) -> None:
        host = Host()
        code, output = self.run_restore(
            [
                ("select", lambda options: options[2]),  # the oldest
                ("text", NAME),  # no question about a safety dump
            ],
            host,
        )

        self.assertEqual(code, 0)
        self.assertNotIn("backup", host.verbs())
        self.assertIn("No dump of the current state will be taken", output)
        restore = host.calls[-1]
        self.assertEqual(restore[restore.index("--from") + 1], OLDEST)

    def test_a_dump_beyond_the_manifests_limit_is_at_risk_too(self) -> None:
        from platform_automation.operator.console import safety_dump_would_prune

        entries = [{"stamp": name} for name in ("d", "c", "b", "a")]  # newest first
        self.assertTrue(safety_dump_would_prune(entries, entries[3], None))
        self.assertFalse(safety_dump_would_prune(entries, entries[2], None))
        # three kept: one more dump leaves room for the two newest only
        self.assertTrue(safety_dump_would_prune(entries, entries[2], 3))
        self.assertFalse(safety_dump_would_prune(entries, entries[1], 3))
        # one kept: the new dump would be the only survivor
        self.assertTrue(safety_dump_would_prune(entries, entries[0], 1))
        self.assertFalse(safety_dump_would_prune(entries, entries[0], 2))

    def test_an_unreadable_current_release_stops_before_anything(self) -> None:
        host = Host(failing={"status"})
        code, output = self.run_restore([], host)

        self.assertEqual(code, 1)
        self.assertEqual(host.verbs(), ["backups", "status"])
        self.assertIn("cannot tell which release is deployed", output)

    def test_the_console_outwaits_the_hosts_own_limits(self) -> None:
        from platform_automation.operator.console import BACKUP_TIMEOUT_SECONDS

        # thirty minutes to decrypt plus an hour to restore, with room to spare
        self.assertGreaterEqual(BACKUP_TIMEOUT_SECONDS, 1800 + 3600 + 600)
        host = Host()
        seen = {}

        def remote(target, user, arguments, **options):
            seen[arguments[0]] = options.get("timeout")
            return host(target, user, arguments, **options)

        self.run_restore(
            [
                ("select", lambda options: options[1]),
                ("confirm", True),
                ("text", NAME),
            ],
            remote,
        )
        for verb in ("verify-backup", "backup", "restore"):
            self.assertEqual(seen[verb], BACKUP_TIMEOUT_SECONDS, verb)

    def test_a_mistyped_name_restores_nothing(self) -> None:
        host = Host()
        code, output = self.run_restore(
            [
                ("select", lambda options: options[0]),
                ("confirm", False),
                ("text", "example/production"),
            ],
            host,
        )

        self.assertEqual(code, 130)
        self.assertNotIn("restore", host.verbs())
        self.assertIn("nothing changed", output)

    def test_the_newest_dump_is_offered_first_in_either_listing_order(self) -> None:
        for listing in (BACKUPS["backups"], list(reversed(BACKUPS["backups"]))):
            host = Host()
            call = host.__call__

            def remote(target, user, arguments, _listing=listing, **options):
                if arguments[0] == "backups":
                    host.calls.append(list(arguments))
                    return RemoteResult(True, 0, {"backups": _listing}, "", "ssh …")
                return call(target, user, arguments, **options)

            code, _ = self.run_restore(
                [
                    ("select", lambda options: options[0]),
                    ("confirm", False),
                    ("text", NAME),
                ],
                remote,
            )
            self.assertEqual(code, 0)
            restore = host.calls[-1]
            self.assertEqual(restore[restore.index("--from") + 1], NEW)

    def test_backing_out_of_the_dump_list_changes_nothing(self) -> None:
        host = Host()
        code, _ = self.run_restore([("select", lambda options: options[-1])], host)

        self.assertEqual(code, 130)
        self.assertEqual(host.verbs(), ["backups", "status"])

    def test_no_dumps_means_nothing_to_restore(self) -> None:
        host = Host()
        host_call = host.__call__

        def empty(target, user, arguments, **options):
            if arguments[0] == "backups":
                host.calls.append(list(arguments))
                return RemoteResult(True, 0, {"backups": []}, "", "ssh …")
            return host_call(target, user, arguments, **options)

        prompts = Prompts([])
        action = restore_action(self.context, self.scope, (prompts, None), remote=empty)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(action.run(), 1)
        self.assertEqual(host.verbs(), ["backups"])


class RotateFlowTest(unittest.TestCase):
    context = RestoreFlowTest.context
    scope = RestoreFlowTest.scope

    def run_rotate(self, answer, host):
        prompts = Prompts([("confirm", answer)])
        action = rotate_action(self.context, self.scope, (prompts, None), remote=host)
        output = io.StringIO()
        with redirect_stdout(output):
            code = action.run()
        return code, output.getvalue()

    def test_declining_changes_nothing(self) -> None:
        host = Host()
        code, _ = self.run_rotate(False, host)

        self.assertEqual(code, 130)
        self.assertEqual(host.calls, [])

    def test_confirming_rotates_with_the_disruptive_flag(self) -> None:
        host = Host()
        code, output = self.run_rotate(True, host)

        self.assertEqual(code, 0)
        self.assertEqual(host.verbs(), ["rotate-database-password"])
        self.assertIn("--confirm-disruptive", host.calls[0])
        self.assertIn("short interruption", output)
        self.assertIn("2 recipient(s)", output)


if __name__ == "__main__":
    unittest.main()
