import unittest

from platform_automation.operator.rollback import (
    blocked_by_deploying,
    eligible_targets,
    migrations_between,
    version_of,
)


def entry(
    tag,
    status="deployed",
    health="succeeded",
    migration="not_required",
    at="2026-09-14T10:00:00Z",
):
    return {
        "release_tag": tag,
        "status": status,
        "healthcheck": health,
        "migration": migration,
        "updated_at": at,
    }


DOCUMENT = {
    "current": entry("lab-v0.1.9"),
    "history": [  # newest first, as the host sends it
        entry("lab-v0.1.9", migration="succeeded"),
        entry("lab-v0.1.8", status="failed", health="failed"),
        entry("lab-v0.1.8"),
        entry("lab-v0.1.7", migration="succeeded"),
        entry("lab-v0.1.6"),
        entry("lab-hotfix", status="rolled_back"),
    ],
}


class RollbackTargetsTest(unittest.TestCase):
    def test_only_successful_earlier_releases_are_offered_once_each(self) -> None:
        tags = [t.release_tag for t in eligible_targets(DOCUMENT)]
        self.assertEqual(tags, ["lab-v0.1.8", "lab-v0.1.7", "lab-v0.1.6"])

    def test_migrations_after_the_target_are_named(self) -> None:
        self.assertEqual(migrations_between(DOCUMENT, "lab-v0.1.8"), ["lab-v0.1.9"])
        self.assertEqual(
            migrations_between(DOCUMENT, "lab-v0.1.6"), ["lab-v0.1.7", "lab-v0.1.9"]
        )
        self.assertEqual(migrations_between(DOCUMENT, "nope"), [])

    def test_a_deploying_release_blocks(self) -> None:
        self.assertFalse(blocked_by_deploying(DOCUMENT))
        stuck = {"history": [entry("x", status="deploying", health="pending")]}
        self.assertTrue(blocked_by_deploying(stuck))

    def test_version_is_taken_from_the_tag_tail(self) -> None:
        self.assertEqual(version_of("lab-v0.1.7"), "v0.1.7")
        self.assertEqual(version_of("production-v2.0.0"), "v2.0.0")
        self.assertIsNone(version_of("lab-abc1234"))
