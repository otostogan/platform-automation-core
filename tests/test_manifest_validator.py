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

    def none_database_manifest(self) -> dict:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["database"] = {"mode": "none", "backup_enabled": False}
        del manifest["restore_validation"]
        return manifest

    def test_accepts_an_application_without_a_database(self) -> None:
        self.assertEqual(self.validate(self.none_database_manifest()), [])

    def test_no_database_still_lets_backup_enabled_false_be_spelled_out(self) -> None:
        manifest = self.none_database_manifest()
        del manifest["database"]["backup_enabled"]

        errors = self.validate(manifest)

        self.assertTrue(
            any("backup_enabled" in error for error in errors),
            errors,
        )

    def test_no_database_forbids_restore_validation(self) -> None:
        manifest = self.none_database_manifest()
        manifest["restore_validation"] = {"query": "SELECT 1"}

        errors = self.validate(manifest)

        self.assertTrue(
            any(
                error.startswith("$.restore_validation:") and "none" in error
                for error in errors
            ),
            errors,
        )

    def test_a_database_still_requires_restore_validation(self) -> None:
        for mode in ("external", "docker"):
            with self.subTest(mode=mode):
                manifest = (
                    self.docker_database_manifest()
                    if mode == "docker"
                    else copy.deepcopy(self.valid_manifest)
                )
                del manifest["restore_validation"]

                errors = self.validate(manifest)

                self.assertTrue(
                    any("restore_validation" in error for error in errors),
                    errors,
                )

    def test_no_database_forbids_postgres_major_backup_and_resources(self) -> None:
        for field, value in (
            ("postgres_major", 18),
            ("backup", {"interval_minutes": 15, "retain": 3}),
            ("resources", {"memory": "512m"}),
        ):
            with self.subTest(field=field):
                manifest = self.none_database_manifest()
                manifest["database"][field] = value

                errors = self.validate(manifest)

                self.assertTrue(
                    any(error.startswith("$.database") for error in errors),
                    errors,
                )

    def test_no_database_cannot_schedule_backups(self) -> None:
        manifest = self.none_database_manifest()
        manifest["database"]["backup_enabled"] = True
        manifest["database"]["backup"] = {"interval_minutes": 15, "retain": 3}

        errors = self.validate(manifest)

        self.assertTrue(
            any(error.startswith("$.database.backup_enabled:") for error in errors),
            errors,
        )

    def test_a_database_still_requires_postgres_major(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        del manifest["database"]["postgres_major"]

        errors = self.validate(manifest)

        self.assertTrue(
            any("postgres_major" in error for error in errors),
            errors,
        )

    def test_no_database_must_not_reference_db_network(self) -> None:
        _, compose = self.docker_database_compose()

        errors = self.validate_compose_contract(compose, self.none_database_manifest())

        self.assertTrue(
            any(
                error.startswith("$.compose.networks.db.name:")
                and "forbidden" in error
                and "no database" in error
                for error in errors
            ),
            errors,
        )

    def test_no_database_may_keep_its_own_db_network(self) -> None:
        compose = copy.deepcopy(self.valid_compose)
        compose["networks"]["db"] = {"name": "helper-net", "external": True}

        self.assertEqual(
            self.validate_compose_contract(compose, self.none_database_manifest()),
            [],
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

    def resourced_manifest(self, **resources) -> dict:
        manifest = self.docker_database_manifest()
        manifest["database"]["resources"] = resources
        return manifest

    def test_accepts_database_resources(self) -> None:
        for resources in (
            {"memory": "1536m"},
            {"memory": "2g", "shared_buffers": "512m"},
            {"memory": "1g", "shared_buffers": "512m"},
        ):
            with self.subTest(resources=resources):
                self.assertEqual(
                    self.validate(self.resourced_manifest(**resources)), []
                )

    def test_database_resources_need_a_unit(self) -> None:
        for memory in ("1536", "1.5g", "1536M", "0m", 1536, "1" * 400 + "m"):
            with self.subTest(memory=memory):
                errors = self.validate(self.resourced_manifest(memory=memory))

                self.assertTrue(
                    any(
                        error.startswith("$.database.resources.memory:")
                        for error in errors
                    ),
                    errors,
                )

    def test_database_resources_need_the_limit(self) -> None:
        errors = self.validate(self.resourced_manifest(shared_buffers="256m"))

        self.assertTrue(
            any(error.startswith("$.database.resources:") for error in errors), errors
        )

    def test_database_memory_has_a_floor(self) -> None:
        errors = self.validate(self.resourced_manifest(memory="128m"))

        self.assertIn("$.database.resources.memory: at least 256m", errors)

    def test_shared_buffers_leave_room_for_the_rest(self) -> None:
        errors = self.validate(
            self.resourced_manifest(memory="1g", shared_buffers="600m")
        )

        self.assertIn(
            "$.database.resources.shared_buffers: at most half of memory", errors
        )

    def test_external_database_takes_no_resources(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["database"]["resources"] = {"memory": "1g"}

        errors = self.validate(manifest)

        self.assertTrue(
            any(error.startswith("$.database:") for error in errors), errors
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
                error.startswith("$.compose.networks.db.name:") and "forbidden" in error
                for error in errors
            ),
            errors,
        )

    def test_migration_service_must_join_db_network(self) -> None:
        manifest, compose = self.docker_database_compose()
        manifest["deployment"] = {
            "migration_service": "migrator",
            "migration_command": ["bin/migrate"],
        }
        compose["services"]["migrator"] = {
            "image": "${PLATFORM_IMAGE:?PLATFORM_IMAGE is required}",
        }

        errors = self.validate_compose_contract(compose, manifest)

        self.assertTrue(
            any("migration service must join db" in error for error in errors),
            errors,
        )

        compose["services"]["migrator"]["networks"] = ["db"]

        self.assertEqual(
            self.validate_compose_contract(compose, manifest),
            [],
        )

    def test_external_database_may_keep_its_own_db_network(self) -> None:
        """The key stays free; only the platform interpolation is claimed."""
        compose = copy.deepcopy(self.valid_compose)
        compose["networks"]["db"] = {"name": "helper-net", "external": True}

        errors = self.validate_compose_contract(compose, self.valid_manifest)

        self.assertEqual(errors, [])

    def test_accepts_a_metrics_endpoint(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["service"]["metrics"] = {"path": "/metrics", "port": 3000}

        self.assertEqual(self.validate(manifest), [])
        self.assertEqual(
            self.validate_compose_contract(self.valid_compose, manifest), []
        )

    def test_rejects_a_metrics_path_that_could_carry_nginx_syntax(self) -> None:
        # "." and ".." are resolved by nginx before it matches a location, so
        # a guard written for the literal path would not cover the real one.
        for path in (
            "metrics",
            "/",
            "/metrics;",
            "/a b",
            "/metrics/",
            "/m{x}",
            "/..",
            "/.",
            "/foo/../metrics",
            "/foo/./metrics",
            "/metrics/..",
        ):
            manifest = copy.deepcopy(self.valid_manifest)
            manifest["service"]["metrics"] = {"path": path, "port": 3000}
            self.assertTrue(self.validate(manifest), path)

    def test_accepts_dots_that_are_part_of_a_name(self) -> None:
        for path in ("/metrics.txt", "/.well-known/metrics", "/v1.2/metrics", "/a..b"):
            manifest = copy.deepcopy(self.valid_manifest)
            manifest["service"]["metrics"] = {"path": path, "port": 3000}
            self.assertEqual(self.validate(manifest), [], path)

    def test_rejects_an_incomplete_metrics_block(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["service"]["metrics"] = {"path": "/metrics"}
        self.assertTrue(self.validate(manifest))

    def test_the_metrics_port_must_be_exposed_by_the_web_service(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["service"]["metrics"] = {"path": "/metrics", "port": 9464}

        errors = self.validate_compose_contract(self.valid_compose, manifest)

        self.assertIn(
            "$.compose.services.app.expose: must expose metrics port 9464", errors
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


class DomainServiceTest(ManifestValidatorTest):
    """A domain may belong to a helper service; the checks name what would fail silently."""

    def with_helper(self):
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["domains"].append(
            {
                "host": "mail.app.example.invalid",
                "tls": True,
                "service": "mailpit",
                "nginx": {},
            }
        )
        compose = copy.deepcopy(self.valid_compose)
        compose["services"]["mailpit"] = {
            "image": "docker.io/axllent/mailpit@sha256:" + "0" * 64,
            "expose": ["8025"],
            "networks": ["edge"],
        }
        return manifest, compose

    def test_a_helper_domain_is_accepted_when_the_service_is_on_edge(self) -> None:
        manifest, compose = self.with_helper()
        self.assertEqual(self.validate(manifest), [])
        self.assertEqual(self.validate_compose_contract(compose, manifest), [])

    def test_a_domain_on_a_missing_service_is_named(self) -> None:
        manifest, compose = self.with_helper()
        del compose["services"]["mailpit"]
        errors = self.validate_compose_contract(compose, manifest)
        self.assertTrue(
            any(e.startswith("$.domains[1].service:") for e in errors), errors
        )

    def test_a_helper_off_the_edge_network_is_refused(self) -> None:
        manifest, compose = self.with_helper()
        compose["services"]["mailpit"]["networks"] = ["mail"]
        errors = self.validate_compose_contract(compose, manifest)
        self.assertTrue(
            any(
                "mailpit.networks" in e and "mail.app.example.invalid" in e
                for e in errors
            ),
            errors,
        )

    def test_the_web_service_must_keep_at_least_one_domain(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        for domain in manifest["domains"]:
            domain["service"] = "mailpit"
        errors = self.validate(manifest)
        self.assertIn(
            "$.domains: at least one domain must belong to the web service", errors
        )

    def test_naming_the_web_service_explicitly_is_the_default(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["domains"][0]["service"] = manifest["service"]["web"]
        self.assertEqual(self.validate(manifest), [])
        self.assertEqual(
            self.validate_compose_contract(self.valid_compose, manifest), []
        )


class DomainAuthTest(ManifestValidatorTest):
    def test_auth_needs_both_username_and_password_env(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["domains"][0]["auth"] = {"username": "team"}
        errors = self.validate(manifest)
        self.assertTrue(any("password_env" in e for e in errors), errors)

        manifest["domains"][0]["auth"] = {
            "username": "team",
            "password_env": "UI_PASSWORD",
        }
        self.assertEqual(self.validate(manifest), [])

    def test_auth_never_carries_a_password(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["domains"][0]["auth"] = {
            "username": "team",
            "password_env": "X",
            "password": "no",
        }
        errors = self.validate(manifest)
        self.assertTrue(any("password" in e for e in errors), errors)

    def test_auth_may_name_the_paths_it_guards(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["domains"][0]["auth"] = {
            "username": "team",
            "password_env": "X",
            "paths": ["/admin", "/docs/internal"],
        }
        self.assertEqual(self.validate(manifest), [])

    def test_auth_paths_are_real_paths_and_never_the_root(self) -> None:
        for paths in ([], ["/"], ["admin"], ["/a/../b"], ["/admin", "/admin"], ["/x/"]):
            with self.subTest(paths=paths):
                manifest = copy.deepcopy(self.valid_manifest)
                manifest["domains"][0]["auth"] = {
                    "username": "team",
                    "password_env": "X",
                    "paths": paths,
                }
                errors = self.validate(manifest)
                self.assertTrue(
                    any("$.domains[0].auth.paths" in e for e in errors), errors
                )

    def test_auth_paths_cannot_name_the_guarded_metrics_path(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        port = manifest["service"]["internal_port"]
        manifest["service"]["metrics"] = {"path": "/metrics", "port": port}
        manifest["domains"][0]["auth"] = {
            "username": "team",
            "password_env": "X",
            "paths": ["/metrics"],
        }
        errors = self.validate(manifest)
        self.assertTrue(any("under the metrics path" in e for e in errors), errors)

        # below it a protected location would be more specific than the guard
        manifest["domains"][0]["auth"]["paths"] = ["/metrics/export"]
        errors = self.validate(manifest)
        self.assertTrue(any("under the metrics path" in e for e in errors), errors)
        # a sibling that merely shares the prefix string is fine
        manifest["domains"][0]["auth"]["paths"] = ["/metrics-ui"]
        self.assertEqual(self.validate(manifest), [])

        # on a port of its own there is no guard, and so nothing to collide with
        manifest["domains"][0]["auth"]["paths"] = ["/metrics"]
        manifest["service"]["metrics"]["port"] = port + 1
        self.assertEqual(self.validate(manifest), [])

    def test_a_path_scoped_host_name_leaves_room_for_the_suffix(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        host = ("a" * 61 + ".") * 3 + "a" * 59 + ".invalid"
        self.assertEqual(len(host), 253)  # the longest host the schema accepts
        manifest["domains"][0]["host"] = host
        self.assertEqual(self.validate(manifest), [])

        manifest["domains"][0]["auth"] = {
            "username": "team",
            "password_env": "X",
            "paths": ["/admin"],
        }
        errors = self.validate(manifest)
        self.assertTrue(any("at most 249 characters" in e for e in errors), errors)

    def test_username_cannot_contain_a_colon(self) -> None:
        manifest = copy.deepcopy(self.valid_manifest)
        manifest["domains"][0]["auth"] = {"username": "te:am", "password_env": "X"}
        self.assertNotEqual(self.validate(manifest), [])


if __name__ == "__main__":
    unittest.main()
