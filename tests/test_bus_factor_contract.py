import contextlib
import io
import unittest
from pathlib import Path

import yaml

from platform_automation.platform_cli import parse_arguments


ROOT = Path(__file__).resolve().parents[1]


class BusFactorContractTest(unittest.TestCase):
    def test_host_wrapper_pins_sops_recipient_policy(self) -> None:
        defaults = yaml.safe_load(
            (ROOT / "roles/platform_cli/defaults/main.yml").read_text(encoding="utf-8")
        )
        wrapper = (ROOT / "roles/platform_cli/templates/platform.j2").read_text(
            encoding="utf-8"
        )

        self.assertEqual(defaults["platform_cli_minimum_age_recipients"], 1)
        self.assertIn(
            "--minimum-age-recipients {{ platform_cli_minimum_age_recipients }}",
            wrapper,
        )

    def test_consumer_examples_enable_two_recipient_policy(self) -> None:
        host_vars = yaml.safe_load(
            (
                ROOT / "examples/consumer/company-infra/inventory/group_vars/"
                "platform_hosts.example.yml"
            ).read_text(encoding="utf-8")
        )
        workflow = (
            ROOT / "examples/consumer/application/.github/workflows/deploy.yml"
        ).read_text(encoding="utf-8")

        self.assertEqual(host_vars["platform_cli_minimum_age_recipients"], 2)
        self.assertIn("minimum_age_recipients: 2", workflow)

    def test_runbook_covers_handoff_stop_conditions(self) -> None:
        runbook = (ROOT / "docs/runbook.md").read_text(encoding="utf-8")

        for required_text in (
            "recovery private key",
            "Bootstrap a new host",
            "Normal deployment",
            "Rollback",
            "stuck in `deploying`",
            "`access_guard` blocks convergence",
            "Reboot acceptance",
            "Quarterly bus-factor drill",
        ):
            self.assertIn(required_text, runbook)

    def test_caller_arguments_cannot_lower_the_host_policy(self) -> None:
        deploy = [
            "--project",
            "example",
            "--environment",
            "lab",
            "--bundle",
            "bundle.tar.gz",
            "--image",
            "ghcr.io/example/app@sha256:" + "0" * 64,
            "--release-tag",
            "r1",
        ]

        def minimum(*policy_arguments: str) -> int:
            return parse_arguments(
                [*policy_arguments, "deploy", *deploy]
            ).minimum_age_recipients

        self.assertEqual(minimum("--minimum-age-recipients", "2"), 2)
        self.assertEqual(
            minimum(
                "--minimum-age-recipients",
                "2",
                "--minimum-age-recipients",
                "1",
            ),
            2,
        )
        self.assertEqual(
            minimum(
                "--minimum-age-recipients",
                "2",
                "--minimum-age-recipients",
                "3",
            ),
            3,
        )

    def test_policy_rejects_non_positive_values(self) -> None:
        for value in ("0", "-1", "abc"):
            with self.subTest(value=value):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parse_arguments(
                            ["--minimum-age-recipients", value, "status"]
                            + ["--project", "example", "--environment", "lab"]
                        )


if __name__ == "__main__":
    unittest.main()
