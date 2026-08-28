import tempfile
import unittest
from pathlib import Path


EXAMPLE_MANIFEST = (
    Path(__file__).parent / "fixtures" / "app-contract" / "deploy" / "platform.yml"
)


from platform_automation.build_bundle import create_bundle  # noqa: E402
from platform_automation.deployment_request import (  # noqa: E402
    DeploymentRequestError,
    load_deployment_request,
)


IMAGE_DIGEST = "sha256:" + ("a" * 64)
IMAGE = f"ghcr.io/example/platform-example@{IMAGE_DIGEST}"


class DeploymentRequestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.bundle_path = self.base / "bundle.tar.gz"

        create_bundle(
            EXAMPLE_MANIFEST,
            self.bundle_path,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def load(self):
        return load_deployment_request(
            bundle_path=self.bundle_path,
            project="example",
            environment="lab",
            image=IMAGE,
            release_tag="v1.2.3",
        )

    def test_accepts_valid_deployment_request(self) -> None:
        request = self.load()

        self.assertEqual(request.project, "example")
        self.assertEqual(request.environment, "lab")
        self.assertEqual(request.release_tag, "v1.2.3")
        self.assertEqual(request.image, IMAGE)
        self.assertEqual(
            request.image_repository,
            "ghcr.io/example/platform-example",
        )
        self.assertEqual(
            request.image_digest,
            IMAGE_DIGEST,
        )
        self.assertEqual(
            request.bundle.metadata["project"],
            "example",
        )

    def test_rejects_image_tag_without_digest(self) -> None:
        with self.assertRaisesRegex(
            DeploymentRequestError,
            "image must use an immutable sha256 digest",
        ):
            load_deployment_request(
                bundle_path=self.bundle_path,
                project="example",
                environment="lab",
                image="ghcr.io/company/example:v1.2.3",
                release_tag="v1.2.3",
            )

    def test_rejects_different_image_repository(self) -> None:
        with self.assertRaisesRegex(
            DeploymentRequestError,
            "image repository does not match",
        ):
            load_deployment_request(
                bundle_path=self.bundle_path,
                project="example",
                environment="lab",
                image=("ghcr.io/attacker/example@" f"{IMAGE_DIGEST}"),
                release_tag="v1.2.3",
            )

    def test_rejects_project_mismatch(self) -> None:
        with self.assertRaisesRegex(
            DeploymentRequestError,
            "requested project does not match",
        ):
            load_deployment_request(
                bundle_path=self.bundle_path,
                project="different-project",
                environment="lab",
                image=IMAGE,
                release_tag="v1.2.3",
            )

    def test_rejects_environment_mismatch(self) -> None:
        with self.assertRaisesRegex(
            DeploymentRequestError,
            "requested environment does not match",
        ):
            load_deployment_request(
                bundle_path=self.bundle_path,
                project="example",
                environment="production",
                image=IMAGE,
                release_tag="v1.2.3",
            )

    def test_rejects_unsafe_release_tag(self) -> None:
        with self.assertRaisesRegex(
            DeploymentRequestError,
            "invalid release tag",
        ):
            load_deployment_request(
                bundle_path=self.bundle_path,
                project="example",
                environment="lab",
                image=IMAGE,
                release_tag="../production",
            )

    def test_wraps_bundle_verification_error(self) -> None:
        self.bundle_path.write_bytes(b"invalid archive")

        with self.assertRaisesRegex(
            DeploymentRequestError,
            "deployment bundle verification failed",
        ):
            self.load()


if __name__ == "__main__":
    unittest.main()
