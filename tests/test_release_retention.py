import json
import tempfile
import unittest
from pathlib import Path


from platform_automation.release_retention import (  # noqa: E402
    DEFAULT_RETAINED_IMAGES,
    ReleaseRetentionError,
    apply_retention,
    discover_ledger_scopes,
    plan_retention,
    protected_image_digests,
    resolve_retained_images,
    retained_image_digests,
)

REPOSITORY = "ghcr.io/example/platform-example"


def digest(marker: str) -> str:
    return "sha256:" + (marker * 64)


def bundle_digest(marker: str) -> str:
    return marker * 64


def build_record(
    release_id: str,
    timestamp: str,
    image_marker: str,
    bundle_marker: str = "a",
    project: str = "example",
    environment: str = "lab",
    status: str = "deployed",
    healthcheck: str = "succeeded",
) -> dict:
    image_digest = digest(image_marker)

    return {
        "api_version": "platform-release/v1",
        "release_id": release_id,
        "project": project,
        "environment": environment,
        "release_tag": f"v-{image_marker}",
        "image": {
            "reference": f"{REPOSITORY}@{image_digest}",
            "repository": REPOSITORY,
            "digest": image_digest,
        },
        "bundle": {
            "digest": bundle_digest(bundle_marker),
            "relative_path": (
                f"{project}/{environment}/bundles/{bundle_digest(bundle_marker)}"
            ),
        },
        "status": status,
        "created_at": timestamp,
        "updated_at": timestamp,
        "previous_release_id": None,
        "rollback_of_release_id": None,
        "migration": {
            "status": "not_required",
            "completed_at": None,
            "error": None,
        },
        "healthcheck": {
            "status": healthcheck,
            "completed_at": timestamp,
            "error": None,
        },
    }


class RetainedImageSelectionTest(unittest.TestCase):
    def test_counts_distinct_digests_not_deployments(self) -> None:
        records = [
            build_record("1" * 32, "2026-08-01T10:00:00.000Z", "a"),
            build_record("2" * 32, "2026-08-01T11:00:00.000Z", "b"),
            build_record("3" * 32, "2026-08-01T12:00:00.000Z", "b"),
            build_record("4" * 32, "2026-08-01T13:00:00.000Z", "b"),
        ]

        self.assertEqual(
            retained_image_digests(records, 2),
            [digest("b"), digest("a")],
        )

    def test_ignores_unsuccessful_releases(self) -> None:
        records = [
            build_record("1" * 32, "2026-08-01T10:00:00.000Z", "a"),
            build_record(
                "2" * 32,
                "2026-08-01T11:00:00.000Z",
                "b",
                status="failed",
                healthcheck="failed",
            ),
        ]

        self.assertEqual(
            retained_image_digests(records, 5),
            [digest("a")],
        )

    def test_always_keeps_the_running_release(self) -> None:
        records = [
            build_record("1" * 32, "2026-08-01T10:00:00.000Z", "a"),
            build_record("2" * 32, "2026-08-01T11:00:00.000Z", "b"),
        ]

        self.assertIn(digest("b"), retained_image_digests(records, 1))

    def test_millisecond_precision_orders_same_second_releases(self) -> None:
        records = [
            build_record("1" * 32, "2026-08-01T10:00:00.100Z", "a"),
            build_record("2" * 32, "2026-08-01T10:00:00.900Z", "b"),
        ]

        self.assertEqual(
            retained_image_digests(records, 1),
            [digest("b")],
        )


class RetentionPlanTest(unittest.TestCase):
    def test_keeps_bundles_of_retained_images_only(self) -> None:
        records = [
            build_record("1" * 32, "2026-08-01T10:00:00.000Z", "a", "a"),
            build_record("2" * 32, "2026-08-01T11:00:00.000Z", "b", "b"),
        ]

        plan = plan_retention(records, 1, {digest("b")}, "2" * 32)

        self.assertEqual(
            plan["retained_bundle_digests"],
            {bundle_digest("b")},
        )
        self.assertEqual(
            plan["removable_images"],
            [f"{REPOSITORY}@{digest('a')}"],
        )

    def test_protected_digests_are_never_removable(self) -> None:
        records = [
            build_record("1" * 32, "2026-08-01T10:00:00.000Z", "a", "a"),
            build_record("2" * 32, "2026-08-01T11:00:00.000Z", "b", "b"),
        ]

        plan = plan_retention(
            records,
            1,
            {digest("a"), digest("b")},
            "2" * 32,
        )

        self.assertEqual(plan["removable_images"], [])


class RetainedReleasesSettingTest(unittest.TestCase):
    def test_defaults_when_unset(self) -> None:
        self.assertEqual(
            resolve_retained_images({"deployment": {}}),
            DEFAULT_RETAINED_IMAGES,
        )

    def test_rejects_boolean_and_out_of_range_values(self) -> None:
        for value in (True, 0, -1, 51, "3"):
            with self.subTest(value=value):
                with self.assertRaises(ReleaseRetentionError):
                    resolve_retained_images(
                        {"deployment": {"retained_releases": value}}
                    )

    def test_accepts_a_configured_depth(self) -> None:
        self.assertEqual(
            resolve_retained_images({"deployment": {"retained_releases": 2}}),
            2,
        )


class RetentionFilesystemTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.projects_root = self.base / "projects"
        self.releases_root = self.base / "releases"
        self.runtime_secrets_root = self.base / "runtime-secrets"
        self.docker_executable = self.base / "docker"
        self.removed_images: list[str] = []

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def remove_image(self, image: str, docker_executable: Path) -> None:
        self.removed_images.append(image)

    def write_ledger(
        self,
        project: str,
        environment: str,
        records: list[dict],
    ) -> None:
        ledger = self.projects_root / project / environment / "ledger"
        ledger.mkdir(mode=0o700, parents=True, exist_ok=True)

        for record in records:
            path = ledger / f"{record['release_id']}.json"
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            path.chmod(0o600)

    def make_bundle(self, project: str, environment: str, marker: str) -> Path:
        path = (
            self.releases_root
            / project
            / environment
            / "bundles"
            / bundle_digest(marker)
        )
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        (path / "platform.yml").write_text("{}", encoding="utf-8")

        return path

    def make_runtime_secrets(
        self,
        project: str,
        environment: str,
        release_id: str,
    ) -> Path:
        path = self.runtime_secrets_root / project / environment / release_id
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        (path / "app.env").write_text("APP_SECRET=x\n", encoding="utf-8")

        return path

    def test_reclaims_superseded_artefacts(self) -> None:
        records = [
            build_record("1" * 32, "2026-08-01T10:00:00.000Z", "a", "a"),
            build_record("2" * 32, "2026-08-01T11:00:00.000Z", "b", "b"),
        ]
        self.write_ledger("example", "lab", records)
        old_bundle = self.make_bundle("example", "lab", "a")
        current_bundle = self.make_bundle("example", "lab", "b")
        old_secrets = self.make_runtime_secrets("example", "lab", "1" * 32)
        current_secrets = self.make_runtime_secrets("example", "lab", "2" * 32)

        result = apply_retention(
            records=records,
            project="example",
            environment="lab",
            current_release_id="2" * 32,
            retained_images=1,
            projects_root=self.projects_root,
            releases_root=self.releases_root,
            runtime_secrets_root=self.runtime_secrets_root,
            docker_executable=self.docker_executable,
            image_remover=self.remove_image,
        )

        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["removed_bundles"], [bundle_digest("a")])
        self.assertEqual(result["removed_runtime_secrets"], ["1" * 32])
        self.assertEqual(
            result["removed_images"],
            [f"{REPOSITORY}@{digest('a')}"],
        )
        self.assertFalse(old_bundle.exists())
        self.assertTrue(current_bundle.is_dir())
        self.assertFalse(old_secrets.exists())
        self.assertTrue(current_secrets.is_dir())

    def test_decrypted_secrets_survive_only_for_the_running_release(self) -> None:
        records = [
            build_record("1" * 32, "2026-08-01T10:00:00.000Z", "a", "a"),
            build_record("2" * 32, "2026-08-01T11:00:00.000Z", "b", "b"),
        ]
        self.write_ledger("example", "lab", records)
        self.make_bundle("example", "lab", "a")
        self.make_bundle("example", "lab", "b")
        rollback_secrets = self.make_runtime_secrets("example", "lab", "1" * 32)

        result = apply_retention(
            records=records,
            project="example",
            environment="lab",
            current_release_id="2" * 32,
            retained_images=5,
            projects_root=self.projects_root,
            releases_root=self.releases_root,
            runtime_secrets_root=self.runtime_secrets_root,
            docker_executable=self.docker_executable,
            image_remover=self.remove_image,
        )

        # The bundle stays so an offline rollback still works, but the
        # decrypted copy is re-materialised from it rather than kept.
        self.assertEqual(result["removed_bundles"], [])
        self.assertFalse(rollback_secrets.exists())

    def test_another_environment_protects_a_shared_image(self) -> None:
        lab_records = [
            build_record("1" * 32, "2026-08-01T10:00:00.000Z", "a", "a"),
            build_record("2" * 32, "2026-08-01T11:00:00.000Z", "b", "b"),
        ]
        production_records = [
            build_record(
                "3" * 32,
                "2026-08-01T09:00:00.000Z",
                "a",
                "a",
                environment="production",
            ),
        ]
        self.write_ledger("example", "lab", lab_records)
        self.write_ledger("example", "production", production_records)
        self.make_bundle("example", "lab", "a")
        self.make_bundle("example", "lab", "b")

        result = apply_retention(
            records=lab_records,
            project="example",
            environment="lab",
            current_release_id="2" * 32,
            retained_images=1,
            projects_root=self.projects_root,
            releases_root=self.releases_root,
            runtime_secrets_root=self.runtime_secrets_root,
            docker_executable=self.docker_executable,
            image_remover=self.remove_image,
        )

        self.assertEqual(result["removed_images"], [])
        self.assertEqual(self.removed_images, [])
        # The staged bundle is still reclaimed: it belongs to this scope only.
        self.assertEqual(result["removed_bundles"], [bundle_digest("a")])

    def test_image_removal_failure_becomes_a_warning(self) -> None:
        def refuse(image: str, docker_executable: Path) -> None:
            raise ReleaseRetentionError("Docker image removal failed")

        records = [
            build_record("1" * 32, "2026-08-01T10:00:00.000Z", "a", "a"),
            build_record("2" * 32, "2026-08-01T11:00:00.000Z", "b", "b"),
        ]
        self.write_ledger("example", "lab", records)

        result = apply_retention(
            records=records,
            project="example",
            environment="lab",
            current_release_id="2" * 32,
            retained_images=1,
            projects_root=self.projects_root,
            releases_root=self.releases_root,
            runtime_secrets_root=self.runtime_secrets_root,
            docker_executable=self.docker_executable,
            image_remover=refuse,
        )

        self.assertEqual(result["removed_images"], [])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("Docker image removal failed", result["warnings"][0])

    def test_unrelated_directories_are_left_alone(self) -> None:
        records = [build_record("1" * 32, "2026-08-01T10:00:00.000Z", "a", "a")]
        self.write_ledger("example", "lab", records)
        self.make_bundle("example", "lab", "a")

        stray = self.releases_root / "example" / "lab" / "bundles" / "scratch"
        stray.mkdir(mode=0o700, parents=True)

        result = apply_retention(
            records=records,
            project="example",
            environment="lab",
            current_release_id="1" * 32,
            retained_images=1,
            projects_root=self.projects_root,
            releases_root=self.releases_root,
            runtime_secrets_root=self.runtime_secrets_root,
            docker_executable=self.docker_executable,
            image_remover=self.remove_image,
        )

        self.assertEqual(result["removed_bundles"], [])
        self.assertTrue(stray.is_dir())

    def test_scope_discovery_skips_invalid_directories(self) -> None:
        self.write_ledger(
            "example",
            "lab",
            [build_record("1" * 32, "2026-08-01T10:00:00.000Z", "a")],
        )
        (self.projects_root / "example" / "not-an-environment").mkdir(mode=0o700)
        (self.projects_root / "Invalid_Project").mkdir(mode=0o700)

        self.assertEqual(
            discover_ledger_scopes(self.projects_root),
            [("example", "lab")],
        )

    def test_protected_digests_span_every_ledger(self) -> None:
        self.write_ledger(
            "example",
            "lab",
            [build_record("1" * 32, "2026-08-01T10:00:00.000Z", "a")],
        )
        self.write_ledger(
            "other",
            "production",
            [
                build_record(
                    "2" * 32,
                    "2026-08-01T10:00:00.000Z",
                    "e",
                    project="other",
                    environment="production",
                )
            ],
        )

        self.assertEqual(
            protected_image_digests(self.projects_root, "example", "lab", 1),
            {digest("a"), digest("e")},
        )


if __name__ == "__main__":
    unittest.main()
