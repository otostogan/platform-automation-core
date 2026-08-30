import ast
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PACKAGE = ROOT / "src" / "platform_automation"
ROLE_DEFAULTS = ROOT / "roles" / "platform_cli" / "defaults" / "main.yml"

# Entry points installed from the wheel on the controller and in CI. They are
# deliberately absent from the host, which only runs the server-side CLI.
CONTROLLER_ONLY_MODULES = {
    "acme_readiness.py",
    "build_bundle.py",
    "bundle_action.py",
}


class PlatformCliRoleFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.defaults = yaml.safe_load(ROLE_DEFAULTS.read_text(encoding="utf-8"))
        self.installed = set(self.defaults["platform_cli_tool_files"])

    def test_every_runtime_module_is_installed_or_declared_controller_only(
        self,
    ) -> None:
        modules = {path.name for path in RUNTIME_PACKAGE.glob("*.py")}
        unaccounted = modules - self.installed - CONTROLLER_ONLY_MODULES

        self.assertEqual(
            unaccounted,
            set(),
            "modules missing from platform_cli_tool_files; add them to the "
            "role or declare them controller-only",
        )

    def test_role_does_not_install_a_module_that_no_longer_exists(self) -> None:
        for name in sorted(self.installed):
            with self.subTest(name=name):
                self.assertTrue((RUNTIME_PACKAGE / name).is_file())

    def test_installed_modules_never_import_a_controller_only_module(self) -> None:
        """The host has no wheel, so a stray import would break the CLI."""
        controller_names = {
            name.removesuffix(".py") for name in CONTROLLER_ONLY_MODULES
        }

        for name in sorted(self.installed):
            if not name.endswith(".py"):
                continue

            tree = ast.parse((RUNTIME_PACKAGE / name).read_text(encoding="utf-8"))

            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue

                if node.level and node.module in controller_names:
                    self.fail(f"{name} imports controller-only {node.module}")

    def test_units_never_rely_on_shell_expansion(self) -> None:
        """systemd expands ${...} itself, before any shell sees it.

        A unit that writes shell parameter expansion gets empty strings
        substituted by systemd and fails at runtime, which no test without a
        real systemd can observe. Refusing the syntax outright is what can be
        checked here.
        """
        units = sorted(
            (ROOT / "roles" / "platform_cli" / "templates").glob("*.service.j2")
        )

        self.assertTrue(units)

        for unit in units:
            with self.subTest(unit=unit.name):
                for line in unit.read_text(encoding="utf-8").splitlines():
                    if not line.startswith(("ExecStart", "ExecStop", "Exec")):
                        continue

                    self.assertNotIn("${", line, unit.name)

    def test_every_contract_is_installed(self) -> None:
        contracts = {
            path.name for path in (RUNTIME_PACKAGE / "contracts").glob("*.json")
        }

        self.assertEqual(
            contracts,
            set(self.defaults["platform_cli_contract_files"]),
        )


if __name__ == "__main__":
    unittest.main()
