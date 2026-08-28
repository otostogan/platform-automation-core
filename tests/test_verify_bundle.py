import secrets
import tarfile
import tempfile
import unittest
import json
import yaml
from pathlib import Path


EXAMPLE_MANIFEST = (
    Path(__file__).parent / "fixtures" / "app-contract" / "deploy" / "platform.yml"
)


from platform_automation.build_bundle import create_bundle  # noqa: E402
from platform_automation.verify_bundle import (  # noqa: E402
    BundleVerificationError,
    validate_bundle_members,
    sha256_bytes,
    verify_bundle,
)


class VerifyBundleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.valid_bundle = self.base / "valid.tar.gz"
        self.modified_bundle = self.base / "modified.tar.gz"

        create_bundle(
            EXAMPLE_MANIFEST,
            self.valid_bundle,
        )

    def test_validates_members_without_archive(self):
        members = dict(self.read_entries())

        metadata, files, manifest, compose, secrets = validate_bundle_members(members)
        expected = verify_bundle(self.valid_bundle)

        self.assertEqual(metadata, expected.metadata)
        self.assertEqual(files, expected.files)
        self.assertEqual(manifest, expected.manifest)
        self.assertEqual(compose, expected.compose)
        self.assertEqual(secrets, expected.secrets)

    def test_rejects_corrupted_members_without_archive(self):
        members = dict(self.read_entries())
        metadata = json.loads(members["platform-bundle.json"])
        compose_path = metadata["files"]["compose"]["path"]
        members[compose_path] += b"\n# changed after deployment\n"

        with self.assertRaisesRegex(
            BundleVerificationError,
            "SHA-256 mismatch",
        ):
            validate_bundle_members(members)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def read_entries(self) -> list[tuple[str, bytes]]:
        entries: list[tuple[str, bytes]] = []

        with tarfile.open(self.valid_bundle, mode="r:gz") as archive:
            for member in archive.getmembers():
                extracted_file = archive.extractfile(member)
                self.assertIsNotNone(extracted_file)
                entries.append(
                    (
                        member.name,
                        extracted_file.read(),
                    )
                )

        return entries

    def write_entries(
        self,
        entries: list[tuple[str, bytes]],
    ) -> None:
        with tarfile.open(
            self.modified_bundle,
            mode="w:gz",
        ) as archive:
            for name, content in entries:
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                info.mode = 0o600
                archive.addfile(info, fileobj=BytesReader(content))

    def replace_member_and_checksum(
        self,
        archive_path: str,
        content: bytes,
    ) -> None:
        entries = dict(self.read_entries())
        metadata = json.loads(entries["platform-bundle.json"])

        descriptor = next(
            descriptor
            for descriptor in metadata["files"].values()
            if descriptor["path"] == archive_path
        )

        descriptor["sha256"] = sha256_bytes(content)
        entries[archive_path] = content
        entries["platform-bundle.json"] = (
            json.dumps(
                metadata,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

        self.write_entries(list(entries.items()))

    def test_accepts_valid_bundle(self) -> None:
        verified = verify_bundle(
            self.valid_bundle,
            minimum_age_recipients=2,
        )

        self.assertEqual(
            verified.metadata["api_version"],
            "platform-bundle/v1",
        )
        self.assertEqual(
            verified.metadata["project"],
            "example",
        )
        self.assertEqual(
            set(verified.files),
            {"manifest", "compose", "secrets"},
        )

    def test_host_policy_rejects_bundle_with_one_recipient(self) -> None:
        entries = dict(self.read_entries())
        secrets = yaml.safe_load(entries["deploy/secrets.lab.sops.yaml"])
        secrets["sops"]["age"] = secrets["sops"]["age"][:1]
        self.replace_member_and_checksum(
            "deploy/secrets.lab.sops.yaml",
            yaml.safe_dump(secrets, sort_keys=False).encode("utf-8"),
        )

        with self.assertRaisesRegex(
            BundleVerificationError,
            "requires at least 2 unique age recipients; found 1",
        ):
            verify_bundle(
                self.modified_bundle,
                minimum_age_recipients=2,
            )

    def test_rejects_invalid_archive(self) -> None:
        self.modified_bundle.write_bytes(b"not a tar archive")

        with self.assertRaisesRegex(
            BundleVerificationError,
            "invalid deployment bundle archive",
        ):
            verify_bundle(self.modified_bundle)

    def test_rejects_checksum_mismatch(self) -> None:
        entries = self.read_entries()
        entries = [
            (
                name,
                b"tampered compose\n" if name == "deploy/compose.yml" else content,
            )
            for name, content in entries
        ]
        self.write_entries(entries)

        with self.assertRaisesRegex(
            BundleVerificationError,
            "SHA-256 mismatch",
        ):
            verify_bundle(self.modified_bundle)

    def test_rejects_unexpected_archive_member(self) -> None:
        entries = self.read_entries()
        entries.append(("unexpected.txt", b"unexpected"))
        self.write_entries(entries)

        with self.assertRaisesRegex(
            BundleVerificationError,
            "must contain exactly 4 files",
        ):
            verify_bundle(self.modified_bundle)

    def test_rejects_path_traversal(self) -> None:
        entries = self.read_entries()
        entries = [
            (
                "../compose.yml" if name == "deploy/compose.yml" else name,
                content,
            )
            for name, content in entries
        ]
        self.write_entries(entries)

        with self.assertRaisesRegex(
            BundleVerificationError,
            "unsafe archive member path",
        ):
            verify_bundle(self.modified_bundle)

    def test_rejects_duplicate_archive_member(self) -> None:
        entries = self.read_entries()
        compose_entry = next(
            entry for entry in entries if entry[0] == "deploy/compose.yml"
        )
        entries = [entry for entry in entries if entry[0] != "deploy/platform.yml"]
        entries.append(compose_entry)
        self.write_entries(entries)

        with self.assertRaisesRegex(
            BundleVerificationError,
            "duplicate archive member",
        ):
            verify_bundle(self.modified_bundle)

    def test_rejects_symbolic_link_member(self) -> None:
        entries = self.read_entries()

        with tarfile.open(
            self.modified_bundle,
            mode="w:gz",
        ) as archive:
            for name, content in entries:
                info = tarfile.TarInfo(name=name)
                info.mode = 0o600

                if name == "deploy/compose.yml":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/etc/passwd"
                    info.size = 0
                    archive.addfile(info)
                else:
                    info.size = len(content)
                    archive.addfile(
                        info,
                        fileobj=BytesReader(content),
                    )

        with self.assertRaisesRegex(
            BundleVerificationError,
            "is not a regular file",
        ):
            verify_bundle(self.modified_bundle)

    def test_rejects_symbolic_link_bundle(self) -> None:
        bundle_link = self.base / "bundle-link.tar.gz"
        bundle_link.symlink_to(self.valid_bundle)

        with self.assertRaisesRegex(
            BundleVerificationError,
            "deployment bundle cannot be a symbolic link",
        ):
            verify_bundle(bundle_link)

    def test_rejects_self_consistent_invalid_manifest(self) -> None:
        entries = dict(self.read_entries())
        manifest = yaml.safe_load(entries["deploy/platform.yml"])
        manifest["image"]["repository"] = "ghcr.io/company/example:latest"

        manifest_content = yaml.safe_dump(
            manifest,
            sort_keys=False,
        ).encode("utf-8")

        self.replace_member_and_checksum(
            "deploy/platform.yml",
            manifest_content,
        )

        with self.assertRaisesRegex(
            BundleVerificationError,
            "invalid embedded application manifest",
        ):
            verify_bundle(self.modified_bundle)

    def test_rejects_self_consistent_invalid_compose(self) -> None:
        entries = dict(self.read_entries())
        compose = yaml.safe_load(entries["deploy/compose.yml"])
        compose["services"]["app"]["build"] = "."

        compose_content = yaml.safe_dump(
            compose,
            sort_keys=False,
        ).encode("utf-8")

        self.replace_member_and_checksum(
            "deploy/compose.yml",
            compose_content,
        )

        with self.assertRaisesRegex(
            BundleVerificationError,
            "invalid embedded application Compose file",
        ):
            verify_bundle(self.modified_bundle)

    def test_rejects_self_consistent_invalid_sops(self) -> None:
        entries = dict(self.read_entries())
        secrets = yaml.safe_load(entries["deploy/secrets.lab.sops.yaml"])
        del secrets["sops"]

        secrets_content = yaml.safe_dump(
            secrets,
            sort_keys=False,
        ).encode("utf-8")

        self.replace_member_and_checksum(
            "deploy/secrets.lab.sops.yaml",
            secrets_content,
        )

        with self.assertRaisesRegex(
            BundleVerificationError,
            "invalid embedded SOPS secrets",
        ):
            verify_bundle(self.modified_bundle)

    def test_rejects_metadata_project_mismatch(self) -> None:
        entries = dict(self.read_entries())
        metadata = json.loads(entries["platform-bundle.json"])
        metadata["project"] = "different-project"

        entries["platform-bundle.json"] = (
            json.dumps(
                metadata,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        self.write_entries(list(entries.items()))

        with self.assertRaisesRegex(
            BundleVerificationError,
            "bundle project does not match embedded manifest",
        ):
            verify_bundle(self.modified_bundle)


class BytesReader:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.position = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.content) - self.position

        chunk = self.content[self.position : self.position + size]
        self.position += len(chunk)

        return chunk


if __name__ == "__main__":
    unittest.main()
