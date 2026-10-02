import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from platform_automation.metrics_export import (
    Samples,
    collect,
    parse_percent,
    parse_size,
    parse_stamp,
    scope_labels,
)

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
