import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE_ROOT = ROOT
ROLE_ROOT = ROOT / "roles" / "vps_readiness"


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class VpsReadinessAnsibleTest(unittest.TestCase):
    def test_playbook_runs_only_the_readiness_role(self) -> None:
        playbook = load_yaml(ANSIBLE_ROOT / "playbooks" / "readiness.yml")

        self.assertTrue(playbook[0]["gather_facts"])
        self.assertEqual(
            [item["role"] for item in playbook[0]["roles"]],
            ["otostogan.platform.vps_readiness"],
        )

    def test_defaults_are_fail_safe(self) -> None:
        defaults = load_yaml(ROLE_ROOT / "defaults" / "main.yml")

        self.assertEqual(defaults["vps_readiness_phase"], "pre")
        self.assertEqual(defaults["vps_readiness_output"], "human")
        self.assertIs(defaults["vps_readiness_fail_on_error"], True)
        self.assertEqual(
            defaults["vps_readiness_allowed_public_tcp_ports"],
            [80, 443],
        )

    def test_remote_probes_are_explicitly_read_only(self) -> None:
        for filename in ("pre.yml", "post.yml"):
            tasks = load_yaml(ROLE_ROOT / "tasks" / filename)
            command_tasks = [
                task for task in tasks if "ansible.builtin.command" in task
            ]

            self.assertGreater(len(command_tasks), 0)
            for task in command_tasks:
                self.assertIs(task.get("changed_when"), False, task["name"])
                self.assertIs(task.get("failed_when"), False, task["name"])
                self.assertIs(task.get("check_mode"), False, task["name"])

    def test_post_checks_cover_platform_boundaries(self) -> None:
        post_tasks = load_yaml(ROLE_ROOT / "tasks" / "post.yml")
        rendered = yaml.safe_dump(post_tasks)

        for expected in (
            "tailscale",
            "SSH_CONNECTION",
            "ufw",
            "DOCKER-USER",
            "PLATFORM-DOCKER-INGRESS",
            "docker_published_ports",
            "listening_tcp_ports",
            "proxy_health",
        ):
            self.assertIn(expected, rendered)

    def test_systemd_checks_report_each_required_unit(self) -> None:
        post_tasks = load_yaml(ROLE_ROOT / "tasks" / "post.yml")
        probe = next(
            task
            for task in post_tasks
            if task["name"] == "Probe required systemd units"
        )
        record = next(
            task
            for task in post_tasks
            if task["name"] == "Record required systemd unit readiness"
        )

        self.assertEqual(
            probe["loop"],
            "{{ vps_readiness_required_services }}",
        )
        command = probe["ansible.builtin.command"]["argv"]
        self.assertIn("--property=ActiveState", command)
        self.assertIn("--property=UnitFileState", command)

        rendered = yaml.safe_dump(record)
        self.assertIn("ActiveState=active", rendered)
        self.assertIn("UnitFileState=enabled", rendered)
        self.assertIn("systemd_", rendered)
        self.assertIn("item.item", rendered)

    def test_report_is_printed_before_final_failure(self) -> None:
        tasks = load_yaml(ROLE_ROOT / "tasks" / "main.yml")
        names = [task["name"] for task in tasks]

        final_failure = names.index(
            "Fail after reporting unsuccessful VPS readiness checks"
        )
        self.assertLess(
            names.index("Print human-readable VPS readiness report"),
            final_failure,
        )
        self.assertLess(
            names.index("Print JSON VPS readiness report"),
            final_failure,
        )


if __name__ == "__main__":
    unittest.main()
