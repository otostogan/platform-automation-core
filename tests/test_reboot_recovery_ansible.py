import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE_ROOT = ROOT / "roles" / "reboot_recovery"


class RebootRecoveryAnsibleTest(unittest.TestCase):
    def test_installs_both_recovery_modules(self) -> None:
        defaults_path = ROOT / "roles" / "platform_cli" / "defaults" / "main.yml"
        defaults = yaml.safe_load(defaults_path.read_text(encoding="utf-8"))

        self.assertIn(
            "reboot_recovery.py",
            defaults["platform_cli_tool_files"],
        )
        self.assertIn(
            "reboot_recovery_entrypoint.py",
            defaults["platform_cli_tool_files"],
        )

    def test_recovery_role_runs_after_platform_cli(self) -> None:
        playbook_path = ROOT / "playbooks" / "converge.yml"
        playbook = yaml.safe_load(playbook_path.read_text(encoding="utf-8"))
        role_names = [item["role"] for item in playbook[0]["roles"]]

        self.assertLess(
            role_names.index("otostogan.platform.platform_cli"),
            role_names.index("otostogan.platform.reboot_recovery"),
        )

    def test_docker_requires_successful_recovery(self) -> None:
        service = (
            ROLE_ROOT / "templates" / "platform-secrets-recovery.service.j2"
        ).read_text(encoding="utf-8")
        drop_in = (ROLE_ROOT / "templates" / "docker-recovery.conf.j2").read_text(
            encoding="utf-8"
        )

        self.assertIn("Before=docker.service", service)
        self.assertIn(
            "Environment=PLATFORM_MINIMUM_AGE_RECIPIENTS={{ "
            "reboot_recovery_minimum_age_recipients }}",
            service,
        )
        self.assertIn(
            "After=local-fs.target systemd-tmpfiles-setup.service",
            service,
        )
        self.assertIn(
            "Requires={{ reboot_recovery_service_name }}",
            drop_in,
        )
        self.assertIn(
            "After={{ reboot_recovery_service_name }}",
            drop_in,
        )

    def test_role_reloads_systemd_without_starting_recovery(self) -> None:
        tasks_path = ROLE_ROOT / "tasks" / "main.yml"
        tasks = yaml.safe_load(tasks_path.read_text(encoding="utf-8"))
        systemd_task = next(
            task for task in tasks if "ansible.builtin.systemd_service" in task
        )
        configuration = systemd_task["ansible.builtin.systemd_service"]

        self.assertIs(configuration["daemon_reload"], True)
        self.assertNotIn("enabled", configuration)
        self.assertNotIn("state", configuration)

    def test_unit_templates_end_with_newline(self) -> None:
        for template_name in (
            "platform-secrets-recovery.service.j2",
            "docker-recovery.conf.j2",
        ):
            with self.subTest(template=template_name):
                content = (ROLE_ROOT / "templates" / template_name).read_bytes()
                self.assertTrue(content.endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()
