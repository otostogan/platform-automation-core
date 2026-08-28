import unittest
from pathlib import Path


from platform_automation.platform_cli import (  # noqa: E402
    DeploymentExecutionError,
    select_rollback_release,
)


class RollbackSelectionTest(unittest.TestCase):
    def test_selects_latest_successful_attempt_for_tag(self):
        first = {
            "release_id": "first",
            "release_tag": "v1",
            "status": "deployed",
            "healthcheck": {"status": "succeeded"},
        }
        second = dict(first, release_id="second")
        failed = dict(
            first,
            release_id="failed",
            status="failed",
            healthcheck={"status": "failed"},
        )
        other = dict(first, release_id="other", release_tag="v2")

        selected = select_rollback_release(
            [first, second, failed, other],
            "v1",
        )

        self.assertEqual(selected["release_id"], "second")

    def test_rejects_unknown_tag(self):
        with self.assertRaisesRegex(
            DeploymentExecutionError,
            "tag not found",
        ):
            select_rollback_release([], "unknown")

    def test_rejects_unsuccessful_targets(self):
        for status, health in [
            ("prepared", "pending"),
            ("failed", "failed"),
            ("rolled_back", "failed"),
            ("deployed", "failed"),
        ]:
            with self.subTest(status=status, health=health):
                record = {
                    "release_tag": "v1",
                    "status": status,
                    "healthcheck": {"status": health},
                }

                with self.assertRaisesRegex(
                    DeploymentExecutionError,
                    "no successful deployment",
                ):
                    select_rollback_release([record], "v1")
