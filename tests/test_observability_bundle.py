import copy
import json
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path

import yaml

from platform_automation.build_bundle import BundleError, create_bundle
from platform_automation.observability_bundle import (
    API_VERSION,
    BUNDLE_PATH,
    MAX_DASHBOARDS,
    MAX_DOCUMENT_BYTES,
    ObservabilityBundleError,
    collect_document,
    dashboard_errors,
    dashboard_uid,
    document_errors,
    load_document,
    provisioned,
)
from platform_automation.stage_bundle import stage_verified_bundle
from platform_automation.verify_bundle import (
    BundleVerificationError,
    validate_bundle_members,
    verify_bundle,
)

FIXTURE = Path(__file__).parent / "fixtures" / "app-contract"
DASHBOARD = {
    "id": 17,
    "uid": "whatever-the-author-had",
    "title": "Orders",
    "editable": True,
    "tags": ["orders"],
    "annotations": {
        "list": [{"datasource": {"type": "grafana", "uid": "-- Grafana --"}}]
    },
    "panels": [
        {
            "title": "Orders per second",
            "datasource": {"type": "prometheus", "uid": "platform-prometheus"},
            "targets": [
                {
                    "datasource": {"type": "prometheus", "uid": "platform-prometheus"},
                    "expr": "rate(example_orders_total[5m])",
                }
            ],
        },
        {
            "title": "Errors",
            "datasource": {"type": "loki", "uid": "platform-loki"},
            "targets": [{"expr": '{project="example"} |= "error"'}],
        },
    ],
}


class DashboardRulesTest(unittest.TestCase):
    def test_a_dashboard_on_the_platforms_sources_is_accepted(self) -> None:
        self.assertEqual(dashboard_errors("orders", DASHBOARD), [])

    def test_a_foreign_data_source_is_named_with_its_place(self) -> None:
        dashboard = copy.deepcopy(DASHBOARD)
        dashboard["panels"][0]["targets"][0]["datasource"]["uid"] = "my-own-prometheus"

        errors = dashboard_errors("orders", dashboard)

        self.assertEqual(len(errors), 1)
        self.assertIn("$.panels[0].targets[0].datasource", errors[0])
        self.assertIn("my-own-prometheus", errors[0])
        self.assertIn("platform-prometheus", errors[0])

    def test_an_export_for_sharing_is_refused_with_the_way_out(self) -> None:
        dashboard = copy.deepcopy(DASHBOARD)
        dashboard["__inputs"] = [{"name": "DS_PROMETHEUS"}]
        dashboard["panels"][0]["datasource"] = {"uid": "${DS_PROMETHEUS}"}

        errors = dashboard_errors("orders", dashboard)

        self.assertTrue(any("sharing externally" in error for error in errors))
        self.assertTrue(any("placeholder" in error for error in errors))

    def test_a_source_named_only_by_type_is_refused(self) -> None:
        dashboard = copy.deepcopy(DASHBOARD)
        dashboard["panels"][0]["datasource"] = {"type": "prometheus"}

        self.assertIn("by uid", dashboard_errors("orders", dashboard)[0])

    def test_shape_and_name_are_checked(self) -> None:
        self.assertTrue(dashboard_errors("Orders", DASHBOARD))
        self.assertTrue(dashboard_errors("orders", []))
        self.assertTrue(dashboard_errors("orders", {"panels": []}))
        self.assertTrue(dashboard_errors("orders", {"title": "x"}))

    def test_the_platform_chooses_the_uid_and_locks_the_dashboard(self) -> None:
        prepared = provisioned("example", "lab", "orders", DASHBOARD)

        self.assertEqual(prepared["uid"], "example-lab-orders")
        self.assertIsNone(prepared["id"])
        self.assertIs(prepared["editable"], False)
        self.assertEqual(prepared["tags"], ["orders", "example", "lab"])
        self.assertEqual(prepared["title"], "Orders")
        # the source document is not changed in place
        self.assertEqual(DASHBOARD["uid"], "whatever-the-author-had")

    def test_a_long_name_still_gets_a_unique_uid_grafana_accepts(self) -> None:
        one = dashboard_uid(
            "a-rather-long-project-name", "production", "orders-by-region"
        )
        two = dashboard_uid(
            "a-rather-long-project-name", "production", "orders-by-reason"
        )

        self.assertLessEqual(len(one), 40)
        self.assertLessEqual(len(two), 40)
        self.assertNotEqual(one, two)

    def test_the_document_has_one_shape(self) -> None:
        good = {"api_version": API_VERSION, "dashboards": {"orders": DASHBOARD}}
        self.assertEqual(document_errors(good), [])
        self.assertTrue(document_errors({**good, "extra": 1}))
        self.assertTrue(document_errors({**good, "api_version": "other/v1"}))
        self.assertTrue(document_errors({**good, "dashboards": {}}))
        many = {f"d{i}": DASHBOARD for i in range(MAX_DASHBOARDS + 1)}
        self.assertTrue(document_errors({**good, "dashboards": many}))
        with self.assertRaises(ObservabilityBundleError):
            load_document(b"not json")


class BundleRoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.app = self.base / "app"
        shutil.copytree(FIXTURE, self.app)
        self.manifest_path = self.app / "deploy/platform.yml"
        self.bundle = self.base / "bundle.tar.gz"

    def declare(self, directory="deploy/dashboards", dashboards=None) -> None:
        manifest = yaml.safe_load(self.manifest_path.read_text())
        manifest["observability"] = {"dashboards": directory}
        self.manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        target = self.app / directory
        target.mkdir(parents=True, exist_ok=True)
        for name, content in (dashboards or {"orders": DASHBOARD}).items():
            (target / f"{name}.json").write_text(json.dumps(content))

    def members(self) -> dict:
        with tarfile.open(self.bundle, mode="r:gz") as archive:
            return {
                member.name: archive.extractfile(member).read()
                for member in archive.getmembers()
            }

    def test_without_a_declaration_the_bundle_is_what_it_always_was(self) -> None:
        create_bundle(self.manifest_path, self.bundle)

        self.assertEqual(len(self.members()), 4)
        self.assertNotIn("observability", verify_bundle(self.bundle).metadata["files"])

    def test_dashboards_travel_as_one_verified_file_and_are_staged(self) -> None:
        self.declare(dashboards={"orders": DASHBOARD, "queue": DASHBOARD})
        create_bundle(self.manifest_path, self.bundle)

        members = self.members()
        self.assertEqual(len(members), 5)
        document = json.loads(members[BUNDLE_PATH])
        self.assertEqual(sorted(document["dashboards"]), ["orders", "queue"])

        verified = verify_bundle(self.bundle)
        self.assertEqual(
            verified.metadata["files"]["observability"]["path"], BUNDLE_PATH
        )
        staged = stage_verified_bundle(verified, self.base / "releases")
        self.assertEqual(json.loads((staged / BUNDLE_PATH).read_text()), document)
        self.assertEqual((staged / BUNDLE_PATH).stat().st_mode & 0o777, 0o600)

    def test_the_same_dashboards_make_the_same_bundle(self) -> None:
        self.declare()
        first = create_bundle(self.manifest_path, self.bundle)
        second = create_bundle(self.manifest_path, self.base / "again.tar.gz")

        self.assertEqual(first, second)

    def test_a_bad_dashboard_fails_the_build_with_its_name(self) -> None:
        bad = copy.deepcopy(DASHBOARD)
        bad["panels"][0]["datasource"]["uid"] = "elsewhere"
        self.declare(dashboards={"orders": bad})

        with self.assertRaisesRegex(BundleError, "dashboard orders"):
            create_bundle(self.manifest_path, self.bundle)

    def test_a_declared_directory_must_exist_and_hold_only_json(self) -> None:
        manifest = yaml.safe_load(self.manifest_path.read_text())
        manifest["observability"] = {"dashboards": "deploy/dashboards"}
        self.manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
        with self.assertRaisesRegex(BundleError, "not a directory"):
            create_bundle(self.manifest_path, self.bundle)

        self.declare()
        (self.app / "deploy/dashboards/notes.txt").write_text("hello")
        with self.assertRaisesRegex(BundleError, "only regular"):
            create_bundle(self.manifest_path, self.bundle)

    def test_a_directory_outside_the_application_is_refused(self) -> None:
        for directory in ("../elsewhere", "/etc", "deploy/../.."):
            manifest = yaml.safe_load(self.manifest_path.read_text())
            manifest["observability"] = {"dashboards": directory}
            self.manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
            with self.assertRaises(BundleError, msg=directory):
                create_bundle(self.manifest_path, self.bundle)

    def test_a_symlinked_dashboard_is_refused(self) -> None:
        self.declare()
        outside = self.base / "outside.json"
        outside.write_text(json.dumps(DASHBOARD))
        (self.app / "deploy/dashboards/linked.json").symlink_to(outside)

        with self.assertRaisesRegex(BundleError, "only regular"):
            create_bundle(self.manifest_path, self.bundle)

    def test_the_host_refuses_a_declaration_without_the_file_and_the_reverse(
        self,
    ) -> None:
        self.declare()
        create_bundle(self.manifest_path, self.bundle)
        members = self.members()
        metadata = json.loads(members["platform-bundle.json"])

        # the file is dropped, the manifest still declares dashboards
        without = dict(members)
        del without[BUNDLE_PATH]
        stripped = copy.deepcopy(metadata)
        del stripped["files"]["observability"]
        without["platform-bundle.json"] = json.dumps(stripped).encode()
        with self.assertRaisesRegex(BundleVerificationError, "must come together"):
            validate_bundle_members(without)

        # the file is tampered with after the build
        tampered = dict(members)
        tampered[BUNDLE_PATH] = members[BUNDLE_PATH] + b" "
        with self.assertRaisesRegex(BundleVerificationError, "SHA-256 mismatch"):
            validate_bundle_members(tampered)

    def test_the_host_validates_the_dashboards_itself(self) -> None:
        self.declare()
        create_bundle(self.manifest_path, self.bundle)
        members = self.members()
        metadata = json.loads(members["platform-bundle.json"])
        forged = json.loads(members[BUNDLE_PATH])
        forged["dashboards"]["orders"]["panels"][0]["datasource"]["uid"] = "elsewhere"
        content = json.dumps(forged).encode()
        import hashlib

        metadata["files"]["observability"]["sha256"] = hashlib.sha256(
            content
        ).hexdigest()
        members[BUNDLE_PATH] = content
        members["platform-bundle.json"] = json.dumps(metadata).encode()

        with self.assertRaisesRegex(BundleVerificationError, "unknown data source"):
            validate_bundle_members(members)

    def test_doctor_reports_dashboards_before_any_deploy(self) -> None:
        from platform_automation.operator.doctor import manifest_findings
        from platform_automation.validate_manifest import load_json
        from platform_automation.build_bundle import DEFAULT_MANIFEST_SCHEMA

        schema = load_json(DEFAULT_MANIFEST_SCHEMA)
        relative = Path("deploy/platform.yml")

        self.declare(dashboards={"orders": DASHBOARD, "queue": DASHBOARD})
        findings = {f.title: f for f in manifest_findings(self.app, relative, schema)}
        good = findings["deploy/platform.yml: dashboards"]
        self.assertFalse(good.failed)
        self.assertIn("2 in deploy/dashboards", good.detail)

        bad = copy.deepcopy(DASHBOARD)
        bad["panels"][1]["datasource"]["uid"] = "elsewhere"
        (self.app / "deploy/dashboards/queue.json").write_text(json.dumps(bad))
        findings = {f.title: f for f in manifest_findings(self.app, relative, schema)}
        broken = findings["deploy/platform.yml: dashboards"]
        self.assertTrue(broken.failed)
        self.assertIn("dashboard queue", broken.detail)
        self.assertEqual(broken.anchor, "#/ref-observability")

    def test_doctor_says_nothing_when_no_dashboards_are_declared(self) -> None:
        from platform_automation.operator.doctor import manifest_findings
        from platform_automation.validate_manifest import load_json
        from platform_automation.build_bundle import DEFAULT_MANIFEST_SCHEMA

        findings = manifest_findings(
            self.app, Path("deploy/platform.yml"), load_json(DEFAULT_MANIFEST_SCHEMA)
        )
        self.assertFalse([f for f in findings if "dashboards" in f.title])

    def test_the_build_never_makes_a_file_the_host_would_refuse(self) -> None:
        from platform_automation.verify_bundle import MAX_MEMBER_BYTES

        self.assertLess(MAX_DOCUMENT_BYTES, MAX_MEMBER_BYTES)
        # each within its own limit, together beyond the limit for all
        heavy = copy.deepcopy(DASHBOARD)
        heavy["description"] = "x" * (480 * 1024)
        self.declare(dashboards={f"d{i}": heavy for i in range(10)})

        with self.assertRaisesRegex(BundleError, "limit for all of them"):
            create_bundle(self.manifest_path, self.bundle)

        # and what does fit is accepted by the host's verifier
        self.declare(dashboards={f"d{i}": heavy for i in range(7)})
        for extra in ("d7", "d8", "d9"):
            (self.app / f"deploy/dashboards/{extra}.json").unlink()
        create_bundle(self.manifest_path, self.bundle)
        self.assertEqual(len(verify_bundle(self.bundle).files), 4)

    def test_collecting_skips_hidden_files(self) -> None:
        self.declare()
        (self.app / "deploy/dashboards/.gitkeep").write_text("")

        document = json.loads(collect_document(self.app, "deploy/dashboards"))

        self.assertEqual(list(document["dashboards"]), ["orders"])


if __name__ == "__main__":
    unittest.main()
