import unittest

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
