import unittest
from pathlib import Path

from platform_automation.operator.console import (
    render_backups,
    render_projects,
    render_status,
    deploy_command,
    explain_host_error,
    tailnet_gate,
)
from platform_automation.operator.tailnet import parse_status


class DeployCommandTest(unittest.TestCase):
    def test_inputs_come_from_the_workflow_and_environment_is_filled_in(self) -> None:
        self.assertEqual(
            deploy_command(("environment", "ref", "label"), "production"),
            "gh workflow run deploy.yml -f environment=production -f ref=<ref> -f label=<label>",
        )

    def test_a_workflow_with_other_inputs_gets_its_own_names(self) -> None:
        command = deploy_command(
            ("image_reference", "release_tag", "application_commit"), "lab"
        )

        self.assertIn("-f image_reference=<image_reference>", command)
        self.assertNotIn("environment", command)

    def test_no_declared_inputs_is_said_not_guessed(self) -> None:
        self.assertIn("declares no workflow_dispatch inputs", deploy_command((), "lab"))


class ExplainHostErrorTest(unittest.TestCase):
    def test_missing_command_means_the_host_core_is_older(self) -> None:
        reason = explain_host_error(
            "platform: error: argument command: invalid choice: 'projects' (choose from 'deploy', 'status')",
            "v0.13.3",
        )

        self.assertIn("older than this console", reason)
        self.assertIn("pins v0.13.3", reason)

    def test_unknown_errors_get_no_story(self) -> None:
        self.assertIsNone(explain_host_error("backups error: something odd", "v0.13.3"))


class TailnetGateTest(unittest.TestCase):
    def test_stopped_client_is_named_before_any_ssh(self) -> None:
        message = tailnet_gate(parse_status({"BackendState": "Stopped"}))

        self.assertIn("Stopped", message)
        self.assertIn("tailscale up", message)

    def test_running_client_opens_the_gate(self) -> None:
        self.assertIsNone(tailnet_gate(parse_status({"BackendState": "Running"})))

    def test_missing_client_is_named(self) -> None:
        self.assertIn("not available", tailnet_gate(parse_status("garbage")))


class RenderStatusTest(unittest.TestCase):
    def test_deployed_release_with_backups(self) -> None:
        text = render_status(
            {
                "project": "my-app",
                "environment": "lab",
                "release_count": 3,
                "current": {
                    "release_tag": "v1.2.0",
                    "status": "deployed",
                    "healthcheck": {"status": "succeeded"},
                    "migration": {"status": "not_required"},
                },
                "backups": {
                    "count": 2,
                    "latest": "20260902T041500Z-schedule",
                    "loss_window": {"newest_age_minutes": 7, "overdue": False},
                    "last_verified": {
                        "outcome": "succeeded",
                        "stamp": "20260902T041500Z-schedule",
                    },
                    "offsite": {"state": "current"},
                },
            }
        )

        self.assertIn("my-app/lab", text)
        self.assertIn("v1.2.0", text)
        self.assertIn("healthcheck=succeeded", text)
        self.assertIn("loss window now: up to 7 minute(s)", text)
        self.assertIn("last proven restorable: succeeded", text)
        self.assertIn("offsite: current", text)

    def test_overdue_schedule_is_named_not_computed(self) -> None:
        text = render_status(
            {
                "project": "p",
                "environment": "lab",
                "current": None,
                "backups": {
                    "count": 1,
                    "latest": "x",
                    "loss_window": {"overdue": True},
                    "last_verified": None,
                },
            }
        )

        self.assertIn("current release: none", text)
        self.assertIn("loss window: unknown", text)
        self.assertIn("last proven restorable: never", text)


class RenderProjectsTest(unittest.TestCase):
    def test_one_line_per_scope_with_the_deployed_release(self) -> None:
        text = render_projects(
            {
                "count": 2,
                "projects": [
                    {
                        "project": "my-app",
                        "environment": "lab",
                        "release_count": 3,
                        "current": {
                            "release_tag": "v1.2.0",
                            "status": "deployed",
                            "healthcheck": "succeeded",
                        },
                        "latest": {
                            "release_tag": "v1.2.0",
                            "status": "deployed",
                            "healthcheck": "succeeded",
                        },
                    },
                    {
                        "project": "my-app",
                        "environment": "production",
                        "release_count": 0,
                        "current": None,
                        "latest": None,
                    },
                ],
            }
        )

        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("PROJECT"))
        self.assertIn("v1.2.0", lines[1])
        self.assertIn("deployed", lines[1])
        self.assertIn("production", lines[2])
        self.assertIn("none", lines[2])

    def test_empty_host(self) -> None:
        self.assertEqual(
            render_projects({"count": 0, "projects": []}), "No projects on this host"
        )


class RenderBackupsTest(unittest.TestCase):
    def test_table_mirrors_the_host_columns(self) -> None:
        text = render_backups(
            {
                "project": "p",
                "environment": "lab",
                "backups": [
                    {
                        "stamp": "20260902T041500Z-schedule",
                        "reason": "schedule",
                        "bytes": 1234,
                        "release_tag": "v1.2.0",
                        "offsite": True,
                        "verified": True,
                    },
                    {
                        "stamp": "20260902T030000Z-operator",
                        "reason": "operator",
                        "bytes": None,
                        "offsite": None,
                    },
                ],
            }
        )

        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("STAMP"))
        self.assertIn("20260902T041500Z", lines[1])
        self.assertIn("yes", lines[1])
        self.assertIn("n/a", lines[2])

    def test_empty_list_says_so(self) -> None:
        self.assertEqual(
            render_backups({"project": "p", "environment": "lab", "backups": []}),
            "No backups for p/lab",
        )


if __name__ == "__main__":
    unittest.main()


class MenuGroupsTest(unittest.TestCase):
    def test_daily_actions_stay_on_top_and_the_rest_is_grouped_in_order(self) -> None:
        from platform_automation.operator.console import Action, group_of, menu_entries

        labels = [
            "Deploy",
            "Roll back",
            "Status on the host",
            "Logs",
            "Backups: list",
            "Validate manifest and Compose",
            "Secrets: push .env.lab → ciphertext",
            "Secrets: pull ciphertext → .env.lab (needs a key)",
            "Database: tunnel for your own client (30 min)",
            "Database: psql on the host",
            "Retire: stop and free the domains (data kept)",
            "Purge: delete data, backups and history (irreversible)",
        ]
        entries = menu_entries([Action(label, "cmd") for label in labels])

        shown = [e[0] if isinstance(e, tuple) else e.label for e in entries]
        self.assertEqual(
            shown,
            [
                "Deploy",
                "Roll back",
                "Status on the host",
                "Logs",
                "Database & backups",
                "Secrets & config",
                "Retire or purge",
            ],
        )
        groups = {
            e[0]: [a.label.split(":")[0] for a in e[1]]
            for e in entries
            if isinstance(e, tuple)
        }
        self.assertEqual(
            groups["Database & backups"], ["Backups", "Database", "Database"]
        )
        self.assertEqual(groups["Retire or purge"], ["Retire", "Purge"])
        self.assertIsNone(group_of("Deploy"))

    def test_a_group_of_one_is_shown_as_the_action_itself(self) -> None:
        from platform_automation.operator.console import Action, menu_entries

        entries = menu_entries(
            [Action("Deploy", "x"), Action("Validate manifest and Compose", "y")]
        )
        self.assertEqual(
            [e.label for e in entries], ["Deploy", "Validate manifest and Compose"]
        )


class WiredHostActionsTest(unittest.TestCase):
    def test_backup_and_convergence_actions_run_when_prompts_exist(self) -> None:
        from types import SimpleNamespace

        from platform_automation.operator.console import host_actions
        from platform_automation.operator.context import Host

        context = SimpleNamespace(root=Path("/nonexistent"), core_pin="v0.0.0")
        host = Host("platform-host-1", "platform-host-1.tailnet.example.net", "ops")
        wired = {a.label: a for a in host_actions(context, host, prompts=(None, None))}
        for label in (
            "Backups: take one now",
            "Backups: prove restorable",
            "Converge (twice)",
            "Readiness",
        ):
            self.assertIsNotNone(wired[label].run, label)
            self.assertTrue(wired[label].remote, label)
        self.assertIn("--limit platform-host-1", wired["Converge (twice)"].command)
        # without prompts the console only shows what it would run
        shown = {a.label: a for a in host_actions(context, host)}
        self.assertIsNone(shown["Backups: prove restorable"].run)

    def test_a_verification_says_what_was_proven_and_what_was_not_touched(self) -> None:
        from platform_automation.operator.console import render_verification

        text = render_verification(
            {
                "outcome": "succeeded",
                "stamp": "20260101T000000Z-aaaaaaaa",
                "query": "SELECT 1",
                "result": "1",
                "verified_at": "2026-01-01T00:01:00Z",
            }
        )
        self.assertIn("restore succeeded", text)
        self.assertIn("\033[32m", text)
        self.assertIn("20260101T000000Z", text)
        self.assertIn("SELECT 1", text)
        self.assertIn("live database was not touched", text)
        failed = render_verification({"outcome": "failed"})
        self.assertIn("restore failed", failed)
        self.assertIn("\033[31m", failed)

    def test_a_backup_result_reports_a_failed_offsite_copy(self) -> None:
        from platform_automation.operator.console import render_backup_result

        text = render_backup_result(
            {
                "path": "/var/backups/x.dump.age",
                "bytes": 10,
                "release_id": "r1",
                "removed_backups": ["old"],
                "warnings": ["w"],
                "offsite": {"state": "failed", "error": "denied"},
            }
        )
        self.assertIn("dump written", text)
        self.assertIn("removed by retention  1", text)
        self.assertIn("FAILED — denied", text)
        self.assertIn(
            "stays on this host",
            render_backup_result({"offsite": {"state": "not-configured"}}),
        )
