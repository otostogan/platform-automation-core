import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


from platform_automation.reboot_recovery import (  # noqa: E402
    RebootRecoveryError,
    discover_recovery_scopes,
)
from platform_automation.reboot_recovery_entrypoint import (  # noqa: E402
    BootRecoveryError,
    main,
    run_boot_recovery,
)


class RebootRecoveryDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.projects_root = self.base / "projects"
        self.projects_root.mkdir(mode=0o750)
        self.projects_root.chmod(0o750)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_boot_recovery_for_test(self) -> list[Path]:
        return run_boot_recovery(
            projects_root=self.projects_root,
            releases_root=self.base / "releases",
            lock_root=self.base / "locks",
            runtime_root=self.base / "runtime-secrets",
            age_key_file=self.base / "age.key",
            sops_executable=self.base / "sops",
        )

    def test_boot_recovery_requires_root_before_discovery(self) -> None:
        with patch(
            "platform_automation.reboot_recovery_entrypoint.os.geteuid",
            return_value=501,
        ):
            with patch(
                "platform_automation.reboot_recovery_entrypoint.discover_recovery_scopes"
            ) as discover:
                with self.assertRaisesRegex(
                    BootRecoveryError,
                    "must run as root",
                ):
                    self.run_boot_recovery_for_test()

        discover.assert_not_called()

    def test_boot_recovery_processes_scopes_in_discovered_order(self) -> None:
        scopes = [
            ("alpha", "lab"),
            ("alpha", "staging"),
            ("zeta", "production"),
        ]
        first_path = self.base / "first.env"
        third_path = self.base / "third.env"

        with patch(
            "platform_automation.reboot_recovery_entrypoint.os.geteuid",
            return_value=0,
        ):
            with patch(
                "platform_automation.reboot_recovery_entrypoint.discover_recovery_scopes",
                return_value=scopes,
            ):
                with patch(
                    "platform_automation.reboot_recovery_entrypoint."
                    "recover_project_environment_secrets",
                    side_effect=[
                        first_path,
                        None,
                        third_path,
                    ],
                ) as recover:
                    result = self.run_boot_recovery_for_test()

        self.assertEqual(
            result,
            [
                first_path,
                third_path,
            ],
        )
        self.assertEqual(
            [
                (
                    invocation.kwargs["project"],
                    invocation.kwargs["environment"],
                )
                for invocation in recover.call_args_list
            ],
            scopes,
        )

    def test_boot_recovery_stops_after_scope_failure(self) -> None:
        scopes = [
            ("alpha", "lab"),
            ("alpha", "staging"),
            ("zeta", "production"),
        ]

        with patch(
            "platform_automation.reboot_recovery_entrypoint.os.geteuid",
            return_value=0,
        ):
            with patch(
                "platform_automation.reboot_recovery_entrypoint.discover_recovery_scopes",
                return_value=scopes,
            ):
                with patch(
                    "platform_automation.reboot_recovery_entrypoint."
                    "recover_project_environment_secrets",
                    side_effect=[
                        self.base / "first.env",
                        RebootRecoveryError("unsafe recovery state"),
                    ],
                ) as recover:
                    with self.assertRaisesRegex(
                        RebootRecoveryError,
                        "unsafe recovery state",
                    ):
                        self.run_boot_recovery_for_test()

        self.assertEqual(recover.call_count, 2)

    def create_scope(
        self,
        project: str,
        environment: str,
    ) -> None:
        project_directory = self.projects_root / project
        environment_directory = project_directory / environment
        ledger_directory = environment_directory / "ledger"

        ledger_directory.mkdir(
            mode=0o700,
            parents=True,
        )

        for directory in (
            project_directory,
            environment_directory,
            ledger_directory,
        ):
            directory.chmod(0o700)

    def test_discovers_scopes_in_stable_order(self) -> None:
        self.create_scope("zeta", "production")
        self.create_scope("alpha", "staging")
        self.create_scope("alpha", "lab")

        self.assertEqual(
            discover_recovery_scopes(self.projects_root),
            [
                ("alpha", "lab"),
                ("alpha", "staging"),
                ("zeta", "production"),
            ],
        )

    def test_rejects_project_symbolic_link(self) -> None:
        outside_directory = self.base / "outside"
        outside_directory.mkdir(mode=0o700)
        outside_directory.chmod(0o700)

        (self.projects_root / "example").symlink_to(
            outside_directory,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(
            RebootRecoveryError,
            "cannot be a symbolic link",
        ):
            discover_recovery_scopes(self.projects_root)

    def test_rejects_unsafe_directory_permissions(self) -> None:
        self.create_scope("example", "lab")
        (self.projects_root / "example" / "lab").chmod(0o755)

        with self.assertRaisesRegex(
            RebootRecoveryError,
            "unsafe permissions",
        ):
            discover_recovery_scopes(self.projects_root)


class RebootRecoveryEntrypointTest(unittest.TestCase):
    def test_main_reports_success(self) -> None:
        output = StringIO()

        with patch(
            "platform_automation.reboot_recovery_entrypoint.run_boot_recovery",
            return_value=[
                Path("/run/platform/secrets/example/lab/1/app.env"),
                Path("/run/platform/secrets/other/staging/2/app.env"),
            ],
        ) as recover:
            with redirect_stdout(output):
                exit_code = main([])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            output.getvalue(),
            "boot recovery completed: 2 secret file(s) restored\n",
        )
        recover.assert_called_once()

    def test_main_converts_recovery_error_to_exit_code(self) -> None:
        error_output = StringIO()

        with patch(
            "platform_automation.reboot_recovery_entrypoint.run_boot_recovery",
            side_effect=RebootRecoveryError("unsafe recovery state"),
        ):
            with redirect_stderr(error_output):
                exit_code = main([])

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            error_output.getvalue(),
            "boot recovery error: unsafe recovery state\n",
        )

    def test_main_rejects_arguments_before_recovery(self) -> None:
        error_output = StringIO()

        with patch(
            "platform_automation.reboot_recovery_entrypoint.run_boot_recovery"
        ) as recover:
            with redirect_stderr(error_output):
                exit_code = main(["unexpected"])

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            error_output.getvalue(),
            "boot recovery error: " "command-line arguments are not supported\n",
        )
        recover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
