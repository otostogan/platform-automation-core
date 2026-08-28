import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from platform_automation import bundle_action as prepare
from platform_automation.verify_bundle import verify_bundle

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONSUMER_APPLICATION = PROJECT_ROOT / "examples" / "consumer" / "application"
SOPS_FIXTURE = (
    PROJECT_ROOT
    / "tests"
    / "fixtures"
    / "app-contract"
    / "deploy"
    / "secrets.lab.sops.yaml"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


class BundleActionTest(unittest.TestCase):
    def test_prepares_example_bundle_and_outputs_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            application_root = temporary_root / "application"
            shutil.copytree(CONSUMER_APPLICATION, application_root)
            shutil.copy2(
                SOPS_FIXTURE,
                application_root / "deploy" / "secrets.staging.sops.yaml",
            )
            manifest_path = application_root / "deploy" / "platform.yml"
            bundle_path = temporary_root / "example.bundle.tar.gz"
            github_output = temporary_root / "github-output"

            values = prepare.prepare_bundle(
                manifest_path=manifest_path,
                bundle_path=bundle_path,
                github_output=github_output,
                minimum_age_recipients=2,
            )

            self.assertTrue(bundle_path.is_file())
            self.assertGreater(bundle_path.stat().st_size, 0)
            self.assertEqual(
                verify_bundle(bundle_path).digest,
                values["bundle_digest"],
            )
            self.assertEqual(
                values["bundle_digest"],
                sha256_file(bundle_path),
            )
            self.assertEqual(values["project"], "platform-example")
            self.assertEqual(values["environment"], "staging")
            self.assertEqual(
                values["healthcheck_host"],
                "app.example.invalid",
            )
            self.assertEqual(
                values["healthcheck_path"],
                "/healthz",
            )

            written_outputs = dict(
                line.split("=", 1)
                for line in github_output.read_text(encoding="utf-8").splitlines()
            )

            self.assertEqual(written_outputs, values)
            self.assertEqual(
                written_outputs["bundle_path"],
                str(bundle_path.resolve()),
            )

    def test_action_installs_release_wheel_without_checkout_imports(self) -> None:
        action = (
            PROJECT_ROOT / ".github" / "actions" / "build-bundle" / "action.yml"
        ).read_text(encoding="utf-8")
        entrypoint = (
            PROJECT_ROOT / "src" / "platform_automation" / "bundle_action.py"
        ).read_text(encoding="utf-8")

        self.assertIn("platform-prepare-bundle", action)
        self.assertIn("minimum-age-recipients", action)
        self.assertIn("platform_automation_runtime-*.whl", action)
        self.assertNotIn("parents[3]", entrypoint)
        self.assertNotIn("sys.path", entrypoint)

    def test_rejects_multiline_github_output(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "cannot contain line breaks",
        ):
            prepare.require_safe_output(
                "project",
                "unsafe\nvalue",
            )


if __name__ == "__main__":
    unittest.main()
