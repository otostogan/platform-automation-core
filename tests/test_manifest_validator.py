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

    def docker_database_manifest(self) -> dict:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["database"] = {
            "mode": "docker",
            "postgres_major": 17,
            "backup_enabled": True,
            "backup": {
                "interval_minutes": 360,
                "retain": 14,
            },
        }
        return manifest

    def test_accepts_docker_database_with_backup(self) -> None:
        self.assertEqual(self.validate(self.docker_database_manifest()), [])

    def test_accepts_every_supported_postgres_major(self) -> None:
        for major in (16, 17, 18):
            with self.subTest(major=major):
                manifest = self.docker_database_manifest()
                manifest["database"]["postgres_major"] = major

                self.assertEqual(self.validate(manifest), [])

    def test_rejects_unsupported_postgres_major(self) -> None:
        for major in (13, 14, 15, 19, "17"):
            with self.subTest(major=major):
                manifest = self.docker_database_manifest()
                manifest["database"]["postgres_major"] = major

                errors = self.validate(manifest)

                self.assertTrue(
                    any(
                        error.startswith("$.database.postgres_major:")
                        for error in errors
                    ),
                    errors,
                )

    def test_rejects_backup_interval_outside_bounds(self) -> None:
        for interval in (0, 14, 1441, 360.5, "360"):
            with self.subTest(interval=interval):
                manifest = self.docker_database_manifest()
                manifest["database"]["backup"]["interval_minutes"] = interval

                errors = self.validate(manifest)

                self.assertTrue(
                    any(
                        error.startswith("$.database.backup.interval_minutes:")
                        for error in errors
                    ),
                    errors,
                )

    def test_rejects_backup_retain_outside_bounds(self) -> None:
        for retain in (0, 101, True):
            with self.subTest(retain=retain):
                manifest = self.docker_database_manifest()
                manifest["database"]["backup"]["retain"] = retain

                errors = self.validate(manifest)

                self.assertTrue(
                    any(
                        error.startswith("$.database.backup.retain:")
                        for error in errors
                    ),
                    errors,
                )

    def test_rejects_enabled_backup_without_cadence(self) -> None:
        """A manifest may not promise a backup while withholding its schedule."""
        manifest = self.docker_database_manifest()
        del manifest["database"]["backup"]

        errors = self.validate(manifest)

        self.assertTrue(
            any(error.startswith("$.database:") for error in errors),
            errors,
        )

    def test_rejects_backup_object_when_backups_are_disabled(self) -> None:
        """Dead configuration is a lie waiting to be believed."""
        manifest = self.docker_database_manifest()
        manifest["database"]["backup_enabled"] = False

        errors = self.validate(manifest)

        self.assertTrue(
            any(error.startswith("$.database:") for error in errors),
            errors,
        )

    def test_rejects_backup_object_for_external_database(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["database"]["backup"] = {
            "interval_minutes": 360,
            "retain": 14,
        }

        errors = self.validate(manifest)

        self.assertTrue(
            any(error.startswith("$.database:") for error in errors),
            errors,
        )

    def docker_database_compose(self) -> tuple[dict, dict]:
        manifest = self.docker_database_manifest()
        compose = copy.deepcopy(self.valid_compose)
        web = manifest["service"]["web"]
        service = compose["services"][web]

        if isinstance(service.get("networks"), list):
            service["networks"] = service["networks"] + ["db"]
        else:
            service.setdefault("networks", {})["db"] = None

        compose["networks"]["db"] = {
            "name": "${PLATFORM_DB_NETWORK:?PLATFORM_DB_NETWORK is required}",
            "external": True,
        }
        return manifest, compose

    def test_accepts_docker_database_compose(self) -> None:
        manifest, compose = self.docker_database_compose()

        self.assertEqual(
            self.validate_compose_contract(compose, manifest),
            [],
        )

    def test_docker_database_requires_db_network(self) -> None:
        manifest = self.docker_database_manifest()

        errors = self.validate_compose_contract(self.valid_compose, manifest)

        self.assertTrue(
            any(error.startswith("$.compose.networks.db:") for error in errors),
            errors,
        )

    def test_db_network_name_must_be_platform_interpolation(self) -> None:
        manifest, compose = self.docker_database_compose()
        compose["networks"]["db"]["name"] = "platform-db-example-lab"

        errors = self.validate_compose_contract(compose, manifest)

        self.assertTrue(
            any(error.startswith("$.compose.networks.db.name:") for error in errors),
            errors,
        )

    def test_web_service_must_join_db_network(self) -> None:
        manifest, compose = self.docker_database_compose()
        web = manifest["service"]["web"]
        networks = compose["services"][web]["networks"]

        if isinstance(networks, list):
            networks.remove("db")
        else:
            del networks["db"]

        errors = self.validate_compose_contract(compose, manifest)

        self.assertTrue(
            any("web service must join db" in error for error in errors),
            errors,
        )

    def test_external_database_must_not_reference_db_network(self) -> None:
        _, compose = self.docker_database_compose()

        errors = self.validate_compose_contract(compose, self.valid_manifest)

        self.assertTrue(
            any(
                error.startswith("$.compose.networks.db:") and "forbidden" in error
                for error in errors
            ),
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
