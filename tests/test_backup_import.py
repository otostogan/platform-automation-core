"""A dump from another host, imported by the host after it proves the envelope opens."""

import argparse
import io
import json
import tempfile
import unittest
from pathlib import Path

from platform_automation import platform_cli
from platform_automation.backup_runtime import (
    ENVELOPE_MAGIC,
    BackupRuntimeError,
    card_from_head,
    card_path,
    import_backup,
    list_backups,
)

CARD = {
    "api_version": "platform-backup/v1",
    "project": "example",
    "environment": "lab",
    "stamp": "20260828T120000Z-schedule",
    "reason": "schedule",
    "created_at": "2026-08-28T12:00:00Z",
    "postgres_major": 18,
    "release_id": "0123456789abcdef0123456789abcdef",
    "release_tag": "lab-v0.1.7",
}
SOURCE = {"host": "platform-host-1", "project": "example", "environment": "lab"}


def envelope(card: dict) -> bytes:
    body = json.dumps(card).encode()
    return ENVELOPE_MAGIC + str(len(body)).encode() + b"\n" + body + b"PGDMP..."


class FakeAge:
    """``age --decrypt`` that answers with a fixed plaintext head, or nothing."""

    def __init__(self, head: bytes, errors: bytes = b""):
        self.head, self.errors = head, errors
        self.commands = []
        self.terminated = False

    def __call__(self, command, stdin=None, stdout=None, stderr=None):
        self.commands.append(command)
        self.stdout = io.BytesIO(self.head)
        self.returncode = 0 if self.head else 1
        return self

    def terminate(self):
        self.terminated = True

    def communicate(self, timeout=None):
        return b"", self.errors


class CardFromHeadTest(unittest.TestCase):
    def test_reads_the_card_the_envelope_carries(self) -> None:
        self.assertEqual(card_from_head(envelope(CARD)), CARD)

    def test_a_dump_without_an_envelope_is_refused(self) -> None:
        with self.assertRaisesRegex(BackupRuntimeError, "no envelope card"):
            card_from_head(b"PGDMP\x01\x02")

    def test_a_truncated_envelope_is_refused(self) -> None:
        with self.assertRaisesRegex(BackupRuntimeError, "truncated"):
            card_from_head(envelope(CARD)[:40])


class ImportBackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "backups"
        self.key = Path(self.temporary.name) / "age.key"
        self.key.write_text("synthetic")

    def run_import(
        self, age: FakeAge, data: bytes = b"age-encrypted-bytes", stamp=CARD["stamp"]
    ):
        return import_backup(
            self.root,
            "example",
            "production",
            stamp,
            SOURCE,
            io.BytesIO(data),
            self.key,
            Path("age"),
            popen=age,
        )

    def test_stores_the_dump_and_a_card_that_says_where_it_came_from(self) -> None:
        age = FakeAge(envelope(CARD))

        document = self.run_import(age)

        directory = self.root / "example" / "production"
        dump = directory / f"{CARD['stamp']}.dump.age"
        self.assertEqual(dump.read_bytes(), b"age-encrypted-bytes")
        self.assertEqual(dump.stat().st_mode & 0o777, 0o600)
        card = json.loads(card_path(dump).read_text())
        self.assertEqual(card["release_tag"], "lab-v0.1.7")
        self.assertEqual(card["imported_from"], SOURCE)
        self.assertIn("imported_at", card)
        self.assertEqual(list_backups(directory), [CARD["stamp"]])
        self.assertEqual(document["stamp"], CARD["stamp"])
        self.assertEqual(document["size_bytes"], len(b"age-encrypted-bytes"))
        self.assertEqual(document["imported_from"], SOURCE)
        # Only the head was read: age was told to stop, the plaintext went nowhere.
        self.assertTrue(age.terminated)
        self.assertEqual(age.commands[0][:2], ["age", "--decrypt"])
        self.assertEqual(age.commands[0][3], str(self.key))

    def test_a_dump_this_host_cannot_open_is_refused_and_leaves_nothing(self) -> None:
        age = FakeAge(
            b"", errors=b"age: error: no identity matched any of the recipients"
        )

        with self.assertRaisesRegex(BackupRuntimeError, "not encrypted to this host"):
            self.run_import(age)

        directory = self.root / "example" / "production"
        self.assertEqual([p.name for p in directory.iterdir()], [])

    def test_an_empty_stream_is_refused(self) -> None:
        with self.assertRaisesRegex(BackupRuntimeError, "no dump arrived"):
            self.run_import(FakeAge(envelope(CARD)), data=b"")

    def test_an_existing_stamp_is_not_overwritten(self) -> None:
        self.run_import(FakeAge(envelope(CARD)))
        with self.assertRaisesRegex(BackupRuntimeError, "already exists"):
            self.run_import(FakeAge(envelope(CARD)), data=b"other")

    def test_a_bad_stamp_is_refused_before_anything_is_read(self) -> None:
        with self.assertRaisesRegex(BackupRuntimeError, "invalid backup stamp"):
            self.run_import(FakeAge(envelope(CARD)), stamp="../etc")


class RunBackupImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.lock_root = Path(self.temporary.name) / "locks"

    def arguments(self, **overrides) -> argparse.Namespace:
        values = {
            "project": "example",
            "environment": "production",
            "stamp": CARD["stamp"],
            "source_host": "platform-host-1",
            "source_project": "example",
            "source_environment": "lab",
            "json": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_hands_the_stream_and_source_to_the_importer_and_prints_json(self) -> None:
        seen = {}

        def importer(root, project, environment, stamp, source, stream, key, age):
            seen.update(
                root=root,
                project=project,
                environment=environment,
                stamp=stamp,
                source=source,
                data=stream.read(),
            )
            return {
                "stamp": stamp,
                "path": "/x",
                "size_bytes": 3,
                "imported_from": source,
            }

        out = io.StringIO()
        import contextlib

        with contextlib.redirect_stdout(out):
            code = platform_cli.run_backup_import(
                self.arguments(),
                Path(self.lock_root),
                Path("/var/backups/platform"),
                Path("/etc/platform/keys/age.key"),
                Path("age"),
                stream=io.BytesIO(b"abc"),
                importer=importer,
            )

        self.assertEqual(code, 0)
        self.assertEqual(seen["data"], b"abc")
        self.assertEqual(seen["source"], SOURCE)
        self.assertEqual(json.loads(out.getvalue())["stamp"], CARD["stamp"])

    def test_the_parser_knows_the_command(self) -> None:
        arguments = platform_cli.parse_arguments(
            [
                "backup-import",
                "--project",
                "example",
                "--environment",
                "production",
                "--stamp",
                CARD["stamp"],
                "--source-host",
                "platform-host-1",
                "--source-project",
                "example",
                "--source-environment",
                "lab",
                "--json",
            ]
        )
        self.assertEqual(arguments.command, "backup-import")
        self.assertEqual(arguments.source_environment, "lab")

    def test_an_importer_error_is_one_line_on_stderr(self) -> None:
        def importer(*args, **kwargs):
            raise BackupRuntimeError(
                "the dump is not encrypted to this host's identity"
            )

        err = io.StringIO()
        import contextlib

        with contextlib.redirect_stderr(err):
            code = platform_cli.run_backup_import(
                self.arguments(),
                Path(self.lock_root),
                Path("/b"),
                Path("/k"),
                Path("age"),
                stream=io.BytesIO(b"x"),
                importer=importer,
            )
        self.assertEqual(code, 1)
        self.assertIn("backup import error: the dump is not encrypted", err.getvalue())


if __name__ == "__main__":
    unittest.main()
