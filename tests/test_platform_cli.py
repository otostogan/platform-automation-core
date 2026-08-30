import io
import json
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch


EXAMPLE_MANIFEST = (
    Path(__file__).parent / "fixtures" / "app-contract" / "deploy" / "platform.yml"
)


from platform_automation.build_bundle import create_bundle  # noqa: E402
from platform_automation.compose_runtime import ComposeRuntimeError  # noqa: E402
from platform_automation.deployment_request import (  # noqa: E402
    DeploymentRequestError,
    load_deployment_request,
)
from platform_automation.operation_lock import project_environment_lock  # noqa: E402
from platform_automation.platform_cli import (  # noqa: E402
    load_saved_release_request,
    main,
)
from platform_automation.registry_pull import RegistryPullError  # noqa: E402
from platform_automation.release_ledger import (  # noqa: E402
    build_prepared_release,
    create_private_ledger_directory,
    find_latest_deployed_release,
    list_release_records,
    write_release_record,
    replace_release_record,
)
from platform_automation.runtime_secrets import RuntimeSecretsError  # noqa: E402
from platform_automation.stage_bundle import stage_verified_bundle  # noqa: E402


IMAGE_DIGEST = "sha256:" + ("a" * 64)
IMAGE = f"ghcr.io/example/platform-example@{IMAGE_DIGEST}"


class PlatformCliTest(unittest.TestCase):
    def test_success_is_recorded_before_nginx_lock_is_released(self):
        @contextmanager
        def prepare_and_check(plan):
            yield self
            records = list_release_records(self.projects_root, "example", "lab")
            self.assertEqual(records[-1]["status"], "deployed")

        with patch.object(self, "prepare", prepare_and_check):
            code, _, stderr = self.run_cli(*self.deploy_arguments())
        self.assertEqual(code, 0, stderr)

    def test_unhealthy_candidate_never_activates_nginx(self):
        self.start_error_images.add(IMAGE)
        code, _, _ = self.run_cli(*self.deploy_arguments())
        self.assertEqual(code, 1)
        self.assertNotIn("activate", self.nginx_events)
        self.assertIn("rollback", self.nginx_events)

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.projects_root = self.base / "projects"
        self.releases_root = self.base / "releases"
        self.lock_root = self.base / "locks"
        self.runtime_secrets_root = self.base / "runtime-secrets"
        self.age_key_file = self.base / "age.key"
        self.sops_executable = self.base / "sops"
        self.registry_runtime_root = self.base / "registry"
        self.docker_executable = self.base / "docker"
        self.backups_root = self.base / "backups"
        self.bundle_path = self.base / "bundle.tar.gz"
        self.materialized_secrets: list[dict] = []
        self.pulled_images: list[dict] = []
        self.runtime_events: list[tuple[str, str]] = []
        self.nginx_events: list[str] = []
        self.nginx_build_error = None
        self.nginx_stage_error = None
        self.nginx_activate_error = None
        self.nginx_rollback_error = None
        self.validation_error = None
        self.migration_error = None
        self.start_error_images: set[str] = set()
        self.stop_error = None
        self.removed_images: list[str] = []
        self.database_ensures: list[dict] = []
        self.backup_calls: list[dict] = []
        self.backup_error = None
        self.restore_calls: list[dict] = []
        self.timer_calls: list[dict] = []
        self.timer_error = None
        self.database_password = None
        self.database_error = None
        self.secrets_materializer = self.materialize_secrets
        self.image_puller = self.pull_image
        self.image_remover = self.remove_image
        self.token_stream = io.BytesIO()

        create_bundle(
            EXAMPLE_MANIFEST,
            self.bundle_path,
        )
        self.request = load_deployment_request(
            bundle_path=self.bundle_path,
            project="example",
            environment="lab",
            image=IMAGE,
            release_tag="v1",
        )
        self.staged_bundle = stage_verified_bundle(
            self.request.bundle,
            self.releases_root,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def seed_rollback_releases(self):
        target = self.write_record(
            "1" * 32,
            "2026-08-25T12:00:00Z",
            "v1",
            "deployed",
        )
        current = self.write_record(
            "2" * 32,
            "2026-08-25T13:00:00Z",
            "v2",
            "deployed",
        )

        current_digest = "sha256:" + ("b" * 64)
        current["image"]["digest"] = current_digest
        current["image"][
            "reference"
        ] = f"{current['image']['repository']}@{current_digest}"
        replace_release_record(self.projects_root, current)

        return target, current

    def test_unhealthy_rollback_restores_current_release(self):
        target, current = self.seed_rollback_releases()
        self.start_error_images.add(target["image"]["reference"])

        code, stdout, stderr = self.run_cli(
            "rollback",
            "--project",
            "example",
            "--environment",
            "lab",
            "--to",
            "v1",
            "--json",
        )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("previous release restored", stderr)

        starts = [image for event, image in self.runtime_events if event == "start"]
        self.assertEqual(
            starts,
            [target["image"]["reference"], current["image"]["reference"]],
        )
        self.assertFalse(any(event == "migration" for event, _ in self.runtime_events))

        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )
        attempts = [
            item
            for item in records
            if item["rollback_of_release_id"] == target["release_id"]
        ]

        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "rolled_back")
        self.assertEqual(attempts[0]["healthcheck"]["status"], "failed")
        self.assertEqual(attempts[0]["migration"]["status"], "not_required")

        status_code, status_stdout, status_stderr = self.run_cli(
            "status",
            "--project",
            "example",
            "--environment",
            "lab",
            "--json",
        )
        self.assertEqual(status_code, 0, status_stderr)
        self.assertEqual(
            json.loads(status_stdout)["current"]["release_id"],
            current["release_id"],
        )

    def test_reports_failed_recovery_after_unhealthy_rollback(self):
        target, current = self.seed_rollback_releases()
        self.start_error_images.update(
            {
                target["image"]["reference"],
                current["image"]["reference"],
            }
        )

        code, stdout, stderr = self.run_cli(
            "rollback",
            "--project",
            "example",
            "--environment",
            "lab",
            "--to",
            "v1",
            "--json",
        )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("rollback failed:", stderr)
        self.assertNotIn("previous release restored", stderr)

        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )
        attempt = next(
            item
            for item in records
            if item["rollback_of_release_id"] == target["release_id"]
        )

        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(attempt["healthcheck"]["status"], "failed")

    def test_rollback_respects_deploy_lock(self):
        with project_environment_lock(
            self.lock_root,
            "example",
            "lab",
            "deploy",
        ):
            code, stdout, stderr = self.run_cli(
                "rollback",
                "--project",
                "example",
                "--environment",
                "lab",
                "--to",
                "v1",
                "--json",
            )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("another operation is already running", stderr)
        self.assertEqual(self.pulled_images, [])
        self.assertEqual(self.materialized_secrets, [])
        self.assertEqual(self.runtime_events, [])

    def test_loads_saved_release_request(self):
        record = build_prepared_release(
            request=self.request,
            staged_bundle_path=self.staged_bundle,
            releases_root=self.releases_root,
        )

        request = load_saved_release_request(
            record,
            self.releases_root,
        )

        self.assertEqual(request, self.request)
        self.assertEqual(self.pulled_images, [])
        self.assertEqual(self.runtime_events, [])

    def test_rejects_inconsistent_saved_image_digest(self):
        record = build_prepared_release(
            request=self.request,
            staged_bundle_path=self.staged_bundle,
            releases_root=self.releases_root,
        )
        record["image"]["digest"] = "sha256:" + ("b" * 64)

        with self.assertRaisesRegex(
            DeploymentRequestError,
            "saved image fields do not match",
        ):
            load_saved_release_request(record, self.releases_root)

    def test_rejects_saved_image_from_another_repository(self):
        record = build_prepared_release(
            request=self.request,
            staged_bundle_path=self.staged_bundle,
            releases_root=self.releases_root,
        )
        repository = "ghcr.io/another-company/another-app"
        record["image"]["repository"] = repository
        record["image"]["reference"] = f"{repository}@{IMAGE_DIGEST}"

        with self.assertRaisesRegex(
            DeploymentRequestError,
            "repository does not match application manifest",
        ):
            load_saved_release_request(record, self.releases_root)

    def test_rollback_restores_saved_digest_without_migrations(self):
        target = self.write_record(
            "1" * 32,
            "2026-08-25T12:00:00Z",
            "v1",
            "deployed",
        )
        current = self.write_record(
            "2" * 32,
            "2026-08-25T13:00:00Z",
            "v2",
            "deployed",
        )

        current_digest = "sha256:" + ("b" * 64)
        current["image"]["digest"] = current_digest
        current["image"][
            "reference"
        ] = f"{current['image']['repository']}@{current_digest}"
        replace_release_record(self.projects_root, current)

        code, stdout, stderr = self.run_cli(
            "rollback",
            "--project",
            "example",
            "--environment",
            "lab",
            "--to",
            "v1",
            "--json",
        )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")

        document = json.loads(stdout)
        self.assertEqual(document["operation"], "rollback")
        self.assertEqual(document["status"], "deployed")
        self.assertEqual(document["image"], IMAGE)
        self.assertEqual(document["target_release_id"], target["release_id"])
        self.assertEqual(
            document["previous_release_id"],
            current["release_id"],
        )
        self.assertEqual(self.runtime_events[-1], ("start", IMAGE))
        self.assertFalse(any(event == "migration" for event, _ in self.runtime_events))

        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )
        rollback = next(
            item for item in records if item["release_id"] == document["release_id"]
        )

        self.assertEqual(rollback["migration"]["status"], "not_required")
        self.assertEqual(rollback["healthcheck"]["status"], "succeeded")
        self.assertEqual(
            rollback["rollback_of_release_id"],
            target["release_id"],
        )
        self.assertEqual(
            {item["release_id"] for item in self.materialized_secrets},
            {current["release_id"], rollback["release_id"]},
        )

    def materialize_secrets(
        self,
        encrypted_file: Path,
        project: str,
        environment: str,
        release_id: str,
        runtime_root: Path,
        age_key_file: Path,
        sops_executable: Path,
    ) -> Path:
        destination = runtime_root / project / environment / release_id / "app.env"
        destination.parent.mkdir(
            mode=0o700,
            parents=True,
            exist_ok=True,
        )
        destination.write_text(
            'APP_SECRET="test-only-value"\n',
            encoding="utf-8",
        )
        destination.chmod(0o600)

        self.materialized_secrets.append(
            {
                "encrypted_file": encrypted_file,
                "project": project,
                "environment": environment,
                "release_id": release_id,
                "runtime_root": runtime_root,
                "age_key_file": age_key_file,
                "sops_executable": sops_executable,
                "destination": destination,
            }
        )

        return destination

    def pull_image(
        self,
        image: str,
        registry_username: str,
        registry_token: bytes,
        runtime_root: Path,
        docker_executable: Path,
    ) -> None:
        self.pulled_images.append(
            {
                "image": image,
                "registry_username": registry_username,
                "registry_token": registry_token,
                "runtime_root": runtime_root,
                "docker_executable": docker_executable,
            }
        )

    def remove_image(
        self,
        image: str,
        docker_executable: Path,
    ) -> None:
        self.removed_images.append(image)

    def reconcile_timer(self, **kwargs):
        self.timer_calls.append(kwargs)

        if self.timer_error is not None:
            from platform_automation.backup_schedule import BackupScheduleError

            raise BackupScheduleError(self.timer_error)

        return {
            "unit": "platform-backup@example-lab.timer",
            "state": "enabled",
            "interval_minutes": 360,
        }

    def create_backup(self, **kwargs):
        self.backup_calls.append(kwargs)

        if self.backup_error is not None:
            from platform_automation.backup_runtime import BackupRuntimeError

            raise BackupRuntimeError(self.backup_error)

        return {
            "stamp": "20260829T140530Z-" + kwargs["reason"],
            "reason": kwargs["reason"],
            "path": "/var/backups/platform/example/lab/dump.age",
            "bytes": 4096,
            "recipients": 2,
            "release_id": kwargs["record"]["release_id"],
            "removed_backups": [],
            "warnings": [],
        }

    def ensure_database(self, **kwargs):
        self.database_ensures.append(kwargs)

        if self.database_error is not None:
            from platform_automation.database_runtime import (
                DatabaseRuntimeError,
            )

            raise DatabaseRuntimeError(self.database_error)

        return self.database_password

    def validate_release_compose(self, **kwargs) -> None:
        self.runtime_events.append(("validate", kwargs["image"]))

        if self.validation_error is not None:
            raise ComposeRuntimeError(self.validation_error)

    def run_release_migration(self, **kwargs) -> None:
        self.runtime_events.append(("migration", kwargs["image"]))

        if self.migration_error is not None:
            raise ComposeRuntimeError(self.migration_error)

    def start_release(self, **kwargs) -> None:
        image = kwargs["image"]
        self.runtime_events.append(("start", image))

        if image in self.start_error_images:
            raise ComposeRuntimeError("application is unhealthy")

    def stop_release(self, **kwargs) -> None:
        self.runtime_events.append(("stop", kwargs["image"]))

        if self.stop_error is not None:
            raise ComposeRuntimeError(self.stop_error)

    def load_staged_manifest(self, staged_bundle_path: Path) -> dict:
        self.runtime_events.append(("load_manifest", str(staged_bundle_path)))
        return self.request.bundle.manifest

    def build_plan(self, manifest: dict, release_id: str):
        self.nginx_events.append("build")
        if self.nginx_build_error is not None:
            from platform_automation.nginx_transaction import NginxTransactionError

            raise NginxTransactionError(self.nginx_build_error)
        return {"release_id": release_id, "manifest": manifest}

    @contextmanager
    def prepare(self, plan):
        self.nginx_events.append("prepare")
        yield self

    def stage(self) -> None:
        self.nginx_events.append("stage")
        if self.nginx_stage_error is not None:
            from platform_automation.nginx_transaction import NginxTransactionError

            raise NginxTransactionError(self.nginx_stage_error)

    def activate(self) -> None:
        self.nginx_events.append("activate")
        if self.nginx_activate_error is not None:
            from platform_automation.nginx_transaction import NginxTransactionError

            raise NginxTransactionError(self.nginx_activate_error)

    def rollback(self) -> None:
        self.nginx_events.append("rollback")
        if self.nginx_rollback_error is not None:
            from platform_automation.nginx_transaction import NginxTransactionError

            raise NginxTransactionError(self.nginx_rollback_error)

    def run_cli(
        self,
        *arguments: str,
    ) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(
                list(arguments),
                projects_root=self.projects_root,
                releases_root=self.releases_root,
                lock_root=self.lock_root,
                runtime_secrets_root=self.runtime_secrets_root,
                age_key_file=self.age_key_file,
                sops_executable=self.sops_executable,
                secrets_materializer=self.secrets_materializer,
                registry_runtime_root=self.registry_runtime_root,
                docker_executable=self.docker_executable,
                image_puller=self.image_puller,
                image_remover=self.image_remover,
                database_ensurer=self.ensure_database,
                backup_creator=self.create_backup,
                backups_root=self.backups_root,
                timer_reconciler=self.reconcile_timer,
                systemd_root=self.base / "systemd",
                token_stream=self.token_stream,
                compose_runtime_module=self,
                nginx_manager=self,
            )

        return result, stdout.getvalue(), stderr.getvalue()

    def deploy_arguments(
        self,
        image: str = IMAGE,
        release_tag: str = "v1",
    ) -> tuple[str, ...]:
        return (
            "deploy",
            "--project",
            "example",
            "--environment",
            "lab",
            "--bundle",
            str(self.bundle_path),
            "--image",
            image,
            "--release-tag",
            release_tag,
            "--json",
        )

    def write_record(
        self,
        release_id: str,
        timestamp: str,
        release_tag: str,
        status: str,
    ) -> dict:
        request = replace(
            self.request,
            release_tag=release_tag,
        )
        record = build_prepared_release(
            request=request,
            staged_bundle_path=self.staged_bundle,
            releases_root=self.releases_root,
            release_id=release_id,
            timestamp=timestamp,
        )

        record["status"] = status
        record["updated_at"] = timestamp

        if status == "deployed":
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
        elif status == "failed":
            record["migration"] = {
                "status": "succeeded",
                "completed_at": timestamp,
                "error": None,
            }
            record["healthcheck"] = {
                "status": "failed",
                "completed_at": timestamp,
                "error": "application is unhealthy",
            }

        write_release_record(
            self.projects_root,
            record,
        )

        return record

    def test_status_without_releases(self) -> None:
        code, stdout, stderr = self.run_cli(
            "status",
            "--project",
            "example",
            "--environment",
            "lab",
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Release records: 0", stdout)
        self.assertIn(
            "Current deployed release: none",
            stdout,
        )
        self.assertIn(
            "Latest release attempt: none",
            stdout,
        )

    def test_status_json_without_releases(self) -> None:
        code, stdout, stderr = self.run_cli(
            "status",
            "--project",
            "example",
            "--environment",
            "lab",
            "--json",
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")

        document = json.loads(stdout)

        self.assertEqual(document["release_count"], 0)
        self.assertIsNone(document["current"])
        self.assertIsNone(document["latest"])

    def test_status_shows_deployed_release(self) -> None:
        record = self.write_record(
            "1" * 32,
            "2026-08-25T12:00:00Z",
            "v1",
            "deployed",
        )

        code, stdout, stderr = self.run_cli(
            "status",
            "--project",
            "example",
            "--environment",
            "lab",
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn(
            f"release_id: {record['release_id']}",
            stdout,
        )
        self.assertIn("release_tag: v1", stdout)
        self.assertIn("status: deployed", stdout)

    def test_failed_attempt_does_not_replace_current(self) -> None:
        deployed = self.write_record(
            "2" * 32,
            "2026-08-25T12:00:00Z",
            "v1",
            "deployed",
        )
        failed = self.write_record(
            "3" * 32,
            "2026-08-25T13:00:00Z",
            "v2",
            "failed",
        )

        code, stdout, stderr = self.run_cli(
            "status",
            "--project",
            "example",
            "--environment",
            "lab",
            "--json",
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")

        document = json.loads(stdout)

        self.assertEqual(document["release_count"], 2)
        self.assertEqual(
            document["current"]["release_id"],
            deployed["release_id"],
        )
        self.assertEqual(
            document["latest"]["release_id"],
            failed["release_id"],
        )
        self.assertEqual(
            document["latest"]["status"],
            "failed",
        )

    def test_status_reports_invalid_ledger(self) -> None:
        ledger_directory = create_private_ledger_directory(
            self.projects_root,
            "example",
            "lab",
        )
        corrupted = ledger_directory / f"{'4' * 32}.json"
        corrupted.write_text("{invalid", encoding="utf-8")
        corrupted.chmod(0o600)

        code, stdout, stderr = self.run_cli(
            "status",
            "--project",
            "example",
            "--environment",
            "lab",
        )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("status error:", stderr)

    def test_deploy_starts_release(self) -> None:
        code, stdout, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")

        document = json.loads(stdout)

        self.assertEqual(document["status"], "deployed")
        self.assertFalse(document["reused"])
        self.assertTrue(document["containers_started"])
        self.assertTrue(document["image_pulled"])
        self.assertEqual(document["release_tag"], "v1")
        self.assertEqual(document["image"], IMAGE)
        self.assertEqual(len(self.pulled_images), 1)
        self.assertEqual(self.pulled_images[0]["image"], IMAGE)
        self.assertIsNone(self.pulled_images[0]["registry_token"])

        staged_path = Path(document["staged_bundle_path"])
        self.assertTrue(staged_path.is_dir())
        runtime_secrets_path = Path(document["runtime_secrets_path"])
        self.assertTrue(runtime_secrets_path.is_file())
        self.assertEqual(
            self.materialized_secrets[0]["encrypted_file"],
            staged_path / "deploy" / "secrets.lab.sops.yaml",
        )
        self.assertEqual(
            self.runtime_events,
            [
                ("validate", IMAGE),
                ("migration", IMAGE),
                ("start", IMAGE),
            ],
        )
        self.assertEqual(
            self.nginx_events,
            ["build", "prepare", "stage", "activate"],
        )

        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["release_id"],
            document["release_id"],
        )

    def test_deploy_reuses_identical_deployed_release(self) -> None:
        first_code, first_stdout, first_stderr = self.run_cli(*self.deploy_arguments())
        second_code, second_stdout, second_stderr = self.run_cli(
            *self.deploy_arguments()
        )

        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(first_stderr, "")
        self.assertEqual(second_stderr, "")

        first = json.loads(first_stdout)
        second = json.loads(second_stdout)

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertTrue(first["containers_started"])
        self.assertFalse(second["containers_started"])
        self.assertEqual(
            first["release_id"],
            second["release_id"],
        )
        self.assertEqual(len(self.materialized_secrets), 2)
        self.assertEqual(len(self.pulled_images), 2)
        self.assertEqual(
            first["runtime_secrets_path"],
            second["runtime_secrets_path"],
        )
        self.assertEqual(
            [event for event in self.runtime_events if event[0] == "start"],
            [("start", IMAGE)],
        )
        self.assertIsNone(second["retention"])

        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )
        self.assertEqual(len(records), 1)

    def test_deploy_supersedes_abandoned_prepared_release(self) -> None:
        prepared = build_prepared_release(
            request=self.request,
            staged_bundle_path=self.staged_bundle,
            releases_root=self.releases_root,
            release_id="7" * 32,
            timestamp="2026-08-25T14:00:00Z",
        )
        write_release_record(self.projects_root, prepared)

        code, stdout, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        document = json.loads(stdout)
        self.assertFalse(document["reused"])
        self.assertTrue(document["containers_started"])
        self.assertNotEqual(document["release_id"], prepared["release_id"])
        self.assertEqual(document["status"], "deployed")
        self.assertEqual(self.runtime_events[-1], ("start", IMAGE))

        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )
        self.assertEqual(len(records), 2)

    def test_redeploying_a_superseded_tag_starts_containers(self) -> None:
        first_code, first_stdout, _ = self.run_cli(*self.deploy_arguments())

        newer_image = "ghcr.io/example/platform-example@sha256:" + ("c" * 64)
        newer_code, newer_stdout, _ = self.run_cli(
            *self.deploy_arguments(newer_image, release_tag="v2")
        )

        self.runtime_events.clear()

        rollforward_code, rollback_stdout, rollback_stderr = self.run_cli(
            *self.deploy_arguments()
        )

        self.assertEqual(first_code, 0)
        self.assertEqual(newer_code, 0)
        self.assertEqual(rollforward_code, 0)
        self.assertEqual(rollback_stderr, "")

        first = json.loads(first_stdout)
        newer = json.loads(newer_stdout)
        restored = json.loads(rollback_stdout)

        self.assertFalse(restored["reused"])
        self.assertTrue(restored["containers_started"])
        self.assertEqual(restored["release_tag"], "v1")
        self.assertEqual(restored["image"], IMAGE)
        self.assertNotEqual(restored["release_id"], first["release_id"])
        self.assertEqual(self.runtime_events[-1], ("start", IMAGE))

        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )
        self.assertEqual(len(records), 3)

        current = find_latest_deployed_release(records)
        self.assertEqual(current["release_id"], restored["release_id"])
        self.assertEqual(
            current["previous_release_id"],
            newer["release_id"],
        )

    def test_migration_failure_leaves_release_failed(self) -> None:
        self.migration_error = "migration command failed"

        code, stdout, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("deploy error: migration command failed", stderr)
        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )
        self.assertEqual(records[-1]["status"], "failed")
        self.assertEqual(records[-1]["migration"]["status"], "failed")
        self.assertEqual(records[-1]["healthcheck"]["status"], "pending")
        self.assertEqual(
            self.runtime_events,
            [("validate", IMAGE), ("migration", IMAGE)],
        )
        self.assertEqual(self.nginx_events, ["build", "prepare"])

    def test_nginx_ownership_conflict_is_rejected_before_migration(self):
        self.nginx_build_error = "domain is owned by another project"

        code, stdout, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("owned by another project", stderr)
        self.assertEqual(self.runtime_events, [("validate", IMAGE)])
        self.assertEqual(self.nginx_events, ["build"])
        records = list_release_records(self.projects_root, "example", "lab")
        self.assertEqual(records[-1]["status"], "prepared")

    def test_nginx_activation_failure_stops_first_release_and_restores_fragments(self):
        self.nginx_activate_error = "nginx test failed"

        code, stdout, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("nginx test failed", stderr)
        self.assertEqual(self.runtime_events[-1], ("stop", IMAGE))
        self.assertEqual(
            self.nginx_events,
            ["build", "prepare", "stage", "activate", "rollback"],
        )
        records = list_release_records(self.projects_root, "example", "lab")
        self.assertEqual(records[-1]["status"], "failed")

    def test_nginx_activation_failure_restores_previous_release(self):
        previous = self.write_record(
            "8" * 32,
            "2026-08-25T14:00:00Z",
            "v0",
            "deployed",
        )
        self.nginx_activate_error = "nginx test failed"
        next_image = "ghcr.io/example/platform-example@sha256:" + ("b" * 64)

        code, _, stderr = self.run_cli(*self.deploy_arguments(next_image, "v2"))

        self.assertEqual(code, 1)
        self.assertIn("previous release restored", stderr)
        self.assertEqual(self.runtime_events[-1], ("start", IMAGE))
        records = list_release_records(self.projects_root, "example", "lab")
        self.assertEqual(records[-1]["status"], "rolled_back")
        self.assertEqual(records[-1]["previous_release_id"], previous["release_id"])

    def test_unhealthy_first_release_is_stopped(self) -> None:
        self.start_error_images.add(IMAGE)

        code, stdout, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("deploy error: application is unhealthy", stderr)
        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )
        self.assertEqual(records[-1]["status"], "failed")
        self.assertEqual(records[-1]["healthcheck"]["status"], "failed")
        self.assertEqual(self.runtime_events[-1], ("stop", IMAGE))

    def test_unhealthy_release_restores_previous_release(self) -> None:
        previous = self.write_record(
            "8" * 32,
            "2026-08-25T14:00:00Z",
            "v0",
            "deployed",
        )
        next_image = "ghcr.io/example/platform-example@sha256:" + ("b" * 64)
        self.start_error_images.add(next_image)

        code, stdout, stderr = self.run_cli(*self.deploy_arguments(next_image, "v2"))

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("previous release restored", stderr)
        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )
        self.assertEqual(records[-1]["status"], "rolled_back")
        self.assertEqual(
            records[-1]["previous_release_id"],
            previous["release_id"],
        )
        current = [record for record in records if record["status"] == "deployed"]
        self.assertEqual(current[-1]["release_id"], previous["release_id"])
        self.assertEqual(self.runtime_events[-1], ("start", IMAGE))

    def test_deploy_prepares_database_before_the_ledger(self) -> None:
        code, stdout, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 0)
        self.assertEqual(len(self.database_ensures), 1)
        call = self.database_ensures[0]
        self.assertEqual(call["project"], "example")
        self.assertEqual(call["environment"], "lab")
        self.assertEqual(
            call["manifest"]["database"]["mode"],
            "external",
        )

    def test_database_failure_leaves_no_ledger_record(self) -> None:
        self.database_error = "database is not healthy"

        code, stdout, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 1)
        self.assertIn("deploy error: database is not healthy", stderr)
        self.assertEqual(
            list_release_records(self.projects_root, "example", "lab"),
            [],
        )
        self.assertEqual(self.runtime_events, [])

    def test_platform_database_url_reaches_the_release_environment(
        self,
    ) -> None:
        self.database_password = "pw-test"

        code, stdout, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 0)
        document = json.loads(stdout)
        content = Path(document["runtime_secrets_path"]).read_text(encoding="utf-8")
        self.assertIn('DATABASE_URL="postgresql://app:pw-test@db:5432/app"', content)

    def test_noop_redeploy_still_ensures_the_database(self) -> None:
        first_code, _, _ = self.run_cli(*self.deploy_arguments())
        self.database_ensures.clear()

        second_code, _, _ = self.run_cli(*self.deploy_arguments())

        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(len(self.database_ensures), 1)

    def backup_arguments(self, *extra: str) -> tuple[str, ...]:
        return (
            "backup",
            "--project",
            "example",
            "--environment",
            "lab",
            "--json",
            *extra,
        )

    def test_backup_describes_the_deployed_release(self) -> None:
        self.run_cli(*self.deploy_arguments())

        code, stdout, stderr = self.run_cli(*self.backup_arguments())

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        document = json.loads(stdout)
        self.assertEqual(document["reason"], "operator")
        self.assertEqual(len(self.backup_calls), 1)

        current = find_latest_deployed_release(
            list_release_records(self.projects_root, "example", "lab")
        )
        self.assertEqual(
            self.backup_calls[0]["record"]["release_id"],
            current["release_id"],
        )

    def test_backup_reason_reaches_the_card(self) -> None:
        self.run_cli(*self.deploy_arguments())

        code, stdout, _ = self.run_cli(
            *self.backup_arguments("--reason", "pre-migration")
        )

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["reason"], "pre-migration")

    def test_backup_without_a_deployed_release_is_refused(self) -> None:
        code, stdout, stderr = self.run_cli(*self.backup_arguments())

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("backup error:", stderr)
        self.assertEqual(self.backup_calls, [])

    def test_backup_respects_the_project_environment_lock(self) -> None:
        self.run_cli(*self.deploy_arguments())

        with project_environment_lock(
            self.lock_root,
            "example",
            "lab",
            "deploy",
        ):
            code, _, stderr = self.run_cli(*self.backup_arguments())

        self.assertEqual(code, 1)
        self.assertIn("backup error:", stderr)
        self.assertEqual(self.backup_calls, [])

    def restore_arguments(self, *extra: str) -> tuple[str, ...]:
        return (
            "restore",
            "--project",
            "example",
            "--environment",
            "lab",
            "--json",
            *extra,
        )

    def test_restore_refuses_without_explicit_confirmation(self) -> None:
        """Restoring replaces the live database; that needs saying out loud."""
        self.run_cli(*self.deploy_arguments())

        code, stdout, stderr = self.run_cli(*self.restore_arguments())

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("--confirm-destructive", stderr)

    def test_restore_refuses_while_a_deploy_holds_the_lock(self) -> None:
        self.run_cli(*self.deploy_arguments())

        with project_environment_lock(
            self.lock_root,
            "example",
            "lab",
            "deploy",
        ):
            code, _, stderr = self.run_cli(
                *self.restore_arguments("--confirm-destructive")
            )

        self.assertEqual(code, 1)
        self.assertIn("restore error:", stderr)

    def test_restore_without_a_deployed_release_is_refused(self) -> None:
        code, _, stderr = self.run_cli(*self.restore_arguments("--confirm-destructive"))

        self.assertEqual(code, 1)
        self.assertIn("restore error:", stderr)

    def test_verify_backup_without_a_deployed_release_is_refused(self) -> None:
        code, _, stderr = self.run_cli(
            "verify-backup",
            "--project",
            "example",
            "--environment",
            "lab",
            "--json",
        )

        self.assertEqual(code, 1)
        self.assertIn("verify-backup error:", stderr)

    def test_status_reports_an_unproven_backup_as_never_verified(self) -> None:
        self.run_cli(*self.deploy_arguments())

        code, stdout, _ = self.run_cli(
            "status",
            "--project",
            "example",
            "--environment",
            "lab",
        )

        self.assertEqual(code, 0)
        self.assertIn("last proven restorable: never", stdout)

    def test_an_external_database_takes_no_pre_migration_backup(self) -> None:
        self.run_cli(*self.deploy_arguments())

        self.assertEqual(self.backup_calls, [])

    def test_deploy_reconciles_the_backup_timer(self) -> None:
        code, stdout, _ = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 0)
        self.assertEqual(len(self.timer_calls), 1)
        self.assertEqual(self.timer_calls[0]["project"], "example")
        self.assertEqual(self.timer_calls[0]["environment"], "lab")
        self.assertEqual(
            json.loads(stdout)["schedule"]["state"],
            "enabled",
        )

    def test_a_timer_failure_warns_without_failing_the_deploy(self) -> None:
        """A release already serving traffic is not a failed release."""
        self.timer_error = "systemctl enable failed"

        code, stdout, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        schedule = json.loads(stdout)["schedule"]
        self.assertEqual(schedule["state"], "unknown")
        self.assertIn("systemctl enable failed", schedule["warning"])

    def use_docker_database_bundle(self) -> None:
        """Rebuild the bundle as a docker-mode application with backups on.

        The shared fixture declares an external database, where a
        pre-migration backup is correctly a no-op.
        """
        import shutil

        import yaml

        app_root = self.base / "docker-db-app"
        deploy = app_root / "deploy"
        deploy.mkdir(parents=True)

        for name in ("platform.yml", "compose.yml", "secrets.lab.sops.yaml"):
            shutil.copy2(EXAMPLE_MANIFEST.parent / name, deploy / name)

        manifest_path = deploy / "platform.yml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["database"] = {
            "mode": "docker",
            "postgres_major": 18,
            "backup_enabled": True,
            "backup": {"interval_minutes": 360, "retain": 14},
        }
        manifest_path.write_text(
            yaml.safe_dump(manifest, sort_keys=False),
            encoding="utf-8",
        )

        compose_path = deploy / "compose.yml"
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        service = compose["services"][manifest["service"]["web"]]
        service["networks"] = list(service.get("networks", [])) + ["db"]
        compose["networks"]["db"] = {
            "name": "${PLATFORM_DB_NETWORK:?PLATFORM_DB_NETWORK is required}",
            "external": True,
        }
        compose_path.write_text(
            yaml.safe_dump(compose, sort_keys=False),
            encoding="utf-8",
        )

        self.bundle_path = self.base / "docker-db.bundle.tar.gz"
        create_bundle(manifest_path, self.bundle_path)

    def test_a_migration_takes_a_backup_first(self) -> None:
        self.use_docker_database_bundle()

        self.run_cli(*self.deploy_arguments())

        reasons = [call["reason"] for call in self.backup_calls]

        self.assertEqual(reasons, ["pre-migration"])
        self.assertEqual(
            self.backup_calls[0]["record"]["release_tag"],
            "v1",
        )
        # The dump must precede the migration it is protecting.
        self.assertEqual(self.runtime_events[0][0], "validate")
        self.assertEqual(self.runtime_events[1][0], "migration")

    def test_a_failed_pre_migration_backup_stops_the_migration(self) -> None:
        """No safety net, no destructive step."""
        self.use_docker_database_bundle()
        self.backup_error = "database dump failed"

        code, _, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 1)
        self.assertIn("pre-migration backup failed", stderr)
        self.assertNotIn(
            "migration",
            [event[0] for event in self.runtime_events],
        )

        records = list_release_records(self.projects_root, "example", "lab")
        self.assertEqual(records[-1]["status"], "failed")
        self.assertEqual(records[-1]["migration"]["status"], "failed")

    def test_deploy_rejects_release_tag_conflict(self) -> None:
        first_code, _, first_stderr = self.run_cli(*self.deploy_arguments())
        conflicting_image = "ghcr.io/example/platform-example@sha256:" + ("b" * 64)
        second_code, _, second_stderr = self.run_cli(
            *self.deploy_arguments(conflicting_image)
        )

        self.assertEqual(first_code, 0)
        self.assertEqual(first_stderr, "")
        self.assertEqual(second_code, 1)
        self.assertIn(
            "release tag already points to a different image or bundle",
            second_stderr,
        )

        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )
        self.assertEqual(len(records), 1)

    def test_deploy_respects_project_environment_lock(self) -> None:
        with project_environment_lock(
            self.lock_root,
            "example",
            "lab",
            "status",
        ):
            code, stdout, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn(
            "another operation is already running",
            stderr,
        )
        self.assertFalse(self.projects_root.exists())

    def test_deploy_does_not_write_ledger_when_secrets_fail(self) -> None:
        def fail_materialization(**kwargs) -> Path:
            raise RuntimeSecretsError("SOPS decryption failed")

        self.secrets_materializer = fail_materialization

        code, stdout, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("deploy error: SOPS decryption failed", stderr)

        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )
        self.assertEqual(records, [])

    def test_deploy_reads_temporary_registry_token_from_stdin(self) -> None:
        token = b"github-actions-temporary-token"
        self.token_stream = io.BytesIO(token + b"\n")
        arguments = self.deploy_arguments() + (
            "--registry-username",
            "github-actions",
            "--registry-token-stdin",
        )

        code, stdout, stderr = self.run_cli(*arguments)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertNotIn(token.decode("utf-8"), stdout)
        self.assertEqual(
            self.pulled_images[0]["registry_username"],
            "github-actions",
        )
        self.assertEqual(
            self.pulled_images[0]["registry_token"],
            token,
        )

    def test_deploy_does_not_write_ledger_when_pull_fails(self) -> None:
        def fail_pull(**kwargs) -> None:
            raise RegistryPullError("Docker registry command failed")

        self.image_puller = fail_pull

        code, stdout, stderr = self.run_cli(*self.deploy_arguments())

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn(
            "deploy error: Docker registry command failed",
            stderr,
        )
        self.assertEqual(self.materialized_secrets, [])

        records = list_release_records(
            self.projects_root,
            "example",
            "lab",
        )
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
