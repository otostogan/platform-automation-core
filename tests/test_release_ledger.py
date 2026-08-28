import copy
import json
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


EXAMPLE_MANIFEST = (
    Path(__file__).parent / "fixtures" / "app-contract" / "deploy" / "platform.yml"
)


from platform_automation.build_bundle import create_bundle  # noqa: E402
from platform_automation.deployment_request import load_deployment_request  # noqa: E402
from platform_automation.operation_lock import (  # noqa: E402
    OperationAlreadyRunningError,
    project_environment_lock,
)
from platform_automation.reboot_recovery import (  # noqa: E402
    RebootRecoveryError,
    recover_project_environment_secrets,
    restore_release_secrets,
    select_recovery_release,
)
from platform_automation.release_ledger import (  # noqa: E402
    ReleaseLedgerError,
    build_prepared_release,
    create_private_ledger_directory,
    build_rollback_release,
    resolve_release_bundle,
    write_release_record,
    find_latest_deployed_release,
    load_release_bundle,
    list_release_records,
    replace_release_record,
)
from platform_automation.runtime_secrets import RuntimeSecretsError  # noqa: E402
from platform_automation.stage_bundle import stage_verified_bundle  # noqa: E402

IMAGE_DIGEST = "sha256:" + ("a" * 64)
IMAGE = f"ghcr.io/example/platform-example@{IMAGE_DIGEST}"
RELEASE_ID = "1" * 32
TIMESTAMP = "2026-08-25T12:00:00Z"


class ReleaseLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.bundle_path = self.base / "bundle.tar.gz"
        self.releases_root = self.base / "releases"
        self.projects_root = self.base / "projects"

        create_bundle(
            EXAMPLE_MANIFEST,
            self.bundle_path,
        )
        self.request = load_deployment_request(
            bundle_path=self.bundle_path,
            project="example",
            environment="lab",
            image=IMAGE,
            release_tag="v1.2.3",
        )
        self.staged_bundle = stage_verified_bundle(
            self.request.bundle,
            self.releases_root,
        )

    def test_project_recovery_with_busy_lock_has_no_side_effects(self):
        lock_root = self.base / "locks"
        runtime_root = self.base / "runtime-secrets"

        with project_environment_lock(
            lock_root,
            "example",
            "lab",
            "deploy",
        ):
            with patch(
                "platform_automation.reboot_recovery.list_release_records"
            ) as list_records:
                with patch(
                    "platform_automation.reboot_recovery.restore_release_secrets"
                ) as restore:
                    with self.assertRaises(OperationAlreadyRunningError):
                        recover_project_environment_secrets(
                            project="example",
                            environment="lab",
                            projects_root=self.projects_root,
                            releases_root=self.releases_root,
                            lock_root=lock_root,
                            runtime_root=runtime_root,
                            age_key_file=self.base / "age.key",
                            sops_executable=self.base / "sops",
                        )

        list_records.assert_not_called()
        restore.assert_not_called()
        self.assertFalse(self.projects_root.exists())
        self.assertFalse(runtime_root.exists())

    def test_project_recovery_with_empty_ledger_does_nothing(self):
        with patch(
            "platform_automation.reboot_recovery.restore_release_secrets"
        ) as restore:
            result = recover_project_environment_secrets(
                project="example",
                environment="lab",
                projects_root=self.projects_root,
                releases_root=self.releases_root,
                lock_root=self.base / "locks",
                runtime_root=self.base / "runtime-secrets",
                age_key_file=self.base / "age.key",
                sops_executable=self.base / "sops",
            )

        self.assertIsNone(result)
        restore.assert_not_called()
        self.assertFalse(self.projects_root.exists())

    def test_project_recovery_restores_selected_release(self):
        record = self.build_deployed_record(RELEASE_ID, TIMESTAMP)
        write_release_record(self.projects_root, record)
        expected_path = (
            self.base / "runtime-secrets" / "example" / "lab" / RELEASE_ID / "app.env"
        )

        with patch(
            "platform_automation.reboot_recovery.restore_release_secrets",
            return_value=expected_path,
        ) as restore:
            result = recover_project_environment_secrets(
                project="example",
                environment="lab",
                projects_root=self.projects_root,
                releases_root=self.releases_root,
                lock_root=self.base / "locks",
                runtime_root=self.base / "runtime-secrets",
                age_key_file=self.base / "age.key",
                sops_executable=self.base / "sops",
            )

        self.assertEqual(result, expected_path)
        restore.assert_called_once_with(
            record=record,
            releases_root=self.releases_root,
            runtime_root=self.base / "runtime-secrets",
            age_key_file=self.base / "age.key",
            sops_executable=self.base / "sops",
            minimum_age_recipients=1,
        )

    def test_recovery_without_successful_release(self):
        self.assertIsNone(select_recovery_release([]))
        self.assertIsNone(select_recovery_release([self.build_record()]))

        for status in ("failed", "rolled_back"):
            with self.subTest(status=status):
                record = self.build_record()
                record["status"] = status

                with self.assertRaisesRegex(
                    RebootRecoveryError,
                    "no successful release",
                ):
                    select_recovery_release([record])

    def test_project_recovery_rejects_age_key_failure(self):
        record = self.build_deployed_record(RELEASE_ID, TIMESTAMP)
        record_path = write_release_record(self.projects_root, record)
        original_record = record_path.read_bytes()
        runtime_root = self.base / "runtime-secrets"
        lock_root = self.base / "locks"

        with patch(
            "platform_automation.runtime_secrets.decrypt_sops_document",
            side_effect=RuntimeSecretsError("SOPS decryption failed"),
        ):
            with self.assertRaisesRegex(
                RuntimeSecretsError,
                "SOPS decryption failed",
            ):
                recover_project_environment_secrets(
                    project="example",
                    environment="lab",
                    projects_root=self.projects_root,
                    releases_root=self.releases_root,
                    lock_root=lock_root,
                    runtime_root=runtime_root,
                    age_key_file=self.base / "missing-age.key",
                    sops_executable=self.base / "sops",
                )

        self.assertEqual(record_path.read_bytes(), original_record)
        self.assertFalse(runtime_root.exists())
        self.assertEqual(
            (lock_root / "example-lab.lock").read_bytes(),
            b"",
        )

    def test_recovery_selects_latest_successful_release(self):
        older = self.build_deployed_record(
            "2" * 32,
            "2026-08-25T11:00:00Z",
        )
        current = self.build_deployed_record(RELEASE_ID, TIMESTAMP)

        prepared = self.build_record()
        prepared["release_id"] = "3" * 32
        prepared["updated_at"] = "2026-08-25T13:00:00Z"

        failed = copy.deepcopy(older)
        failed["release_id"] = "4" * 32
        failed["status"] = "failed"

        records = [prepared, current, failed, older]
        original = copy.deepcopy(records)

        self.assertIs(select_recovery_release(records), current)
        self.assertEqual(records, original)

    def test_recovery_rejects_unresolved_operations(self):
        current = self.build_deployed_record(RELEASE_ID, TIMESTAMP)

        for status in ("deploying", "failed", "rolled_back"):
            with self.subTest(status=status):
                attempt = self.build_record()
                attempt["release_id"] = "3" * 32
                attempt["status"] = status
                attempt["updated_at"] = "2026-08-25T13:00:00Z"
                attempt["previous_release_id"] = None

                with self.assertRaises(RebootRecoveryError):
                    select_recovery_release([current, attempt])

    def test_recovery_accepts_confirmed_automatic_rollback(self):
        current = self.build_deployed_record(RELEASE_ID, TIMESTAMP)
        attempt = self.build_record()
        attempt["release_id"] = "3" * 32
        attempt["status"] = "rolled_back"
        attempt["updated_at"] = "2026-08-25T13:00:00Z"
        attempt["previous_release_id"] = current["release_id"]
        attempt["healthcheck"]["status"] = "failed"

        self.assertIs(
            select_recovery_release([attempt, current]),
            current,
        )

    def test_recovery_rejects_ambiguous_release_order(self):
        first = self.build_deployed_record(RELEASE_ID, TIMESTAMP)
        second = self.build_deployed_record("2" * 32, TIMESTAMP)

        with self.assertRaisesRegex(
            RebootRecoveryError,
            "ambiguous release order",
        ):
            select_recovery_release([first, second])

    def test_recovery_rejects_unsuccessful_healthcheck(self):
        record = self.build_deployed_record(RELEASE_ID, TIMESTAMP)
        record["healthcheck"]["status"] = "failed"

        with self.assertRaisesRegex(
            RebootRecoveryError,
            "no successful healthcheck",
        ):
            select_recovery_release([record])

    def test_restores_missing_runtime_secrets(self):
        record = self.build_deployed_record(RELEASE_ID, TIMESTAMP)
        original = copy.deepcopy(record)
        runtime_root = self.base / "runtime-secrets"

        with patch(
            "platform_automation.runtime_secrets.decrypt_sops_document",
            return_value={"APP_SECRET": "test-only-value"},
        ):
            destination = restore_release_secrets(
                record=record,
                releases_root=self.releases_root,
                runtime_root=runtime_root,
                age_key_file=self.base / "age.key",
                sops_executable=self.base / "sops",
            )

        self.assertEqual(
            destination,
            runtime_root / "example" / "lab" / RELEASE_ID / "app.env",
        )
        self.assertEqual(
            destination.read_text(encoding="utf-8"),
            'APP_SECRET="test-only-value"\n',
        )
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(destination.parent.stat().st_mode),
            0o700,
        )
        self.assertEqual(record, original)
        self.assertFalse(self.projects_root.exists())

    def test_secret_recovery_rejects_unfinished_release(self):
        with patch(
            "platform_automation.runtime_secrets.decrypt_sops_document"
        ) as decrypt:
            with self.assertRaisesRegex(
                RebootRecoveryError,
                "successfully deployed",
            ):
                restore_release_secrets(
                    record=self.build_record(),
                    releases_root=self.releases_root,
                    runtime_root=self.base / "runtime-secrets",
                    age_key_file=self.base / "age.key",
                    sops_executable=self.base / "sops",
                )

            decrypt.assert_not_called()

    def test_secret_recovery_rejects_corrupted_bundle(self):
        record = self.build_deployed_record(RELEASE_ID, TIMESTAMP)
        compose_path = (
            self.staged_bundle
            / self.request.bundle.metadata["files"]["compose"]["path"]
        )
        compose_path.write_bytes(b"corrupted\n")

        with patch(
            "platform_automation.runtime_secrets.decrypt_sops_document"
        ) as decrypt:
            with self.assertRaisesRegex(
                ReleaseLedgerError,
                "SHA-256 mismatch",
            ):
                restore_release_secrets(
                    record=record,
                    releases_root=self.releases_root,
                    runtime_root=self.base / "runtime-secrets",
                    age_key_file=self.base / "age.key",
                    sops_executable=self.base / "sops",
                )

            decrypt.assert_not_called()

    def test_loads_saved_release_bundle(self):
        bundle = load_release_bundle(
            self.build_record(),
            self.releases_root,
        )

        self.assertEqual(bundle, self.request.bundle)

    def test_rejects_corrupted_saved_bundle(self):
        compose_path = (
            self.staged_bundle
            / self.request.bundle.metadata["files"]["compose"]["path"]
        )
        compose_path.write_bytes(compose_path.read_bytes() + b"\n# unexpected change\n")

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "SHA-256 mismatch",
        ):
            load_release_bundle(
                self.build_record(),
                self.releases_root,
            )

    def test_rejects_unsafe_saved_file_permissions(self):
        compose_path = (
            self.staged_bundle
            / self.request.bundle.metadata["files"]["compose"]["path"]
        )
        compose_path.chmod(0o644)

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "unsafe permissions",
        ):
            load_release_bundle(
                self.build_record(),
                self.releases_root,
            )

    def test_rejects_symlink_inside_saved_bundle(self):
        link = self.staged_bundle / "unexpected-link"
        link.symlink_to(self.bundle_path)

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "symbolic link",
        ):
            load_release_bundle(
                self.build_record(),
                self.releases_root,
            )

    def test_resolves_release_bundle(self):
        result = resolve_release_bundle(
            self.build_record(),
            self.releases_root,
        )

        self.assertEqual(result, self.staged_bundle.resolve())

    def test_rejects_bundle_path_identity_mismatch(self):
        record = self.build_record()
        record["bundle"][
            "relative_path"
        ] = f"example/production/bundles/{record['bundle']['digest']}"

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "does not match release identity",
        ):
            resolve_release_bundle(record, self.releases_root)

    def test_rejects_missing_release_bundle(self):
        record = self.build_record()
        missing_digest = "b" * 64
        record["bundle"] = {
            "digest": missing_digest,
            "relative_path": f"example/lab/bundles/{missing_digest}",
        }

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "directory does not exist",
        ):
            resolve_release_bundle(record, self.releases_root)

    def test_rejects_symlink_in_release_bundle_path(self):
        record = self.build_record()
        project_directory = self.releases_root / "example"
        relocated_directory = self.base / "relocated-example"

        project_directory.rename(relocated_directory)
        project_directory.symlink_to(
            relocated_directory,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "symbolic link",
        ):
            resolve_release_bundle(record, self.releases_root)

    def test_builds_rollback_without_changing_target(self):
        target = self.build_deployed_record("1" * 32, TIMESTAMP)
        current = self.build_deployed_record("2" * 32, TIMESTAMP)
        original = copy.deepcopy(target)

        rollback = build_rollback_release(target, current)

        self.assertEqual(target, original)
        self.assertNotEqual(rollback["release_id"], target["release_id"])
        self.assertNotEqual(rollback["release_id"], current["release_id"])
        self.assertEqual(
            rollback["release_tag"],
            f"rollback-{rollback['release_id']}",
        )
        self.assertEqual(rollback["image"], target["image"])
        self.assertEqual(rollback["bundle"], target["bundle"])
        self.assertIsNot(rollback["image"], target["image"])
        self.assertIsNot(rollback["bundle"], target["bundle"])
        self.assertEqual(
            rollback["previous_release_id"],
            current["release_id"],
        )
        self.assertEqual(
            rollback["rollback_of_release_id"],
            target["release_id"],
        )
        self.assertEqual(rollback["status"], "prepared")
        self.assertEqual(rollback["migration"]["status"], "not_required")
        self.assertEqual(rollback["healthcheck"]["status"], "pending")
        self.assertIsNone(rollback["healthcheck"]["completed_at"])

    def test_rejects_rollback_across_environments(self):
        target = self.build_deployed_record("1" * 32, TIMESTAMP)
        current = self.build_deployed_record("2" * 32, TIMESTAMP)
        current["environment"] = "production"

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "same project/environment",
        ):
            build_rollback_release(target, current)

    def test_rejects_unsuccessful_rollback_records(self):
        for invalid_record in ("target", "current"):
            with self.subTest(invalid_record=invalid_record):
                target = self.build_deployed_record("1" * 32, TIMESTAMP)
                current = self.build_deployed_record("2" * 32, TIMESTAMP)
                records = {"target": target, "current": current}
                records[invalid_record]["healthcheck"]["status"] = "failed"

                with self.assertRaisesRegex(
                    ReleaseLedgerError,
                    "requires successful",
                ):
                    build_rollback_release(target, current)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def build_record(self) -> dict:
        return build_prepared_release(
            request=self.request,
            staged_bundle_path=self.staged_bundle,
            releases_root=self.releases_root,
            previous_release_id="2" * 32,
            release_id=RELEASE_ID,
            timestamp=TIMESTAMP,
        )

    def build_deployed_record(
        self,
        release_id: str,
        timestamp: str,
    ) -> dict:
        record = build_prepared_release(
            request=self.request,
            staged_bundle_path=self.staged_bundle,
            releases_root=self.releases_root,
            release_id=release_id,
            timestamp=timestamp,
        )

        record["status"] = "deployed"
        record["updated_at"] = timestamp

        if record["migration"]["status"] == "pending":
            record["migration"] = {
                "status": "succeeded",
                "completed_at": timestamp,
                "error": None,
            }

        record["healthcheck"] = {
            "status": "succeeded",
            "completed_at": timestamp,
            "error": None,
        }

        return record

    def test_builds_prepared_release_record(self) -> None:
        record = self.build_record()

        self.assertEqual(
            record["api_version"],
            "platform-release/v1",
        )
        self.assertEqual(record["release_id"], RELEASE_ID)
        self.assertEqual(record["release_tag"], "v1.2.3")
        self.assertEqual(record["status"], "prepared")
        self.assertEqual(record["created_at"], TIMESTAMP)
        self.assertEqual(record["updated_at"], TIMESTAMP)
        self.assertEqual(
            record["previous_release_id"],
            "2" * 32,
        )
        self.assertIsNone(record["rollback_of_release_id"])

        self.assertEqual(
            record["image"],
            {
                "reference": IMAGE,
                "repository": "ghcr.io/example/platform-example",
                "digest": IMAGE_DIGEST,
            },
        )
        self.assertEqual(
            record["bundle"]["digest"],
            self.request.bundle.digest,
        )
        self.assertEqual(
            record["bundle"]["relative_path"],
            ("example/lab/bundles/" f"{self.request.bundle.digest}"),
        )
        self.assertEqual(
            record["migration"]["status"],
            "pending",
        )
        self.assertEqual(
            record["healthcheck"]["status"],
            "pending",
        )

    def test_marks_missing_migration_as_not_required(self) -> None:
        manifest = copy.deepcopy(self.request.bundle.manifest)
        manifest["deployment"] = {}

        bundle = replace(
            self.request.bundle,
            manifest=manifest,
        )
        request = replace(
            self.request,
            bundle=bundle,
        )

        record = build_prepared_release(
            request=request,
            staged_bundle_path=self.staged_bundle,
            releases_root=self.releases_root,
            release_id=RELEASE_ID,
            timestamp=TIMESTAMP,
        )

        self.assertEqual(
            record["migration"]["status"],
            "not_required",
        )

    def test_writes_private_atomic_record(self) -> None:
        record = self.build_record()
        record_path = write_release_record(
            self.projects_root,
            record,
        )

        self.assertEqual(
            json.loads(record_path.read_text(encoding="utf-8")),
            record,
        )
        self.assertEqual(
            stat.S_IMODE(record_path.stat().st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(record_path.parent.stat().st_mode),
            0o700,
        )
        self.assertEqual(
            list(record_path.parent.glob("*.tmp")),
            [],
        )

    def test_rejects_duplicate_release_id(self) -> None:
        record = self.build_record()

        write_release_record(
            self.projects_root,
            record,
        )

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "release record already exists",
        ):
            write_release_record(
                self.projects_root,
                record,
            )

    def test_atomically_replaces_release_state(self) -> None:
        record = self.build_record()
        write_release_record(self.projects_root, record)
        updated = copy.deepcopy(record)
        updated["status"] = "deploying"
        updated["updated_at"] = "2026-08-25T12:01:00Z"

        record_path = replace_release_record(
            self.projects_root,
            updated,
        )

        self.assertEqual(
            json.loads(record_path.read_text(encoding="utf-8")),
            updated,
        )
        self.assertEqual(stat.S_IMODE(record_path.stat().st_mode), 0o600)
        self.assertEqual(list(record_path.parent.glob("*.tmp")), [])

    def test_rejects_release_identity_change(self) -> None:
        record = self.build_record()
        write_release_record(self.projects_root, record)
        changed = copy.deepcopy(record)
        changed["release_tag"] = "different"

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "release record identity changed",
        ):
            replace_release_record(self.projects_root, changed)

    def test_rejects_bundle_outside_releases_root(self) -> None:
        outside_bundle = self.base / "outside-bundle"
        outside_bundle.mkdir()

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "escapes releases root",
        ):
            build_prepared_release(
                request=self.request,
                staged_bundle_path=outside_bundle,
                releases_root=self.releases_root,
            )

    def test_rejects_symbolic_link_bundle_path(self) -> None:
        bundle_link = self.base / "bundle-link"
        bundle_link.symlink_to(
            self.staged_bundle,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "cannot be a symbolic link",
        ):
            build_prepared_release(
                request=self.request,
                staged_bundle_path=bundle_link,
                releases_root=self.releases_root,
            )

    def test_rejects_invalid_release_record(self) -> None:
        record = self.build_record()
        record["image"]["digest"] = "latest"

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "invalid release record",
        ):
            write_release_record(
                self.projects_root,
                record,
            )

    def test_rejects_symbolic_link_ledger_path(self) -> None:
        project_directory = self.projects_root / "example"
        project_directory.mkdir(parents=True)

        protected_directory = self.base / "protected"
        protected_directory.mkdir()

        environment_link = project_directory / "lab"
        environment_link.symlink_to(
            protected_directory,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "ledger path cannot contain a symbolic link",
        ):
            create_private_ledger_directory(
                self.projects_root,
                "example",
                "lab",
            )

        self.assertEqual(
            list(protected_directory.iterdir()),
            [],
        )

    def test_lists_records_and_finds_latest_deployed(self) -> None:
        first = self.build_deployed_record(
            "3" * 32,
            "2026-08-25T12:00:00Z",
        )
        second = self.build_deployed_record(
            "4" * 32,
            "2026-08-25T13:00:00Z",
        )

        write_release_record(self.projects_root, second)
        write_release_record(self.projects_root, first)

        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )

        self.assertEqual(
            [record["release_id"] for record in records],
            ["3" * 32, "4" * 32],
        )
        self.assertEqual(
            find_latest_deployed_release(records)["release_id"],
            "4" * 32,
        )

    def test_missing_ledger_returns_empty_list(self) -> None:
        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )

        self.assertEqual(records, [])
        self.assertIsNone(find_latest_deployed_release(records))

    def test_rejects_corrupted_release_record(self) -> None:
        ledger_directory = create_private_ledger_directory(
            self.projects_root,
            "example",
            "lab",
        )
        corrupted = ledger_directory / f"{'5' * 32}.json"
        corrupted.write_text("{invalid json", encoding="utf-8")
        corrupted.chmod(0o600)

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "release record is not valid JSON",
        ):
            list_release_records(
                self.projects_root,
                "example",
                "lab",
            )

    def test_rejects_symbolic_link_release_record(self) -> None:
        record = self.build_record()
        record_path = write_release_record(
            self.projects_root,
            record,
        )

        link = record_path.parent / f"{'6' * 32}.json"
        link.symlink_to(record_path)

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "release record cannot be a symbolic link",
        ):
            list_release_records(
                self.projects_root,
                "example",
                "lab",
            )

    def test_rejects_unsafe_record_permissions(self) -> None:
        record = self.build_record()
        record_path = write_release_record(
            self.projects_root,
            record,
        )
        record_path.chmod(0o644)

        with self.assertRaisesRegex(
            ReleaseLedgerError,
            "release record has unsafe permissions",
        ):
            list_release_records(
                self.projects_root,
                "example",
                "lab",
            )


if __name__ == "__main__":
    unittest.main()
