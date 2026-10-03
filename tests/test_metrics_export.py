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
    edge_addresses,
    release_samples,
    scope_labels,
    scrape_targets,
    staged_dashboards,
    sync_dashboards,
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


class ValuePrecisionTest(unittest.TestCase):
    def test_a_timestamp_keeps_every_digit(self) -> None:
        samples = Samples()
        samples.add("t", "gauge", "h", {}, 1790943672.0)
        samples.add("f", "gauge", "h", {"a": "b"}, 1790943672.25)
        samples.add("p", "gauge", "h", {}, 3.1)
        text = samples.render()

        self.assertIn("t 1790943672\n", text)
        self.assertIn('f{a="b"} 1790943672.25\n', text)
        self.assertIn("p 3.1\n", text)
        self.assertNotIn("e+", text)

    def test_the_export_stamp_is_the_clock_not_a_rounding_of_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            none = Path(directory) / "none"
            text = collect(
                Path("/usr/bin/docker"),
                none,
                none,
                none,
                runner=runner,
                now=1790943672.0,
            )
        self.assertIn("platform_metrics_export_timestamp_seconds 1790943672\n", text)


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


class ApplicationMetricsTest(unittest.TestCase):
    SERVICE = {
        "web": "web",
        "internal_port": 3000,
        "metrics": {"path": "/metrics", "port": 9464},
    }
    CONTAINERS = [
        {
            "project": "shop",
            "environment": "lab",
            "service": "web",
            "container": "shop-lab-web-2",
            "address": "192.0.2.12",
        },
        {
            "project": "shop",
            "environment": "lab",
            "service": "web",
            "container": "shop-lab-web-1",
            "address": "192.0.2.11",
        },
        {
            "project": "shop",
            "environment": "lab",
            "service": "worker",
            "container": "shop-lab-worker-1",
            "address": "192.0.2.13",
        },
        {
            "project": "other",
            "environment": "lab",
            "service": "web",
            "container": "other-lab-web-1",
            "address": "192.0.2.20",
        },
    ]

    def test_every_web_container_of_a_declaring_release_is_a_target(self) -> None:
        targets = scrape_targets({("shop", "lab"): self.SERVICE}, self.CONTAINERS)

        self.assertEqual(
            [target["targets"] for target in targets],
            [["192.0.2.11:9464"], ["192.0.2.12:9464"]],
        )
        self.assertEqual(
            targets[0]["labels"],
            {
                "__metrics_path__": "/metrics",
                "project": "shop",
                "environment": "lab",
                "service": "web",
                "container": "shop-lab-web-1",
            },
        )

    def test_nothing_declared_means_nothing_scraped(self) -> None:
        self.assertEqual(scrape_targets({}, self.CONTAINERS), [])

    def test_a_declaration_reaches_the_metric_and_the_target_list(self) -> None:
        manifest = {
            "service": self.SERVICE,
            "database": {"mode": "docker"},
            "domains": [],
        }
        declared = {}
        samples = Samples()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.multiple(
                metrics_export,
                list_project_scopes=lambda root: [("shop", "lab")],
                list_release_records=lambda *_: [ReleaseSamplesTest.RECORD],
                is_retired=lambda *_: False,
                resolve_release_bundle=lambda record, root: Path(directory),
                load_staged_manifest=lambda bundle: manifest,
            ),
        ):
            none = Path(directory) / "none"
            release_samples(samples, none, none, none, declared)

        self.assertIn(
            'platform_application_metrics_declared{environment="lab",project="shop",service="web"} 1',
            samples.render(),
        )
        self.assertEqual(declared, {("shop", "lab"): self.SERVICE})

    def test_addresses_come_from_the_proxys_network_only(self) -> None:
        def docker(command, **_):
            if "ps" in command:
                self.assertIn("network=platform-edge", command)
                self.assertIn("status=running", command)
                out = "aaa|shop-lab-web-1|shop-lab|web\nbbb|platform-nginx|platform-proxy|nginx\n"
            else:
                self.assertIn('"platform-edge"', command[command.index("--format") + 1])
                out = "aaa|192.0.2.11\nbbb|\n"
            return subprocess.CompletedProcess(command, 0, out.encode(), b"")

        found = edge_addresses(Path("/usr/bin/docker"), "platform-edge", docker)

        self.assertEqual(
            found,
            [
                {
                    "project": "shop",
                    "environment": "lab",
                    "service": "web",
                    "container": "shop-lab-web-1",
                    "address": "192.0.2.11",
                }
            ],
        )

    def test_the_target_file_is_emptied_when_no_release_declares_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            targets = base / "targets/applications.json"
            targets.parent.mkdir()
            targets.write_text('[{"targets": ["192.0.2.1:1"]}]')
            with mock.patch.object(metrics_export.subprocess, "run", runner):
                code = metrics_export.main(
                    [
                        "--output",
                        str(base / "platform.prom"),
                        "--targets",
                        str(targets),
                        "--projects-root",
                        str(base / "none"),
                        "--releases-root",
                        str(base / "none"),
                        "--backups-root",
                        str(base / "none"),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(targets.read_text()), [])


class ApplicationDashboardsTest(unittest.TestCase):
    DASHBOARD = {
        "title": "Orders",
        "uid": "authors-own",
        "panels": [
            {
                "title": "p",
                "datasource": {"type": "prometheus", "uid": "platform-prometheus"},
            }
        ],
    }

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "dashboards"

    def tree(self) -> dict:
        return {
            str(path.relative_to(self.root)): path.read_text()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def test_the_directory_becomes_exactly_what_is_wanted(self) -> None:
        sync_dashboards(
            self.root, {"shop-lab": {"orders.json": "{}\n", "queue.json": "[]\n"}}
        )
        self.assertEqual(
            self.tree(), {"shop-lab/orders.json": "{}\n", "shop-lab/queue.json": "[]\n"}
        )
        self.assertEqual((self.root / "shop-lab").stat().st_mode & 0o777, 0o755)
        self.assertEqual(
            (self.root / "shop-lab/orders.json").stat().st_mode & 0o777, 0o644
        )

        changed = sync_dashboards(self.root, {"shop-lab": {"orders.json": "{}\n"}})
        self.assertEqual(changed, ["removed shop-lab/queue.json"])
        self.assertEqual(self.tree(), {"shop-lab/orders.json": "{}\n"})

    def test_an_unchanged_dashboard_is_not_rewritten(self) -> None:
        wanted = {"shop-lab": {"orders.json": "{}\n"}}
        sync_dashboards(self.root, wanted)
        before = (self.root / "shop-lab/orders.json").stat().st_mtime_ns

        self.assertEqual(sync_dashboards(self.root, wanted), [])
        self.assertEqual(
            (self.root / "shop-lab/orders.json").stat().st_mtime_ns, before
        )

    def test_an_application_that_is_gone_takes_its_folder_with_it(self) -> None:
        sync_dashboards(
            self.root, {"shop-lab": {"a.json": "1"}, "blog-lab": {"b.json": "2"}}
        )

        sync_dashboards(self.root, {"blog-lab": {"b.json": "2"}})

        self.assertEqual(self.tree(), {"blog-lab/b.json": "2"})
        self.assertFalse((self.root / "shop-lab").exists())

    def test_a_folder_marked_untouched_survives_a_failed_read(self) -> None:
        sync_dashboards(self.root, {"shop-lab": {"a.json": "1"}})

        sync_dashboards(self.root, {"shop-lab": None})

        self.assertEqual(self.tree(), {"shop-lab/a.json": "1"})

    def test_strangers_in_the_directory_are_removed(self) -> None:
        sync_dashboards(self.root, {"shop-lab": {"a.json": "1"}})
        (self.root / "stray.json").write_text("x")
        (self.root / "shop-lab/nested").mkdir()
        (self.root / "shop-lab/nested/deep.json").write_text("x")
        outside = Path(self.temporary.name) / "outside.json"
        outside.write_text("keep me")
        (self.root / "shop-lab/link.json").symlink_to(outside)

        sync_dashboards(self.root, {"shop-lab": {"a.json": "1"}})

        self.assertEqual(self.tree(), {"shop-lab/a.json": "1"})
        self.assertEqual(outside.read_text(), "keep me")

    def bundle(self, document) -> Path:
        bundle = Path(self.temporary.name) / "bundle"
        bundle.mkdir(exist_ok=True)
        (bundle / "platform-observability.json").write_text(json.dumps(document))
        return bundle

    def test_staged_dashboards_get_the_platforms_uid(self) -> None:
        bundle = self.bundle(
            {
                "api_version": "platform-observability/v1",
                "dashboards": {"orders": self.DASHBOARD},
            }
        )

        files = staged_dashboards(bundle, "shop", "lab")

        self.assertEqual(list(files), ["orders.json"])
        prepared = json.loads(files["orders.json"])
        self.assertEqual(prepared["uid"], "shop-lab-orders")
        self.assertIs(prepared["editable"], False)

    def walk(self, manifest, retired=False, readable=True):
        """One scope, shop/lab, through release_samples with a staged bundle."""
        bundle = self.bundle(
            {
                "api_version": "platform-observability/v1",
                "dashboards": {"orders": self.DASHBOARD},
            }
        )
        if not readable:
            (bundle / "platform-observability.json").write_text("not json")
        found = {}
        with mock.patch.multiple(
            metrics_export,
            list_project_scopes=lambda root: [("shop", "lab")],
            list_release_records=lambda *_: [ReleaseSamplesTest.RECORD],
            is_retired=lambda *_: retired,
            resolve_release_bundle=lambda record, root: bundle,
            load_staged_manifest=lambda _: manifest,
        ):
            none = Path(self.temporary.name) / "none"
            walked = release_samples(Samples(), none, none, none, None, found)
        return walked, found

    MANIFEST = {
        "observability": {"dashboards": "deploy/dashboards"},
        "service": {"web": "web"},
        "database": {"mode": "docker"},
        "domains": [],
    }

    def test_a_serving_release_that_ships_dashboards_is_collected(self) -> None:
        walked, found = self.walk(self.MANIFEST)

        self.assertTrue(walked)
        self.assertEqual(list(found), ["shop-lab"])
        self.assertEqual(list(found["shop-lab"]), ["orders.json"])

    def test_a_release_without_the_declaration_ships_none(self) -> None:
        manifest = {k: v for k, v in self.MANIFEST.items() if k != "observability"}
        self.assertEqual(self.walk(manifest)[1], {})

    def test_a_retired_application_shows_no_dashboards(self) -> None:
        self.assertEqual(self.walk(self.MANIFEST, retired=True)[1], {})

    def test_an_unreadable_bundle_leaves_the_folder_as_it_is(self) -> None:
        self.assertEqual(
            self.walk(self.MANIFEST, readable=False)[1], {"shop-lab": None}
        )

    def test_an_unwalkable_ledger_changes_no_dashboards_and_no_targets(self) -> None:
        base = Path(self.temporary.name)
        sync_dashboards(self.root, {"shop-lab": {"a.json": "1"}})
        targets = base / "targets.json"
        targets.write_text("[1]")

        def unreadable(_):
            raise ReleaseLedgerError("cannot walk")

        with (
            mock.patch.object(metrics_export, "list_project_scopes", unreadable),
            mock.patch.object(metrics_export.subprocess, "run", runner),
        ):
            code = metrics_export.main(
                [
                    "--output",
                    str(base / "platform.prom"),
                    "--targets",
                    str(targets),
                    "--dashboards",
                    str(self.root),
                    "--projects-root",
                    str(base / "none"),
                    "--releases-root",
                    str(base / "none"),
                    "--backups-root",
                    str(base / "none"),
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(self.tree(), {"shop-lab/a.json": "1"})
        self.assertEqual(targets.read_text(), "[1]")
        self.assertIn(
            "platform_ledger_readable 0", (base / "platform.prom").read_text()
        )
