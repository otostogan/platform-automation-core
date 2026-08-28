import copy
import json
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


EXAMPLE_MANIFEST = (
    Path(__file__).parent / "fixtures" / "app-contract" / "deploy" / "platform.yml"
)


from platform_automation.build_bundle import create_bundle  # noqa: E402
from platform_automation.stage_bundle import (  # noqa: E402
    BundleStagingError,
    stage_verified_bundle,
)
from platform_automation.verify_bundle import verify_bundle  # noqa: E402


class StageBundleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.bundle_path = self.base / "bundle.tar.gz"
        self.releases_root = self.base / "releases"

        create_bundle(
            EXAMPLE_MANIFEST,
            self.bundle_path,
        )
        self.verified = verify_bundle(self.bundle_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def expected_destination(self) -> Path:
        return (
            self.releases_root.resolve()
            / "example"
            / "lab"
            / "bundles"
            / self.verified.digest
        )

    def test_stages_verified_bundle(self) -> None:
        destination = stage_verified_bundle(
            self.verified,
            self.releases_root,
        )

        self.assertEqual(
            destination,
            self.expected_destination(),
        )
        self.assertTrue(destination.is_dir())
        self.assertEqual(
            stat.S_IMODE(destination.stat().st_mode),
            0o700,
        )

        metadata_path = destination / "platform-bundle.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata, self.verified.metadata)

        for name, content in self.verified.files.items():
            relative_path = self.verified.metadata["files"][name]["path"]
            staged_file = destination / relative_path

            self.assertEqual(
                staged_file.read_bytes(),
                content,
            )
            self.assertEqual(
                stat.S_IMODE(staged_file.stat().st_mode),
                0o600,
            )

        self.assertEqual(
            stat.S_IMODE(metadata_path.stat().st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE((destination / "deploy").stat().st_mode),
            0o700,
        )

    def test_reuses_existing_staged_bundle(self) -> None:
        destination = stage_verified_bundle(
            self.verified,
            self.releases_root,
        )
        metadata_before = (destination / "platform-bundle.json").read_bytes()

        second_destination = stage_verified_bundle(
            self.verified,
            self.releases_root,
        )

        self.assertEqual(second_destination, destination)
        self.assertEqual(
            (destination / "platform-bundle.json").read_bytes(),
            metadata_before,
        )

    def test_rejects_invalid_bundle_digest(self) -> None:
        invalid = replace(
            self.verified,
            digest="../invalid",
        )

        with self.assertRaisesRegex(
            BundleStagingError,
            "invalid bundle digest for staging",
        ):
            stage_verified_bundle(
                invalid,
                self.releases_root,
            )

        self.assertFalse(self.releases_root.exists())

    def test_cleans_temporary_directory_after_failure(self) -> None:
        metadata = copy.deepcopy(self.verified.metadata)
        metadata["files"]["compose"]["path"] = metadata["files"]["manifest"]["path"]

        invalid = replace(
            self.verified,
            metadata=metadata,
        )

        with self.assertRaisesRegex(
            BundleStagingError,
            "duplicate staged file path",
        ):
            stage_verified_bundle(
                invalid,
                self.releases_root,
            )

        destination = self.expected_destination()
        bundle_parent = destination.parent

        self.assertFalse(destination.exists())
        self.assertEqual(
            list(bundle_parent.glob(f".{self.verified.digest}.*")),
            [],
        )

    def test_rejects_symbolic_link_destination(self) -> None:
        destination = self.expected_destination()
        destination.parent.mkdir(parents=True)

        protected_directory = self.base / "protected"
        protected_directory.mkdir()
        destination.symlink_to(
            protected_directory,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(
            BundleStagingError,
            "existing staged bundle is invalid",
        ):
            stage_verified_bundle(
                self.verified,
                self.releases_root,
            )

        self.assertEqual(
            list(protected_directory.iterdir()),
            [],
        )

    def test_rejects_corrupted_existing_stage(self) -> None:
        destination = stage_verified_bundle(
            self.verified,
            self.releases_root,
        )
        compose_path = destination / "deploy" / "compose.yml"
        compose_path.write_bytes(b"corrupted\n")

        with self.assertRaisesRegex(
            BundleStagingError,
            "content mismatch",
        ):
            stage_verified_bundle(
                self.verified,
                self.releases_root,
            )


if __name__ == "__main__":
    unittest.main()
