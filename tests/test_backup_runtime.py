import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from platform_automation.backup_runtime import (  # noqa: E402
    CARD_SUFFIX,
    DUMP_SUFFIX,
    BackupRuntimeError,
    age_command,
    backup_stamp,
    build_metadata_card,
    card_path,
    create_backup,
    loss_window,
    split_envelope,
    render_envelope_header,
    stream_encrypted_dump,
    stamp_taken_at,
    list_backups,
    prune_backups,
    resolve_retention,
)

RECIPIENTS = {"age1hostexample", "age1recoveryexample"}
MOMENT = datetime(2026, 8, 29, 14, 5, 30, tzinfo=timezone.utc)


def secrets_document(recipients=RECIPIENTS) -> dict:
    return {
        "sops": {
            "age": [
                {"recipient": recipient, "enc": "ENC[data]"}
                for recipient in sorted(recipients)
            ],
            "mac": "ENC[mac]",
            "version": "3.8.1",
        },
    }


def manifest(retain: int = 3, mode: str = "docker") -> dict:
    return {
        "database": {
            "mode": mode,
            "postgres_major": 18,
            "backup_enabled": True,
            "backup": {"interval_minutes": 360, "retain": retain},
        },
    }


def release_record() -> dict:
    return {
        "release_id": "a" * 32,
        "release_tag": "production-2026-08-28-1",
        "image": {"reference": "ghcr.io/example/app@sha256:" + ("b" * 64)},
        "bundle": {"digest": "c" * 64},
    }


class StampTest(unittest.TestCase):
    def test_stamp_sorts_chronologically_and_names_the_reason(self) -> None:
        self.assertEqual(
            backup_stamp("pre-migration", MOMENT),
            "20260829T140530Z-pre-migration",
        )

    def test_unknown_reason_is_refused(self) -> None:
        with self.assertRaises(BackupRuntimeError):
            backup_stamp("whatever", MOMENT)


class MetadataCardTest(unittest.TestCase):
    def test_card_keys_on_release_id_not_on_the_tag(self) -> None:
        """A tag may be anything; release_id is always present and unique."""
        card = build_metadata_card(
            "example",
            "lab",
            "20260829T140530Z-operator",
            "operator",
            release_record(),
            18,
        )

        self.assertEqual(card["release_id"], "a" * 32)
        self.assertEqual(card["release_tag"], "production-2026-08-28-1")
        self.assertEqual(card["postgres_major"], 18)
        self.assertEqual(card["api_version"], "platform-backup/v1")


class AgeCommandTest(unittest.TestCase):
    def test_every_recipient_is_named(self) -> None:
        command = age_command(RECIPIENTS, Path("/usr/local/bin/age"))

        self.assertEqual(command.count("--recipient"), 2)
        for recipient in RECIPIENTS:
            self.assertIn(recipient, command)

    def test_no_recipients_is_refused(self) -> None:
        with self.assertRaises(BackupRuntimeError):
            age_command(set(), Path("/usr/local/bin/age"))


class RetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def seed(self, *stamps: str) -> None:
        for stamp in stamps:
            (self.directory / f"{stamp}{DUMP_SUFFIX}").write_bytes(b"x")
            (self.directory / f"{stamp}{CARD_SUFFIX}").write_text("{}")

    def test_lists_only_well_formed_dumps_oldest_first(self) -> None:
        self.seed(
            "20260829T140530Z-operator",
            "20260828T140530Z-schedule",
        )
        (self.directory / "notes.txt").write_text("x")
        (self.directory / f"garbage{DUMP_SUFFIX}").write_bytes(b"x")

        self.assertEqual(
            list_backups(self.directory),
            ["20260828T140530Z-schedule", "20260829T140530Z-operator"],
        )

    def test_prunes_the_oldest_and_keeps_the_newest(self) -> None:
        self.seed(
            "20260827T140530Z-schedule",
            "20260828T140530Z-schedule",
            "20260829T140530Z-operator",
        )
        warnings: list[str] = []

        removed = prune_backups(self.directory, 2, warnings)

        self.assertEqual(removed, ["20260827T140530Z-schedule"])
        self.assertEqual(warnings, [])
        self.assertEqual(len(list_backups(self.directory)), 2)

    def test_retain_of_one_still_keeps_the_newest(self) -> None:
        self.seed("20260828T140530Z-schedule", "20260829T140530Z-operator")

        prune_backups(self.directory, 1, [])

        self.assertEqual(
            list_backups(self.directory),
            ["20260829T140530Z-operator"],
        )

    def test_retention_comes_from_the_manifest(self) -> None:
        self.assertEqual(resolve_retention(manifest(retain=7)), 7)

    def test_absent_backup_block_falls_back_to_the_default(self) -> None:
        document = manifest()
        del document["database"]["backup"]

        self.assertEqual(resolve_retention(document), 14)


class EnvelopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def card(self) -> dict:
        return {"release_id": "a" * 32, "stamp": "s"}

    def write(self, body: bytes) -> Path:
        path = self.base / "dump"
        path.write_bytes(body)
        return path

    def test_a_dump_without_an_envelope_is_left_alone(self) -> None:
        """A backup that stopped restoring because the format improved would
        not be an improvement."""
        card, offset = split_envelope(self.write(b"PGDMP\x01\x02legacy body"))

        self.assertIsNone(card)
        self.assertEqual(offset, 0)

    def test_an_envelope_round_trips(self) -> None:
        body = render_envelope_header(self.card()) + b"PGDMP-body"
        card, offset = split_envelope(self.write(body))

        self.assertEqual(card["release_id"], "a" * 32)
        self.assertEqual(body[offset:], b"PGDMP-body")

    def test_the_offset_survives_being_handed_to_a_real_process(self) -> None:
        """The bug this guards against was invisible to a fake runner.

        A buffered reader's seek restores the Python-level position but leaves
        the descriptor where read-ahead put it, so a child handed that
        descriptor received an empty stream, and pg_dump's own complaint was
        the only evidence it had happened.
        """
        for label, body, expected in (
            ("legacy", b"PGDMP\x01\x02legacy body", b"PGDMP\x01\x02legacy body"),
            (
                "enveloped",
                render_envelope_header(self.card()) + b"PGDMP-body",
                b"PGDMP-body",
            ),
        ):
            with self.subTest(label=label):
                path = self.write(body)
                _, offset = split_envelope(path)
                descriptor = os.open(path, os.O_RDONLY)

                try:
                    os.lseek(descriptor, offset, os.SEEK_SET)
                    result = subprocess.run(
                        ["cat"],
                        stdin=descriptor,
                        capture_output=True,
                    )
                finally:
                    os.close(descriptor)

                self.assertEqual(result.stdout, expected)

    def test_a_truncated_envelope_is_loud(self) -> None:
        header = render_envelope_header(self.card())

        with self.assertRaises(BackupRuntimeError):
            split_envelope(self.write(header[:-20]))

    def test_a_malformed_length_is_loud(self) -> None:
        with self.assertRaises(BackupRuntimeError):
            split_envelope(self.write(b"PLATFORM-BACKUP/1 notanumber\n{}"))

    def test_an_oversized_card_is_refused(self) -> None:
        with self.assertRaises(BackupRuntimeError):
            render_envelope_header({"pad": "x" * 70000})


class LossWindowTest(unittest.TestCase):
    def now(self, minutes_after: int) -> datetime:
        return datetime(2026, 8, 29, 14, 5, 30, tzinfo=timezone.utc) + timedelta(
            minutes=minutes_after
        )

    def test_the_age_of_the_newest_dump_is_the_window(self) -> None:
        window = loss_window("20260829T140530Z-schedule", 15, self.now(4))

        self.assertEqual(window["newest_age_minutes"], 4)
        self.assertEqual(window["interval_minutes"], 15)
        self.assertFalse(window["overdue"])

    def test_a_late_run_inside_the_slack_is_not_overdue(self) -> None:
        """A randomised delay makes running late normal; crying wolf is how
        a signal gets ignored."""
        window = loss_window("20260829T140530Z-schedule", 15, self.now(25))

        self.assertFalse(window["overdue"])

    def test_a_stopped_schedule_is_named(self) -> None:
        window = loss_window("20260829T140530Z-schedule", 15, self.now(120))

        self.assertTrue(window["overdue"])

    def test_no_schedule_is_never_overdue(self) -> None:
        window = loss_window("20260829T140530Z-operator", None, self.now(9999))

        self.assertFalse(window["overdue"])
        self.assertIsNone(window["interval_minutes"])

    def test_no_dumps_with_a_schedule_is_overdue(self) -> None:
        window = loss_window(None, 15, self.now(0))

        self.assertTrue(window["overdue"])
        self.assertIsNone(window["newest_age_minutes"])

    def test_a_malformed_stamp_yields_no_age(self) -> None:
        self.assertIsNone(loss_window("garbage", 15, self.now(0))["newest_age_minutes"])

    def test_the_stamp_is_read_as_utc(self) -> None:
        self.assertEqual(
            stamp_taken_at("20260829T140530Z-operator"),
            datetime(2026, 8, 29, 14, 5, 30, tzinfo=timezone.utc),
        )


class RealPipelineTest(unittest.TestCase):
    """Exercise the dump pipeline with real processes.

    Two defects in a row hid behind fakes that did not model process
    semantics: a descriptor a child inherits, and a stdin that cannot be
    flushed once closed. `cat` stands in for both pg_dump and age, so the
    pipes, the closes and the waits are the real ones.
    """

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.body = b"PGDMP" + bytes(range(256)) * 40

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def popen(self, command, **options):
        # pg_dump emits the body; age passes it through untouched.
        if "pg_dump" in command:
            source = self.base / "source.bin"
            source.write_bytes(self.body)
            return subprocess.Popen(
                ["cat", str(source)],
                stdout=options["stdout"],
                stderr=options["stderr"],
            )

        return subprocess.Popen(
            ["cat"],
            stdin=options["stdin"],
            stdout=options["stdout"],
            stderr=options["stderr"],
        )

    def test_the_pipeline_writes_the_envelope_and_the_whole_dump(self) -> None:
        destination = self.base / f"20260829T140530Z-operator{DUMP_SUFFIX}"
        card = {"release_id": "a" * 32, "stamp": "s"}

        written = stream_encrypted_dump(
            destination=destination,
            container="c",
            password="p",
            recipients={"age1example"},
            card=card,
            docker_executable=Path("docker"),
            age_executable=Path("age"),
            popen=self.popen,
        )

        self.assertEqual(written, destination.stat().st_size)

        parsed, offset = split_envelope(destination)
        self.assertEqual(parsed["release_id"], "a" * 32)
        self.assertEqual(destination.read_bytes()[offset:], self.body)

    def test_a_path_that_is_not_a_dump_is_refused(self) -> None:
        """The sidecar name is derived, not substituted: a mismatched name
        would otherwise resolve to the dump's own path and overwrite it."""
        with self.assertRaises(BackupRuntimeError):
            card_path(self.base / "out.age")


class FakeStdin:
    """Collect what the platform writes into age, so tests can read it back."""

    def __init__(self, sink) -> None:
        self.sink = sink

    def write(self, data: bytes) -> int:
        return self.sink.write(data)

    def close(self) -> None:
        pass


class FakeProcess:
    def __init__(
        self,
        returncode: int,
        output: bytes = b"",
        stdout_pipe=None,
        stdin=None,
    ):
        self.returncode = returncode
        self._output = output
        self.stdout = stdout_pipe
        self.stdin = stdin
        self.stderr = io.BytesIO(b"")

    def communicate(self, timeout=None):
        return b"", b""

    def wait(self, timeout=None):
        return self.returncode


class CreateBackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(self.temporary_directory.name)
        self.databases_root = base / "databases"
        self.backups_root = base / "backups"
        self.age_key_file = base / "age.key"
        self.age_key_file.write_text("AGE-SECRET-KEY-TEST\n")
        self.commands: list[list[str]] = []
        self.dump_code = 0
        self.encrypt_code = 0

        credentials = self.databases_root / "example" / "lab"
        credentials.mkdir(mode=0o700, parents=True)
        (credentials / "credentials.sops.json").write_text(
            json.dumps({"ciphertext": "ENC[pw-secret]"})
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def runner(self, command, **options):
        self.commands.append(list(command))
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps({"password": "pw-secret"}).encode("utf-8"),
            stderr=b"",
        )

    def popen(self, command, **options):
        self.commands.append(list(command))

        if "pg_dump" in command:
            return FakeProcess(
                self.dump_code,
                stdout_pipe=io.BytesIO(b"PGDMP-body"),
            )

        # age is faked as a passthrough so a test can read the envelope the
        # platform wrote and confirm the dump follows it untouched.
        return FakeProcess(
            self.encrypt_code,
            stdin=FakeStdin(options["stdout"]),
        )

    def backup(self, document=None, secrets=None, reason="operator"):
        return create_backup(
            manifest=document or manifest(),
            project="example",
            environment="lab",
            record=release_record(),
            secrets_document=(secrets_document() if secrets is None else secrets),
            reason=reason,
            databases_root=self.databases_root,
            backups_root=self.backups_root,
            age_key_file=self.age_key_file,
            sops_executable=Path("sops"),
            age_executable=Path("age"),
            docker_executable=Path("docker"),
            runner=self.runner,
            popen=self.popen,
            moment=MOMENT,
        )

    def test_writes_an_encrypted_dump_and_its_card(self) -> None:
        result = self.backup()

        directory = self.backups_root / "example" / "lab"
        dump = directory / f"20260829T140530Z-operator{DUMP_SUFFIX}"
        card = directory / f"20260829T140530Z-operator{CARD_SUFFIX}"

        self.assertTrue(dump.is_file())
        self.assertTrue(card.is_file())
        self.assertEqual(result["recipients"], 2)
        self.assertGreater(result["bytes"], 0)
        self.assertEqual(
            json.loads(card.read_text())["release_id"],
            "a" * 32,
        )

    def test_dump_and_card_are_private(self) -> None:
        self.backup()
        directory = self.backups_root / "example" / "lab"

        for suffix in (DUMP_SUFFIX, CARD_SUFFIX):
            path = directory / f"20260829T140530Z-operator{suffix}"
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_every_recipient_encrypts_the_dump(self) -> None:
        self.backup()
        age = [c for c in self.commands if "--encrypt" in c][0]

        self.assertEqual(age.count("--recipient"), 2)

    def test_filename_carries_no_application_version(self) -> None:
        """Object keys are readable by anyone who can list a bucket."""
        result = self.backup()

        self.assertNotIn("production-2026-08-28-1", result["stamp"])
        self.assertNotIn("0.1.7", result["stamp"])

    def test_external_database_has_nothing_to_back_up(self) -> None:
        with self.assertRaises(BackupRuntimeError):
            self.backup(document=manifest(mode="external"))

    def test_secrets_without_recipients_are_refused(self) -> None:
        with self.assertRaises(BackupRuntimeError):
            self.backup(secrets={})

    def test_a_failed_dump_leaves_nothing_behind(self) -> None:
        self.dump_code = 1

        with self.assertRaises(BackupRuntimeError):
            self.backup()

        directory = self.backups_root / "example" / "lab"
        self.assertEqual(list_backups(directory), [])
        self.assertEqual(list(directory.iterdir()), [])

    def test_a_failed_encryption_leaves_nothing_behind(self) -> None:
        self.encrypt_code = 1

        with self.assertRaises(BackupRuntimeError):
            self.backup()

        self.assertEqual(
            list((self.backups_root / "example" / "lab").iterdir()),
            [],
        )

    def test_the_dump_carries_its_own_card(self) -> None:
        """A dump found alone still describes itself once decrypted."""
        self.backup()

        path = (
            self.backups_root
            / "example"
            / "lab"
            / f"20260829T140530Z-operator{DUMP_SUFFIX}"
        )
        card, offset = split_envelope(path)

        self.assertEqual(card["release_id"], "a" * 32)
        self.assertEqual(card["stamp"], "20260829T140530Z-operator")
        # The pg_dump bytes follow the envelope untouched.
        self.assertEqual(path.read_bytes()[offset:], b"PGDMP-body")

    def test_the_envelope_opens_with_a_readable_line(self) -> None:
        """Someone holding only age should see what they have."""
        self.backup()

        written = (
            self.backups_root
            / "example"
            / "lab"
            / f"20260829T140530Z-operator{DUMP_SUFFIX}"
        ).read_bytes()

        self.assertTrue(written.startswith(b"PLATFORM-BACKUP/1 "))

    def test_missing_credential_is_loud(self) -> None:
        (self.databases_root / "example" / "lab" / "credentials.sops.json").unlink()

        with self.assertRaises(BackupRuntimeError):
            self.backup()


if __name__ == "__main__":
    unittest.main()
