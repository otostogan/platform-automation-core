import copy
import unittest
from pathlib import Path
from typing import Any

from platform_automation.contract_resources import contract_path

from platform_automation.validate_manifest import (  # noqa: E402
    load_json,
    load_yaml,
    validate_compose,
    validate_manifest,
)


SCHEMA_PATH = contract_path("platform-v1.schema.json")
EXAMPLE_ROOT = Path(__file__).parent / "fixtures" / "app-contract"
MANIFEST_PATH = EXAMPLE_ROOT / "deploy" / "platform.yml"
COMPOSE_PATH = EXAMPLE_ROOT / "deploy" / "compose.yml"


class ManifestValidatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_json(SCHEMA_PATH)
        cls.valid_manifest = load_yaml(MANIFEST_PATH)
        cls.valid_compose = load_yaml(COMPOSE_PATH)

    def validate(self, manifest: Any) -> list[str]:
        return validate_manifest(manifest, self.schema)

    def validate_compose_contract(
        self,
        compose: Any,
        manifest: Any = None,
    ) -> list[str]:
        selected_manifest = self.valid_manifest if manifest is None else manifest

        return validate_compose(
            selected_manifest,
            compose,
        )

    def test_accepts_valid_manifest(self) -> None:
        self.assertEqual(self.validate(self.valid_manifest), [])

    def test_accepts_valid_compose(self) -> None:
        self.assertEqual(
            self.validate_compose_contract(self.valid_compose),
            [],
        )

    def test_rejects_image_tag(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["image"]["repository"] = "ghcr.io/company/example:v1"

        errors = self.validate(manifest)

        self.assertTrue(
            any(error.startswith("$.image.repository:") for error in errors),
            errors,
        )

    def test_rejects_unknown_field(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["unexpected"] = True

        errors = self.validate(manifest)

        self.assertTrue(
            any("Additional properties are not allowed" in error for error in errors),
            errors,
        )

    def test_rejects_invalid_nginx_size(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["domains"][0]["nginx"]["client_max_body_size"] = "20mb"

        errors = self.validate(manifest)

        self.assertTrue(
            any(
                error.startswith("$.domains[0].nginx.client_max_body_size:")
                for error in errors
            ),
            errors,
        )

    def test_rejects_empty_gzip_types_when_enabled(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["domains"][0]["nginx"]["gzip"] = {
            "enabled": True,
            "types": [],
        }

        errors = self.validate(manifest)

        self.assertTrue(
            any(error.startswith("$.domains[0].nginx.gzip.types:") for error in errors),
            errors,
        )

    def test_accepts_empty_gzip_types_when_disabled(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["domains"][0]["nginx"]["gzip"] = {
            "enabled": False,
            "types": [],
        }

        self.assertEqual(self.validate(manifest), [])

    def test_rejects_duplicate_domain_host(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["domains"].append(copy.deepcopy(manifest["domains"][0]))

        errors = self.validate(manifest)

        self.assertIn(
            (
                "$.domains[1].host: duplicate domain host "
                "app.example.invalid; first declared at $.domains[0].host"
            ),
            errors,
        )

    def test_rejects_incomplete_migration(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        del manifest["deployment"]["migration_command"]

        errors = self.validate(manifest)

        self.assertTrue(
            any(error.startswith("$.deployment:") for error in errors),
            errors,
        )

    def test_rejects_platform_backup_for_external_database(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["database"]["backup_enabled"] = True

        errors = self.validate(manifest)

        self.assertTrue(
            any(error.startswith("$.database.backup_enabled:") for error in errors),
            errors,
        )

    def test_rejects_compose_build(self) -> None:
        compose = copy.deepcopy(self.valid_compose)
        compose["services"]["app"]["build"] = "."

        errors = self.validate_compose_contract(compose)

        self.assertIn(
            "$.compose.services.app.build: build is forbidden",
            errors,
        )

    def test_rejects_compose_host_ports(self) -> None:
        compose = copy.deepcopy(self.valid_compose)
        compose["services"]["app"]["ports"] = ["3000:3000"]

        errors = self.validate_compose_contract(compose)

        self.assertIn(
            "$.compose.services.app.ports: " "host port publishing is forbidden",
            errors,
        )

    def test_rejects_floating_service_image(self) -> None:
        compose = copy.deepcopy(self.valid_compose)
        compose["services"]["worker"]["image"] = "nginx:latest"

        errors = self.validate_compose_contract(compose)

        self.assertTrue(
            any(
                error.startswith("$.compose.services.worker.image:") for error in errors
            ),
            errors,
        )

    def test_rejects_missing_web_service(self) -> None:
        compose = copy.deepcopy(self.valid_compose)
        del compose["services"]["app"]

        errors = self.validate_compose_contract(compose)

        self.assertTrue(
            any(error.startswith("$.service.web:") for error in errors),
            errors,
        )

    def test_rejects_wrong_exposed_port(self) -> None:
        compose = copy.deepcopy(self.valid_compose)
        compose["services"]["app"]["expose"] = ["8080"]

        errors = self.validate_compose_contract(compose)

        self.assertTrue(
            any(error.startswith("$.compose.services.app.expose:") for error in errors),
            errors,
        )

    def test_rejects_web_without_edge_network(self) -> None:
        compose = copy.deepcopy(self.valid_compose)
        compose["services"]["app"]["networks"] = ["private"]

        errors = self.validate_compose_contract(compose)

        self.assertTrue(
            any(
                error.startswith("$.compose.services.app.networks:") for error in errors
            ),
            errors,
        )

    def test_rejects_host_network_mode(self) -> None:
        compose = copy.deepcopy(self.valid_compose)
        compose["services"]["worker"]["network_mode"] = "host"

        errors = self.validate_compose_contract(compose)

        self.assertTrue(
            any(
                error.startswith("$.compose.services.worker.network_mode:")
                for error in errors
            ),
            errors,
        )


if __name__ == "__main__":
    unittest.main()
