import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts import check_release_artifacts as scanner


class ReleaseArtifactScannerTest(unittest.TestCase):
    def create_archive(self, members: dict[str, bytes]) -> Path:
        root = Path(self.temporary_directory.name)
        archive_path = root / f"artifact-{len(list(root.iterdir()))}.whl"

        with zipfile.ZipFile(archive_path, mode="w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)

        return archive_path

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_accepts_sanitized_archive(self) -> None:
        artifact = self.create_archive(
            {"platform_automation/runtime.py": b"PLATFORM_ROOT = '/opt/platform'\n"}
        )

        self.assertEqual(scanner.validate_artifact(artifact), 1)

    def test_rejects_public_address(self) -> None:
        artifact = self.create_archive(
            {
                "platform_automation/runtime.py": (
                    b"host = '" + b"8.8." + b"8.8" + b"'\n"
                )
            }
        )

        with self.assertRaisesRegex(ValueError, "non-documentation IPv4"):
            scanner.validate_artifact(artifact)

    def test_rejects_non_documentation_email(self) -> None:
        artifact = self.create_archive(
            {
                "platform_automation/runtime.py": (
                    b"contact = '" + b"ops@" + b"customer.test" + b"'\n"
                )
            }
        )

        with self.assertRaisesRegex(ValueError, "non-documentation email"):
            scanner.validate_artifact(artifact)

    def test_rejects_non_allowlisted_fqdn(self) -> None:
        domain = b".".join((b"customer", b"com"))
        artifact = self.create_archive(
            {"platform_automation/runtime.py": b"host = '" + domain + b"'\n"}
        )

        with self.assertRaisesRegex(ValueError, "non-allowlisted FQDN"):
            scanner.validate_artifact(artifact)

    def test_rejects_provider_hostname(self) -> None:
        hostname = b".".join((b"123456-node", b"provider", b"example", b"ua"))
        artifact = self.create_archive(
            {"platform_automation/runtime.py": b"host = '" + hostname + b"'\n"}
        )

        with self.assertRaisesRegex(ValueError, "non-allowlisted FQDN"):
            scanner.validate_artifact(artifact)

    def test_accepts_allowlisted_fqdns(self) -> None:
        artifact = self.create_archive(
            {
                "platform_automation/runtime.py": (
                    b"docs = 'app.example.invalid deep.example.test'\n"
                    b"schema = 'https://json-schema.org/example'\n"
                    b"image = 'ghcr.io/example/platform-example:latest'\n"
                )
            }
        )

        self.assertEqual(scanner.validate_artifact(artifact), 1)

    def test_accepts_markdown_filenames(self) -> None:
        artifact = self.create_archive(
            {
                "platform_automation/runtime.py": (
                    b"files = ['README.md', 'docs/runbook.md', 'v0.1.0.md']\n"
                )
            }
        )

        self.assertEqual(scanner.validate_artifact(artifact), 1)

    def test_rejects_global_ipv6_address(self) -> None:
        address = b":".join((b"2606", b"4700", b"4700", b"", b"1111"))
        artifact = self.create_archive(
            {"platform_automation/runtime.py": b"host = '" + address + b"'\n"}
        )

        with self.assertRaisesRegex(ValueError, "non-documentation IPv6"):
            scanner.validate_artifact(artifact)

    def test_accepts_documentation_and_private_ipv6_addresses(self) -> None:
        artifact = self.create_archive(
            {
                "platform_automation/runtime.py": (
                    b"docs = '2001:db8::10'\n"
                    b"ula = 'fd00::10'\n"
                    b"link_local = 'fe80::10'\n"
                    b"loopback = '::1'\n"
                )
            }
        )

        self.assertEqual(scanner.validate_artifact(artifact), 1)

    def test_rejects_tailscale_ipv6_address(self) -> None:
        address = b":".join((b"fd7a", b"115c", b"a1e0", b"", b"ab12", b"1"))
        artifact = self.create_archive(
            {"platform_automation/runtime.py": b"host = '" + address + b"'\n"}
        )

        with self.assertRaisesRegex(ValueError, "non-documentation IPv6"):
            scanner.validate_artifact(artifact)

    def test_rejects_marker_supplied_through_environment(self) -> None:
        marker = "legacy-" + "customer-image"
        artifact = self.create_archive(
            {"platform_automation/runtime.py": marker.encode("utf-8")}
        )

        with patch.dict(os.environ, {"CORE_FORBIDDEN_MARKERS": marker}, clear=True):
            with self.assertRaisesRegex(ValueError, "runtime-supplied private marker"):
                scanner.validate_artifact(artifact)

    def test_rejects_marker_supplied_through_file(self) -> None:
        marker = "private-" + "infrastructure-host"
        marker_file = Path(self.temporary_directory.name) / "forbidden-markers.txt"
        marker_file.write_text(f"{marker}\n", encoding="utf-8")
        artifact = self.create_archive(
            {"platform_automation/runtime.py": marker.encode("utf-8")}
        )

        with patch.dict(
            os.environ,
            {"CORE_FORBIDDEN_MARKERS_FILE": str(marker_file)},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "runtime-supplied private marker"):
                scanner.validate_artifact(artifact)

    def test_rejects_required_empty_marker_list(self) -> None:
        artifact = self.create_archive(
            {"platform_automation/runtime.py": b"PLATFORM_ROOT = '/opt/platform'\n"}
        )

        with patch.dict(
            os.environ,
            {"CORE_REQUIRE_FORBIDDEN_MARKERS": "1"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "must provide at least one marker"):
                scanner.validate_artifact(artifact)

    def test_accepts_required_nonempty_marker_list(self) -> None:
        artifact = self.create_archive(
            {"platform_automation/runtime.py": b"PLATFORM_ROOT = '/opt/platform'\n"}
        )

        with patch.dict(
            os.environ,
            {
                "CORE_FORBIDDEN_MARKERS": "known-private-value",
                "CORE_REQUIRE_FORBIDDEN_MARKERS": "1",
            },
            clear=True,
        ):
            self.assertEqual(scanner.validate_artifact(artifact), 1)

    def test_accepts_documentation_values(self) -> None:
        artifact = self.create_archive(
            {
                "platform_automation/runtime.py": (
                    b"host = '203.0.113.10'\ncontact = 'ops@example.invalid'\n"
                )
            }
        )

        self.assertEqual(scanner.validate_artifact(artifact), 1)

    def test_rejects_private_inventory_path(self) -> None:
        artifact = self.create_archive({"inventory/production.yml": b"all: {}\n"})

        with self.assertRaisesRegex(ValueError, "forbidden path component"):
            scanner.validate_artifact(artifact)


if __name__ == "__main__":
    unittest.main()
