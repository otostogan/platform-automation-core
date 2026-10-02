import argparse
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from platform_automation import platform_cli
from platform_automation.platform_cli import parse_arguments, run_verify_backup
from platform_automation.restore_runtime import RestoreRuntimeError

# Whether a release schedules dumps does not matter here: a pre-migration
# dump of one that schedules none is proven all the same.
SCOPES = [
    ("alpha", "lab"),
    ("bravo", "production"),
    ("retired", "lab"),
    ("empty", "lab"),
]


class VerifyEveryBackupTest(unittest.TestCase):
    def run_all(self, verifier, json_output=True):
        arguments = argparse.Namespace(
            all=True, project=None, environment=None, stamp=None, json=json_output
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        root = Path("/nonexistent")
        with (
            mock.patch.multiple(
                platform_cli,
                list_project_scopes=lambda _: SCOPES,
                is_retired=lambda _, project, __: project == "retired",
                load_current_manifest=lambda _, __, project, *___: (
                    {"name": project},
                    {},
                ),
                list_backups=lambda directory: (
                    [] if directory.parent.name == "empty" else ["20260101T000000Z-a"]
                ),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = run_verify_backup(
                arguments,
                root,
                root,
                root,
                root,
                root,
                root,
                root,
                root,
                root,
                root,
                2,
                verifier=verifier,
            )
        return code, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def proven(**call):
        return {
            "outcome": "succeeded",
            "stamp": "20260101T000000Z-a",
            "result": "1",
        }

    def test_every_scheduled_application_is_proven_and_the_rest_say_why_not(
        self,
    ) -> None:
        calls = []

        def verifier(**call):
            calls.append((call["project"], call["environment"], call["stamp"]))
            return self.proven()

        code, stdout, _ = self.run_all(verifier)

        self.assertEqual(code, 0)
        self.assertEqual(calls, [("alpha", "lab", None), ("bravo", "production", None)])
        outcomes = {
            entry["project"]: (entry["outcome"], entry.get("reason"))
            for entry in json.loads(stdout)["scopes"]
        }
        self.assertEqual(
            outcomes,
            {
                "alpha": ("succeeded", None),
                "bravo": ("succeeded", None),
                "retired": ("skipped", "retired"),
                "empty": ("skipped", "no backup yet"),
            },
        )

    def test_one_failure_does_not_stop_the_others_and_fails_the_run(self) -> None:
        def verifier(**call):
            if call["project"] == "alpha":
                raise RestoreRuntimeError("pg_restore failed")
            return self.proven()

        code, stdout, stderr = self.run_all(verifier)

        self.assertEqual(code, 1)
        outcomes = {
            entry["project"]: entry["outcome"] for entry in json.loads(stdout)["scopes"]
        }
        self.assertEqual(outcomes["alpha"], "failed")
        self.assertEqual(outcomes["bravo"], "succeeded")
        self.assertIn("alpha/lab: pg_restore failed", stderr)

    def test_plain_output_names_each_application(self) -> None:
        code, stdout, _ = self.run_all(self.proven, json_output=False)

        self.assertEqual(code, 0)
        self.assertIn("alpha/lab: succeeded (20260101T000000Z-a)", stdout)
        self.assertIn("retired/lab: skipped (retired)", stdout)

    def test_all_and_a_named_application_exclude_each_other(self) -> None:
        root = Path("/nonexistent")
        for argv in (
            ["verify-backup", "--all", "--project", "alpha", "--environment", "lab"],
            ["verify-backup", "--all", "--from", "20260101T000000Z-a"],
            ["verify-backup"],
            ["verify-backup", "--project", "alpha"],
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = run_verify_backup(
                    parse_arguments(argv),
                    root,
                    root,
                    root,
                    root,
                    root,
                    root,
                    root,
                    root,
                    root,
                    root,
                    2,
                    verifier=self.proven,
                )
            self.assertEqual(code, 2, argv)
            self.assertIn("verify-backup error:", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
