import json
import subprocess
import unittest
from pathlib import Path

from platform_automation.operator.deploy import (
    DeployError,
    dispatch_arguments,
    find_run,
    list_run_ids,
    parse_runs,
    release_tags,
)


def completed(stdout: str, code: int = 0):
    return subprocess.CompletedProcess([], code, stdout.encode(), b"")


class DispatchArgumentsTest(unittest.TestCase):
    def test_only_declared_inputs_are_passed_and_empty_ref_is_omitted(self) -> None:
        self.assertEqual(
            dispatch_arguments(
                ("environment", "ref", "label"), "lab", "", "", "v2/main"
            ),
            [
                "workflow",
                "run",
                "deploy.yml",
                "--ref",
                "v2/main",
                "-f",
                "environment=lab",
            ],
        )
        self.assertEqual(
            dispatch_arguments(("environment", "ref"), "production", "v0.3.0"),
            [
                "workflow",
                "run",
                "deploy.yml",
                "-f",
                "environment=production",
                "-f",
                "ref=v0.3.0",
            ],
        )
        self.assertNotIn(
            "label", " ".join(dispatch_arguments(("environment",), "lab", "x", "y"))
        )


class RunsTest(unittest.TestCase):
    def test_runs_are_parsed_and_the_new_one_is_found(self) -> None:
        before = json.dumps(
            [
                {
                    "databaseId": 1,
                    "url": "u1",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        )
        after = json.dumps(
            [
                {
                    "databaseId": 2,
                    "url": "u2",
                    "status": "in_progress",
                    "conclusion": None,
                },
                {
                    "databaseId": 1,
                    "url": "u1",
                    "status": "completed",
                    "conclusion": "success",
                },
            ]
        )
        calls = []

        def runner(command, **_):
            calls.append(command)
            return completed(before if len(calls) < 3 else after)

        self.assertEqual({r.id for r in parse_runs(after)}, {1, 2})
        known = list_run_ids(Path("."), "v2/main", runner=runner)
        self.assertEqual(known, {1})
        run = find_run(
            Path("."), "v2/main", known, runner=runner, sleeper=lambda _: None
        )
        self.assertEqual((run.id, run.url, run.status), (2, "u2", "in_progress"))
        self.assertIn("--branch", calls[0])

    def test_no_new_run_is_an_error_not_a_hang(self) -> None:
        runner = lambda command, **_: completed("[]")
        with self.assertRaises(DeployError):
            find_run(
                Path("."),
                None,
                set(),
                runner=runner,
                attempts=2,
                sleeper=lambda _: None,
            )

    def test_garbage_from_gh_is_no_runs(self) -> None:
        self.assertEqual(parse_runs("not json"), [])
        self.assertEqual(parse_runs('{"a": 1}'), [])

    def test_release_tags_are_version_tags_newest_first(self) -> None:
        runner = lambda command, **_: completed("v0.3.0\nv0.2.1\nnightly\nv0.2.0\n")
        self.assertEqual(
            release_tags(Path("."), runner=runner, limit=2), ["v0.3.0", "v0.2.1"]
        )

    def test_missing_gh_is_named(self) -> None:
        def runner(command, **_):
            raise FileNotFoundError("gh")

        with self.assertRaises(DeployError) as caught:
            list_run_ids(Path("."), None, runner=runner)
        self.assertIn("gh (GitHub CLI)", str(caught.exception))
