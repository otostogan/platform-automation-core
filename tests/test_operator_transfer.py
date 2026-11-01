"""The pieces of a backup transfer: recipients, sources, the pipe and the lines."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from platform_automation.operator.context import Host
from platform_automation.operator.transfer import (
    Source,
    TransferError,
    backup_entries,
    candidate_sources,
    describe_entry,
    dump_remote_path,
    fetch_command,
    import_command,
    rekey,
    rekey_commands,
    sops_recipients,
)

HOST_RECIPIENT = "age1" + "h" * 58
RECOVERY = "age1" + "r" * 58


class RecipientsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_reads_both_recipients_from_sops_yaml(self) -> None:
        (self.root / ".sops.yaml").write_text(
            "creation_rules:\n    - path_regex: deploy/secrets\n      key_groups:\n"
            f"          - age:\n                - {HOST_RECIPIENT}\n                - {RECOVERY}\n"
        )
        self.assertEqual(sops_recipients(self.root), [HOST_RECIPIENT, RECOVERY])

    def test_one_recipient_is_not_enough(self) -> None:
        (self.root / ".sops.yaml").write_text(
            f"creation_rules:\n    - key_groups:\n          - age:\n                - {HOST_RECIPIENT}\n"
        )
        with self.assertRaisesRegex(TransferError, "recovery recipient"):
            sops_recipients(self.root)


class SourcesTest(unittest.TestCase):
    def test_every_host_of_every_infra_but_the_target(self) -> None:
        infras = [
            SimpleNamespace(name="example-infra", path=Path("/a")),
            SimpleNamespace(name="other-infra", path=Path("/l")),
        ]
        active = {
            Path("/a"): (
                Host(
                    "platform-host-1",
                    "platform-host-1.tailnet.example.invalid",
                    "ops",
                    "~/.ssh/example-ops",
                ),
            ),
            Path("/l"): (
                Host(
                    "platform-host-2",
                    "platform-host-2.tailnet.example.invalid",
                    "ops",
                    "~/.ssh/other-ops",
                ),
            ),
        }
        retired = {
            Path("/a"): (
                Host(
                    "platform-host-0",
                    "platform-host-0.tailnet.example.invalid",
                    "ops",
                    None,
                ),
            ),
            Path("/l"): (),
        }

        def secret_path(root, host, key):
            return Path(f"/keys/{host.name}.agekey")

        sources = candidate_sources(
            "platform-host-1.tailnet.example.invalid",
            infras,
            lambda root: active[root],
            lambda root: retired[root],
            secret_path,
        )

        self.assertEqual(
            [s.name for s in sources], ["platform-host-0", "platform-host-2"]
        )
        self.assertTrue(sources[0].retired)
        self.assertIn("retired", sources[0].label)
        self.assertEqual(sources[1].key_file, Path("~/.ssh/other-ops").expanduser())
        self.assertEqual(sources[1].age_key, Path("/keys/platform-host-2.agekey"))


class CommandsTest(unittest.TestCase):
    def test_paths_and_lines(self) -> None:
        path = dump_remote_path("app", "lab", "20260101T120000Z-schedule")
        self.assertEqual(
            path, "/var/backups/platform/app/lab/20260101T120000Z-schedule.dump.age"
        )
        self.assertEqual(fetch_command(path), f"sudo -n cat {path}")
        source = Source(
            "other-infra",
            "platform-host-2",
            "platform-host-2.tailnet.example.invalid",
            None,
            None,
        )
        line = import_command(
            "app", "production", "20260101T120000Z-schedule", source, "app", "lab"
        )
        self.assertTrue(
            line.startswith(
                "sudo -n platform backup-import --project app --environment production"
            )
        )
        self.assertIn(
            "--stamp 20260101T120000Z-schedule --source-host platform-host-2", line
        )
        self.assertIn("--source-project app --source-environment lab --json", line)

    def test_rekey_commands_name_the_identity_and_every_recipient(self) -> None:
        decrypt, encrypt = rekey_commands(
            Path("/keys/platform-host-2.agekey"), [HOST_RECIPIENT, RECOVERY]
        )
        self.assertEqual(
            decrypt, ["age", "--decrypt", "--identity", "/keys/platform-host-2.agekey"]
        )
        self.assertEqual(
            encrypt, ["age", "--recipient", HOST_RECIPIENT, "--recipient", RECOVERY]
        )

    def test_entries_and_their_description(self) -> None:
        entries = backup_entries(
            {
                "backups": [
                    {"stamp": "s1", "release_tag": "v1", "verified": True},
                    {"nope": 1},
                ]
            }
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(describe_entry(entries[0]), "s1  v1  proven")
        self.assertEqual(backup_entries("junk"), [])


class FakeProcess:
    def __init__(self, returncode, output=b"", errors=b""):
        self.returncode, self.output, self.errors = returncode, output, errors
        self.stdout = None

    def communicate(self):
        return self.output, self.errors


class RekeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.dump = self.work / "in.age"
        self.dump.write_bytes(b"ciphertext")
        self.out = self.work / "out.age"

    def pipe(self, decrypt_code=0, encrypt_code=0, written=b"re-encrypted"):
        calls = []

        def popen(command, stdin=None, stdout=None, stderr=None):
            calls.append(command)
            if command[1:2] == ["--decrypt"]:
                process = FakeProcess(
                    decrypt_code, errors=b"no identity matched" if decrypt_code else b""
                )
                process.stdout = subprocess.PIPE and __import__("io").BytesIO(b"plain")
                return process
            if hasattr(stdout, "write"):
                stdout.write(written)
            return FakeProcess(
                encrypt_code, errors=b"bad recipient" if encrypt_code else b""
            )

        return popen, calls

    def test_decrypts_with_the_source_key_and_encrypts_to_the_recipients(self) -> None:
        popen, calls = self.pipe()
        rekey(
            self.dump,
            self.out,
            Path("/keys/platform-host-2.agekey"),
            [HOST_RECIPIENT, RECOVERY],
            popen,
        )
        self.assertEqual(self.out.read_bytes(), b"re-encrypted")
        self.assertEqual(calls[0][:2], ["age", "--decrypt"])
        self.assertEqual(calls[1][0], "age")
        self.assertIn(RECOVERY, calls[1])

    def test_a_wrong_source_key_is_reported(self) -> None:
        popen, _ = self.pipe(decrypt_code=1, written=b"")
        with self.assertRaisesRegex(
            TransferError, "did not decrypt with the source host's key"
        ):
            rekey(
                self.dump,
                self.out,
                Path("/keys/x.agekey"),
                [HOST_RECIPIENT, RECOVERY],
                popen,
            )

    def test_a_failed_encryption_is_reported(self) -> None:
        popen, _ = self.pipe(encrypt_code=1, written=b"")
        with self.assertRaisesRegex(TransferError, "re-encryption"):
            rekey(
                self.dump,
                self.out,
                Path("/keys/x.agekey"),
                [HOST_RECIPIENT, RECOVERY],
                popen,
            )


if __name__ == "__main__":
    unittest.main()
