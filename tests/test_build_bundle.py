import json
import stat
import tarfile
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml


EXAMPLE_ROOT = Path(__file__).parent / "fixtures" / "app-contract"


from platform_automation.build_bundle import (  # noqa: E402
    BundleError,
    collect_bundle,
    create_bundle,
    resolve_app_file,
    sha256_file,
)
from platform_automation.validate_manifest import load_yaml  # noqa: E402


class BuildBundleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.app_root = self.base / "application"
        self.deploy_directory = self.app_root / "deploy"
        self.deploy_directory.mkdir(parents=True)

        shutil.copy2(
            EXAMPLE_ROOT / "deploy" / "platform.yml",
            self.deploy_directory / "platform.yml",
        )
        shutil.copy2(
            EXAMPLE_ROOT / "deploy" / "compose.yml",
            self.deploy_directory / "compose.yml",
        )

        self.manifest_path = self.deploy_directory / "platform.yml"
        self.secrets_path = self.deploy_directory / "secrets.lab.sops.yaml"
        self.secrets_path.write_text(
            (
                "database_password: "
                "ENC[AES256_GCM,data:test,iv:test,tag:test,type:str]\n"
                "sops:\n"
                "  age:\n"
                "    - recipient: age1example\n"
                "      enc: fake-encrypted-key\n"
                "  mac: "
                "ENC[AES256_GCM,data:test,iv:test,tag:test,type:str]\n"
                "  version: 3.9.4\n"
            ),
            encoding="utf-8",
        )

    def test_creates_expected_archive(self) -> None:
        output_path = self.base / "example.bundle.tar.gz"

        digest = create_bundle(
            self.manifest_path,
            output_path,
        )

        self.assertEqual(digest, sha256_file(output_path))
        self.assertEqual(
            stat.S_IMODE(output_path.stat().st_mode),
            0o600,
        )

        with tarfile.open(output_path, mode="r:gz") as archive:
            self.assertEqual(
                archive.getnames(),
                [
                    "platform-bundle.json",
                    "deploy/compose.yml",
                    "deploy/platform.yml",
                    "deploy/secrets.lab.sops.yaml",
                ],
            )

            metadata_file = archive.extractfile("platform-bundle.json")
            self.assertIsNotNone(metadata_file)
            metadata = json.load(metadata_file)

            self.assertEqual(
                metadata["project"],
                "example",
            )
            self.assertEqual(
                metadata["environment"],
                "lab",
            )

            for archive_path, source_path in (
                (
                    "deploy/compose.yml",
                    self.deploy_directory / "compose.yml",
                ),
                (
                    "deploy/platform.yml",
                    self.manifest_path,
                ),
                (
                    "deploy/secrets.lab.sops.yaml",
                    self.secrets_path,
                ),
            ):
                archived_file = archive.extractfile(archive_path)
                self.assertIsNotNone(archived_file)
                self.assertEqual(
                    archived_file.read(),
                    source_path.read_bytes(),
                )

    def test_build_is_reproducible(self) -> None:
        first_output = self.base / "first.tar.gz"
        second_output = self.base / "second.tar.gz"

        first_digest = create_bundle(
            self.manifest_path,
            first_output,
        )
        second_digest = create_bundle(
            self.manifest_path,
            second_output,
        )

        self.assertEqual(first_digest, second_digest)
        self.assertEqual(
            first_output.read_bytes(),
            second_output.read_bytes(),
        )

    def test_rejects_symbolic_link_output(self) -> None:
        target = self.base / "protected-target.tar.gz"
        target.write_bytes(b"must not be overwritten")

        output_link = self.base / "bundle.tar.gz"
        output_link.symlink_to(target)

        with self.assertRaisesRegex(
            BundleError,
            "bundle output cannot be a symbolic link",
        ):
            create_bundle(
                self.manifest_path,
                output_link,
            )

        self.assertEqual(
            target.read_bytes(),
            b"must not be overwritten",
        )

    def test_rejects_sops_file_without_age_recipient(self) -> None:
        secrets = load_yaml(self.secrets_path)
        secrets["sops"]["age"] = []

        self.secrets_path.write_text(
            yaml.safe_dump(secrets, sort_keys=False),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            BundleError,
            "SOPS metadata has no age recipient",
        ):
            collect_bundle(self.manifest_path)

    def test_enforces_unique_recovery_recipient_when_configured(self) -> None:
        secrets = load_yaml(self.secrets_path)
        secrets["sops"]["age"].append(dict(secrets["sops"]["age"][0]))
        self.secrets_path.write_text(
            yaml.safe_dump(secrets, sort_keys=False),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            BundleError,
            "requires at least 2 unique age recipients; found 1",
        ):
            collect_bundle(
                self.manifest_path,
                minimum_age_recipients=2,
            )

        secrets["sops"]["age"][1]["recipient"] = "age1recoveryexample"
        self.secrets_path.write_text(
            yaml.safe_dump(secrets, sort_keys=False),
            encoding="utf-8",
        )

        metadata, _ = collect_bundle(
            self.manifest_path,
            minimum_age_recipients=2,
        )
        self.assertEqual(metadata["project"], "example")

    def test_rejects_sops_file_without_encrypted_mac(self) -> None:
        secrets = load_yaml(self.secrets_path)
        secrets["sops"]["mac"] = "plaintext"

        self.secrets_path.write_text(
            yaml.safe_dump(secrets, sort_keys=False),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            BundleError,
            "SOPS metadata has no encrypted MAC",
        ):
            collect_bundle(self.manifest_path)

    def test_rejects_sops_file_without_version(self) -> None:
        secrets = load_yaml(self.secrets_path)
        del secrets["sops"]["version"]

        self.secrets_path.write_text(
            yaml.safe_dump(secrets, sort_keys=False),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            BundleError,
            "SOPS metadata has no version",
        ):
            collect_bundle(self.manifest_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_collects_valid_bundle_metadata(self) -> None:
        metadata, files = collect_bundle(self.manifest_path)

        self.assertEqual(
            metadata["api_version"],
            "platform-bundle/v1",
        )
        self.assertEqual(metadata["project"], "example")
        self.assertEqual(metadata["environment"], "lab")

        self.assertEqual(
            metadata["files"]["manifest"]["path"],
            "deploy/platform.yml",
        )
        self.assertEqual(
            metadata["files"]["compose"]["path"],
            "deploy/compose.yml",
        )
        self.assertEqual(
            metadata["files"]["secrets"]["path"],
            "deploy/secrets.lab.sops.yaml",
        )

        for name, path in files.items():
            self.assertEqual(
                metadata["files"][name]["sha256"],
                sha256_file(path),
            )

    def test_rejects_missing_secrets_file(self) -> None:
        self.secrets_path.unlink()

        with self.assertRaisesRegex(
            BundleError,
            "secrets file does not exist",
        ):
            collect_bundle(self.manifest_path)

    def test_rejects_plaintext_secrets_file(self) -> None:
        self.secrets_path.write_text(
            "database_password: plaintext\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            BundleError,
            "secrets file is not SOPS-encrypted",
        ):
            collect_bundle(self.manifest_path)

    def test_rejects_path_outside_application_root(self) -> None:
        outside_file = self.base / "outside.yml"
        outside_file.write_text("outside: true\n", encoding="utf-8")

        with self.assertRaisesRegex(
            BundleError,
            "escapes application root",
        ):
            resolve_app_file(
                self.app_root,
                "../outside.yml",
                "test file",
            )

    def test_rejects_symbolic_link(self) -> None:
        link = self.deploy_directory / "compose-link.yml"
        link.symlink_to(self.deploy_directory / "compose.yml")

        with self.assertRaisesRegex(
            BundleError,
            "cannot be a symbolic link",
        ):
            resolve_app_file(
                self.app_root,
                "deploy/compose-link.yml",
                "compose file",
            )

    def test_rejects_invalid_compose_contract(self) -> None:
        compose_path = self.deploy_directory / "compose.yml"
        compose = load_yaml(compose_path)
        compose["services"]["app"]["build"] = "."

        compose_path.write_text(
            yaml.safe_dump(compose, sort_keys=False),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            BundleError,
            "invalid application Compose file",
        ):
            collect_bundle(self.manifest_path)


if __name__ == "__main__":
    unittest.main()
