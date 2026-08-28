import copy
import unittest
from pathlib import Path


from platform_automation.nginx_config import (  # noqa: E402
    NginxConfigError,
    generate_vhost_fragments,
)
from platform_automation.validate_manifest import load_yaml  # noqa: E402


MANIFEST_PATH = (
    Path(__file__).parent / "fixtures" / "app-contract" / "deploy" / "platform.yml"
)


class NginxConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.valid_manifest = load_yaml(MANIFEST_PATH)

    def manifest(self) -> dict:
        return copy.deepcopy(self.valid_manifest)

    def test_generates_typed_vhost_fragment(self) -> None:
        fragments = generate_vhost_fragments(self.manifest())

        self.assertEqual(
            fragments["app.example.invalid"],
            (
                "# Managed by platform. Do not edit manually.\n"
                "client_max_body_size 20m;\n"
                "proxy_connect_timeout 10s;\n"
                "proxy_read_timeout 60s;\n"
                "proxy_send_timeout 60s;\n"
                "gzip on;\n"
                "gzip_vary on;\n"
                "gzip_types application/json text/css;\n"
            ),
        )

    def test_generates_gzip_off(self) -> None:
        manifest = self.manifest()
        manifest["domains"][0]["nginx"]["gzip"] = {
            "enabled": False,
            "types": [],
        }

        fragments = generate_vhost_fragments(manifest)

        self.assertIn(
            "gzip off;\n",
            fragments["app.example.invalid"],
        )
        self.assertNotIn(
            "gzip_types",
            fragments["app.example.invalid"],
        )

    def test_rejects_empty_gzip_types_when_enabled(self) -> None:
        manifest = self.manifest()
        manifest["domains"][0]["nginx"]["gzip"]["types"] = []

        with self.assertRaisesRegex(
            NginxConfigError,
            "gzip.types cannot be empty",
        ):
            generate_vhost_fragments(manifest)

    def test_rejects_raw_snippet_without_allowlist(self) -> None:
        manifest = self.manifest()
        manifest["domains"][0]["nginx"][
            "raw_vhost_snippet"
        ] = "add_header X-Lab enabled;"

        with self.assertRaisesRegex(
            NginxConfigError,
            "Raw nginx snippets are not allowed",
        ):
            generate_vhost_fragments(manifest)

    def test_allows_raw_snippets_for_allowlisted_project(self) -> None:
        manifest = self.manifest()
        nginx = manifest["domains"][0]["nginx"]

        nginx["raw_vhost_snippet"] = "add_header X-Lab enabled;"
        nginx["raw_location_snippet"] = "proxy_buffering off;"

        fragments = generate_vhost_fragments(
            manifest,
            allowed_raw_projects={"example"},
        )

        self.assertIn(
            "add_header X-Lab enabled;",
            fragments["app.example.invalid"],
        )
        self.assertEqual(
            fragments["app.example.invalid_location"],
            (
                "# Managed by platform. Do not edit manually.\n"
                "# Raw location snippet for allowlisted project: example\n"
                "proxy_buffering off;\n"
            ),
        )

    def test_rejects_duplicate_domain(self) -> None:
        manifest = self.manifest()
        manifest["domains"].append(copy.deepcopy(manifest["domains"][0]))

        with self.assertRaisesRegex(
            NginxConfigError,
            "Duplicate domain host: app.example.invalid",
        ):
            generate_vhost_fragments(manifest)


if __name__ == "__main__":
    unittest.main()
