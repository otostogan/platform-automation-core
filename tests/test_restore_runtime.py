import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from platform_automation.backup_runtime import CARD_SUFFIX, DUMP_SUFFIX  # noqa: E402
from platform_automation.restore_runtime import (  # noqa: E402
    RestoreRuntimeError,
    describe_revision_gap,
    last_verification,
    read_metadata_card,
    restore_backup,
    select_backup,
    verify_backup,
)

OLDER = "20260828T120000Z-schedule"
NEWER = "20260829T140530Z-operator"


def card(release_id: str = "a" * 32, tag: str = "lab-v0.1.7") -> dict:
    return {
        "api_version": "platform-backup/v1",
        "project": "example",
        "environment": "lab",
        "release_id": release_id,
        "release_tag": tag,
        "postgres_major": 18,
    }


def manifest(query: str = "SELECT 1") -> dict:
    return {
        "database": {"mode": "docker", "postgres_major": 18},
        "restore_validation": {"query": query},
    }


def record(release_id: str = "a" * 32, tag: str = "lab-v0.1.7") -> dict:
    return {"release_id": release_id, "release_tag": tag}


class RevisionGapTest(unittest.TestCase):
    def test_same_release_is_no_gap(self) -> None:
        self.assertIsNone(describe_revision_gap(card(), record()))

    def test_no_deployed_release_is_no_gap(self) -> None:
        self.assertIsNone(describe_revision_gap(card(), None))

    def test_a_different_release_is_named_in_both_directions(self) -> None:
        gap = describe_revision_gap(
            card("a" * 32, "lab-v0.0.1"),
            record("b" * 32, "lab-v1.0.1"),
        )

        self.assertIn("lab-v0.0.1", gap)
        self.assertIn("lab-v1.0.1", gap)


class BackupDirectoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(self.temporary_directory.name)
        self.backups_root = base / "backups"
        self.databases_root = base / "databases"
        self.runtime_root = base / "runtime"
        self.runtime_root.mkdir(mode=0o700, parents=True)
        self.age_key_file = base / "age.key"
        self.age_key_file.write_text("AGE-SECRET-KEY-TEST\n")
        self.directory = self.backups_root / "example" / "lab"
        self.directory.mkdir(mode=0o700, parents=True)

        credentials = self.databases_root / "example" / "lab"
        credentials.mkdir(mode=0o700, parents=True)
        (credentials / "credentials.sops.json").write_text(
            json.dumps({"ciphertext": "ENC[pw]"})
        )

        self.commands: list[list[str]] = []
        self.fail_actions: set[str] = set()
        self.embedded_card = None

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def seed(self, *stamps: str) -> None:
        for stamp in stamps:
            (self.directory / f"{stamp}{DUMP_SUFFIX}").write_bytes(b"encrypted")
            (self.directory / f"{stamp}{CARD_SUFFIX}").write_text(json.dumps(card()))

    def action(self, command: list[str]) -> str:
        for name in (
            "--decrypt",
            "pg_restore",
            "pg_isready",
            "psql",
            "run",
            "rm",
            "decrypt",
        ):
            if name in command:
                return "age-decrypt" if name == "--decrypt" else name

        raise AssertionError(f"unexpected command: {command}")

    def runner(self, command, **options):
        self.commands.append(list(command))
        action = self.action(command)

        if action == "age-decrypt":
            from platform_automation.backup_runtime import (
                render_envelope_header,
            )

            if self.embedded_card is not None:
                options["stdout"].write(render_envelope_header(self.embedded_card))

            options["stdout"].write(b"PGDMP-plaintext")

        return subprocess.CompletedProcess(
            args=command,
            returncode=1 if action in self.fail_actions else 0,
            stdout=(
                json.dumps({"password": "pw"}).encode("utf-8")
                if action == "decrypt"
                else b"1"
            ),
            stderr=b"",
        )


class SelectBackupTest(BackupDirectoryFixture):
    def test_defaults_to_the_newest(self) -> None:
        self.seed(OLDER, NEWER)

        self.assertEqual(select_backup(self.directory, None), NEWER)

    def test_an_explicit_stamp_is_honoured(self) -> None:
        self.seed(OLDER, NEWER)

        self.assertEqual(select_backup(self.directory, OLDER), OLDER)

    def test_an_unknown_stamp_is_refused(self) -> None:
        self.seed(NEWER)

        with self.assertRaises(RestoreRuntimeError):
            select_backup(self.directory, OLDER)

    def test_a_malformed_stamp_is_refused(self) -> None:
        self.seed(NEWER)

        with self.assertRaises(RestoreRuntimeError):
            select_backup(self.directory, "../../etc/passwd")

    def test_no_backups_is_refused(self) -> None:
        with self.assertRaises(RestoreRuntimeError):
            select_backup(self.directory, None)

    def test_a_dump_without_a_card_is_refused(self) -> None:
        (self.directory / f"{NEWER}{DUMP_SUFFIX}").write_bytes(b"x")

        with self.assertRaises(RestoreRuntimeError):
            read_metadata_card(self.directory, NEWER)


class RestoreTest(BackupDirectoryFixture):
    def restore(self, stamp=None, current=None):
        return restore_backup(
            project="example",
            environment="lab",
            stamp=stamp,
            current_record=current,
            databases_root=self.databases_root,
            backups_root=self.backups_root,
            runtime_secrets_root=self.runtime_root,
            age_key_file=self.age_key_file,
            sops_executable=Path("sops"),
            age_executable=Path("age"),
            docker_executable=Path("docker"),
            runner=self.runner,
        )

    def test_decrypts_then_restores_the_live_database(self) -> None:
        self.seed(NEWER)

        result = self.restore(current=record())

        self.assertEqual(result["stamp"], NEWER)
        self.assertIsNone(result["revision_gap"])
        self.assertEqual(
            [self.action(c) for c in self.commands],
            ["decrypt", "age-decrypt", "pg_restore"],
        )

    def test_targets_the_project_database_container(self) -> None:
        self.seed(NEWER)
        self.restore()

        restore_call = [c for c in self.commands if "pg_restore" in c][0]

        self.assertIn("platform-db-example-lab-postgres-1", restore_call)
        self.assertIn("--clean", restore_call)
        self.assertIn("--exit-on-error", restore_call)

    def test_a_legacy_dump_without_an_envelope_still_restores(self) -> None:
        """Dumps written before the envelope existed must keep working."""
        self.seed(NEWER)

        result = self.restore(current=record())

        self.assertEqual(result["stamp"], NEWER)
        self.assertFalse(result["self_describing"])

    def test_the_card_inside_the_stream_wins_over_the_sidecar(self) -> None:
        """The embedded card travelled with the data; the sidecar beside it."""
        self.seed(NEWER)
        # A sidecar that disagrees is exactly the drift the envelope removes.
        (self.directory / f"{NEWER}{CARD_SUFFIX}").write_text(
            json.dumps(card("f" * 32, "sidecar-drifted"))
        )
        self.embedded_card = card("a" * 32, "lab-from-the-stream")

        result = self.restore(current=record())

        self.assertTrue(result["self_describing"])
        self.assertEqual(result["release_tag"], "lab-from-the-stream")

    def test_a_revision_gap_is_reported_not_refused(self) -> None:
        self.seed(NEWER)

        result = self.restore(current=record("b" * 32, "lab-v9.9.9"))

        self.assertIsNotNone(result["revision_gap"])

    def test_a_failed_decryption_never_reaches_the_database(self) -> None:
        self.seed(NEWER)
        self.fail_actions.add("age-decrypt")

        with self.assertRaises(RestoreRuntimeError):
            self.restore()

        self.assertNotIn(
            "pg_restore",
            [self.action(c) for c in self.commands],
        )

    def test_plaintext_does_not_outlive_the_restore(self) -> None:
        self.seed(NEWER)
        self.restore()

        leftovers = list((self.runtime_root / "example" / "lab").iterdir())

        self.assertEqual(leftovers, [])


class VerifyTest(BackupDirectoryFixture):
    def verify(self, document=None, stamp=None):
        return verify_backup(
            manifest=document or manifest(),
            project="example",
            environment="lab",
            stamp=stamp,
            databases_root=self.databases_root,
            backups_root=self.backups_root,
            runtime_secrets_root=self.runtime_root,
            age_key_file=self.age_key_file,
            sops_executable=Path("sops"),
            age_executable=Path("age"),
            docker_executable=Path("docker"),
            runner=self.runner,
            sleeper=lambda _: None,
        )

    def test_never_touches_the_live_database(self) -> None:
        self.seed(NEWER)

        self.verify()

        for command in self.commands:
            self.assertNotIn("platform-db-example-lab-postgres-1", command)

    def test_the_throwaway_container_has_no_network(self) -> None:
        self.seed(NEWER)
        self.verify()

        run = [c for c in self.commands if "run" in c][0]

        self.assertIn("--network", run)
        self.assertIn("none", run)
        self.assertIn("--rm", run)

    def test_runs_the_declared_validation_query(self) -> None:
        self.seed(NEWER)

        result = self.verify(document=manifest("SELECT count(*) FROM patients"))

        psql = [c for c in self.commands if "psql" in c][0]

        self.assertIn("SELECT count(*) FROM patients", psql)
        self.assertEqual(result["outcome"], "succeeded")
        self.assertEqual(result["result"], "1")

    def test_the_container_is_removed_even_when_the_restore_fails(self) -> None:
        self.seed(NEWER)
        self.fail_actions.add("pg_restore")

        with self.assertRaises(RestoreRuntimeError):
            self.verify()

        self.assertIn("rm", [self.action(c) for c in self.commands])

    def test_a_failure_is_recorded_as_loudly_as_a_success(self) -> None:
        self.seed(NEWER)
        self.fail_actions.add("psql")

        with self.assertRaises(RestoreRuntimeError):
            self.verify()

        entry = last_verification(self.directory)

        self.assertEqual(entry["outcome"], "failed")
        self.assertIsNotNone(entry["error"])
        self.assertEqual(entry["stamp"], NEWER)

    def test_a_success_is_dated_and_kept(self) -> None:
        self.seed(NEWER)
        self.verify()

        entry = last_verification(self.directory)

        self.assertEqual(entry["outcome"], "succeeded")
        self.assertEqual(entry["release_id"], "a" * 32)
        self.assertTrue(entry["completed_at"])

    def test_no_verification_yet_reads_as_none(self) -> None:
        self.assertIsNone(last_verification(self.directory))


if __name__ == "__main__":
    unittest.main()
