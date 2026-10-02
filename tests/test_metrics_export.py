import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from platform_automation import metrics_export
from platform_automation.metrics_export import (
    Samples,
    collect,
    parse_percent,
    parse_size,
    parse_stamp,
    release_samples,
    scope_labels,
)
from platform_automation.release_ledger import ReleaseLedgerError

PS = (
    "aaa|my-app-lab-web-1|my-app-lab|web|running\n"
    "bbb|platform-db-my-app-lab-postgres-1|platform-db-my-app-lab|postgres|exited\n"
    "ccc|platform-nginx|platform-proxy|nginx|running\n"
)
STATS = "\n".join(
    json.dumps(row)
    for row in (
        {
            "ID": "aaa",
            "CPUPerc": "3.10%",
            "MemUsage": "103.8MiB / 512MiB",
            "NetIO": "1.43MB / 324kB",
        },
        {
            "ID": "ccc",
            "CPUPerc": "0.00%",
            "MemUsage": "7.3MiB / 15.62GiB",
            "NetIO": "0B / 0B",
        },
        {"ID": "zzz", "CPUPerc": "9%", "MemUsage": "1MiB / 2MiB", "NetIO": "0B / 0B"},
    )
)


def runner(command, **_):
    if "ps" in command:
        out = PS
    elif "inspect" in command:
        out = "aaa|2\nbbb|0\nccc|0\n"
    elif "stats" in command:
        out = STATS
    else:
        raise AssertionError(command)
    return subprocess.CompletedProcess(command, 0, out.encode(), b"")


class MetricsExportTest(unittest.TestCase):
    def test_scope_labels_follow_the_platform_naming(self) -> None:
        self.assertEqual(
            scope_labels("my-app-lab", "web"),
            {"project": "my-app", "environment": "lab", "service": "web"},
        )
        self.assertEqual(
            scope_labels("platform-db-my-app-production", "postgres"),
            {"project": "my-app", "environment": "production", "service": "postgres"},
        )
        self.assertEqual(scope_labels("platform-proxy", "nginx")["project"], "platform")
        self.assertEqual(scope_labels("", "x")["project"], "")

    def test_docker_units_are_parsed(self) -> None:
        self.assertEqual(parse_size("103.8MiB"), 103.8 * 1024**2)
        self.assertEqual(parse_size(" 1.43MB "), 1.43 * 1000**2)
        self.assertEqual(parse_size("15.62GiB"), 15.62 * 1024**3)
        self.assertIsNone(parse_size("--"))
        self.assertEqual(parse_percent("3.10%"), 3.1)
        self.assertEqual(parse_stamp("20260914T133613Z-schedule"), 1789392973.0)
        self.assertIsNone(parse_stamp("garbage"))

    def test_containers_are_reported_under_their_scope_and_strangers_ignored(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            none = Path(directory) / "none"
            text = collect(
                Path("/usr/bin/docker"), none, none, none, runner=runner, now=1.0
            )

        self.assertIn(
            'platform_container_up{container="my-app-lab-web-1",environment="lab",project="my-app",service="web"} 1',
            text,
        )
        self.assertIn(
            'platform_container_up{container="platform-db-my-app-lab-postgres-1",environment="lab",project="my-app",service="postgres"} 0',
            text,
        )
        self.assertIn(
            'platform_container_restarts_total{container="my-app-lab-web-1",environment="lab",project="my-app",service="web"} 2',
            text,
        )
        self.assertIn(
            'platform_container_cpu_percent{container="my-app-lab-web-1",environment="lab",project="my-app",service="web"} 3.1',
            text,
        )
        self.assertIn("platform_container_memory_limit_bytes", text)
        self.assertIn(
            'platform_container_up{container="platform-nginx",project="platform",service="nginx"} 1',
            text,
        )
        self.assertNotIn("zzz", text)
        self.assertIn("platform_ledger_readable 1", text)
        self.assertIn("# TYPE platform_container_restarts_total counter", text)

    def test_label_values_are_escaped(self) -> None:
        samples = Samples()
        samples.add("m", "gauge", "h", {"a": 'x"y\\z'}, 1)
        self.assertIn('m{a="x\\"y\\\\z"} 1', samples.render())


class ReleaseSamplesTest(unittest.TestCase):
    def render(self, records, manifest) -> str:
        """Two scopes: ``good`` reads through ``records``, ``bad`` cannot be read."""

        def listing(root, project, environment):
            if project == "bad":
                raise ReleaseLedgerError("corrupt record")
            return records

        samples = Samples()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.multiple(
                metrics_export,
                list_project_scopes=lambda root: [("bad", "lab"), ("good", "lab")],
                list_release_records=listing,
                is_retired=lambda *_: False,
                resolve_release_bundle=lambda record, root: Path(directory),
                load_staged_manifest=lambda bundle: manifest,
            ),
        ):
            none = Path(directory) / "none"
            release_samples(samples, none, none, none)
        return samples.render()

    RECORD = {
        "release_tag": "lab-v1.0.0",
        "status": "deployed",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "release_id": "20260101T000000Z-aaaaaaaa",
    }

    def test_one_unreadable_scope_is_reported_beside_a_healthy_walk(self) -> None:
        text = self.render([], {})

        self.assertIn("platform_ledger_readable 1", text)
        self.assertIn(
            'platform_ledger_readable{environment="lab",project="bad"} 0', text
        )
        self.assertIn(
            'platform_ledger_readable{environment="lab",project="good"} 1', text
        )

    def test_a_release_that_asks_for_dumps_says_so_even_with_none_taken(self) -> None:
        manifest = {
            "database": {"mode": "docker", "backup_enabled": True},
            "domains": [],
        }
        text = self.render([self.RECORD], manifest)

        self.assertIn(
            'platform_backup_scheduled{environment="lab",project="good"} 1', text
        )
        self.assertIn('platform_backup_count{environment="lab",project="good"} 0', text)
        self.assertNotIn("platform_backup_latest_timestamp_seconds{", text)

    def test_a_release_without_scheduled_dumps_says_that_too(self) -> None:
        manifest = {"database": {"mode": "docker"}, "domains": []}
        text = self.render([self.RECORD], manifest)

        self.assertIn(
            'platform_backup_scheduled{environment="lab",project="good"} 0', text
        )

    def test_a_restore_the_runtime_recorded_as_proven_counts_as_proven(self) -> None:
        # The word comes from the runtime that writes the log, not from here.
        from platform_automation.restore_runtime import VERIFICATION_SUCCEEDED

        proven = {
            "outcome": VERIFICATION_SUCCEEDED,
            "stamp": "20260101T000000Z-aaaaaaaa",
        }
        with mock.patch.object(metrics_export, "last_verification", lambda _: proven):
            text = self.render([], {})
        self.assertIn(
            'platform_backup_restore_proven{environment="lab",project="good"} 1', text
        )
        self.assertIn("platform_backup_verified_dump_timestamp_seconds{", text)

        failed = {"outcome": "failed", "stamp": "20260101T000000Z-aaaaaaaa"}
        with mock.patch.object(metrics_export, "last_verification", lambda _: failed):
            text = self.render([], {})
        self.assertIn(
            'platform_backup_restore_proven{environment="lab",project="good"} 0', text
        )
