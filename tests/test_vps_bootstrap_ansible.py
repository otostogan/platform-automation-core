import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ANSIBLE_ROOT = ROOT
CONSUMER_INFRA_ROOT = ROOT / "examples" / "consumer" / "company-infra"


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class VpsBootstrapAnsibleTest(unittest.TestCase):
    def test_bootstrap_does_not_harden_or_filter_remote_access(self) -> None:
        playbook = load_yaml(ANSIBLE_ROOT / "playbooks" / "bootstrap.yml")
        role_names = [item["role"] for item in playbook[0]["roles"]]

        self.assertEqual(
            role_names,
            [
                "otostogan.platform.common",
                "otostogan.platform.users",
                "otostogan.platform.platform_base",
                "otostogan.platform.tailscale",
            ],
        )
        self.assertNotIn("otostogan.platform.ssh", role_names)
        self.assertNotIn("otostogan.platform.firewall", role_names)

    def test_full_convergence_starts_with_access_guard(self) -> None:
        playbook = load_yaml(ANSIBLE_ROOT / "playbooks" / "converge.yml")
        role_names = [item["role"] for item in playbook[0]["roles"]]

        self.assertEqual(role_names[0], "otostogan.platform.access_guard")
        self.assertLess(
            role_names.index("otostogan.platform.access_guard"),
            role_names.index("otostogan.platform.ssh"),
        )
        self.assertLess(
            role_names.index("otostogan.platform.access_guard"),
            role_names.index("otostogan.platform.firewall"),
        )

    def test_access_guard_requires_ops_tailnet_ssh(self) -> None:
        defaults = load_yaml(
            ANSIBLE_ROOT / "roles" / "access_guard" / "defaults" / "main.yml"
        )
        tasks = load_yaml(
            ANSIBLE_ROOT / "roles" / "access_guard" / "tasks" / "main.yml"
        )
        guard = next(
            task
            for task in tasks
            if task["name"] == "Require a verified tailnet operations session"
        )["ansible.builtin.assert"]
        assertions = "\n".join(guard["that"])

        self.assertIs(defaults["access_guard_enabled"], True)
        self.assertIn(
            "users_ops_name",
            defaults["access_guard_expected_user"],
        )
        self.assertEqual(
            defaults["access_guard_tailscale_interface"],
            "tailscale0",
        )
        self.assertIn("ansible_user == access_guard_expected_user", assertions)
        self.assertIn('access_guard_backend_state == "Running"', assertions)
        self.assertIn("access_guard_ssh_destination", assertions)
        self.assertIn("access_guard_tailscale_addresses.stdout_lines", assertions)

    def test_tailscale_supports_secret_safe_noninteractive_enrollment(self) -> None:
        defaults = load_yaml(
            ANSIBLE_ROOT / "roles" / "tailscale" / "defaults" / "main.yml"
        )
        tasks = load_yaml(ANSIBLE_ROOT / "roles" / "tailscale" / "tasks" / "main.yml")
        enrollment = next(
            task
            for task in tasks
            if task["name"] == "Enroll Tailscale non-interactively"
        )
        block = enrollment["block"]
        copy_task = next(
            task
            for task in block
            if task["name"] == "Install transient Tailscale auth key"
        )
        join_task = next(
            task for task in block if task["name"] == "Join host to Tailscale"
        )
        cleanup = enrollment["always"][0]

        self.assertEqual(defaults["tailscale_auth_key_source"], "")
        self.assertEqual(
            defaults["tailscale_auth_key_temporary_path"],
            "/run/platform/tailscale-auth.key",
        )
        self.assertEqual(copy_task["ansible.builtin.copy"]["mode"], "0600")
        self.assertIs(copy_task["no_log"], True)
        self.assertIn("--auth-key=file:", join_task["ansible.builtin.command"]["argv"])
        self.assertEqual(cleanup["ansible.builtin.file"]["state"], "absent")

    def test_deploy_user_does_not_require_permanent_ssh_key(self) -> None:
        tasks = load_yaml(ANSIBLE_ROOT / "roles" / "users" / "tasks" / "main.yml")

        validation = next(
            task
            for task in tasks
            if task["name"] == "Validate required operations SSH public keys"
        )["ansible.builtin.assert"]

        self.assertEqual(
            validation["that"],
            ["users_ops_ssh_keys | length > 0"],
        )

        deploy_keys = next(
            task
            for task in tasks
            if task["name"] == "Install deployment authorized keys"
        )["ansible.builtin.copy"]

        self.assertEqual(
            deploy_keys["content"],
            "{{ users_deploy_ssh_keys | join('\n') }}\n",
        )
        self.assertEqual(deploy_keys["mode"], "0600")

    def test_docker_repository_refreshes_missing_apt_metadata(self) -> None:
        tasks = load_yaml(ANSIBLE_ROOT / "roles" / "docker" / "tasks" / "main.yml")
        probe = next(
            task
            for task in tasks
            if task["name"] == "Probe Docker repository package metadata"
        )
        refresh = next(
            task
            for task in tasks
            if task["name"] == "Update APT cache after adding Docker repository"
        )

        self.assertEqual(
            probe["ansible.builtin.command"]["argv"],
            ["/usr/bin/apt-cache", "show", "containerd.io"],
        )
        self.assertIs(probe["changed_when"], False)
        self.assertIs(probe["failed_when"], False)
        self.assertIs(refresh["ansible.builtin.apt"]["update_cache"], True)
        self.assertIn("docker_repository_metadata.rc != 0", refresh["when"][0])

    def test_vps_inventories_separate_bootstrap_and_tailnet_access(
        self,
    ) -> None:
        inventory_root = CONSUMER_INFRA_ROOT / "inventory"

        bootstrap = load_yaml(inventory_root / "bootstrap.example.yml")
        tailnet = load_yaml(inventory_root / "hosts.example.yml")

        bootstrap_hosts = bootstrap["all"]["children"]["platform_hosts"]["hosts"]
        tailnet_hosts = tailnet["all"]["children"]["platform_hosts"]["hosts"]

        self.assertEqual(
            set(bootstrap_hosts),
            {"example-staging"},
        )
        self.assertEqual(
            set(tailnet_hosts),
            {"example-staging"},
        )

        bootstrap_host = bootstrap_hosts["example-staging"]
        tailnet_host = tailnet_hosts["example-staging"]

        self.assertEqual(bootstrap_host["ansible_user"], "ubuntu")
        self.assertEqual(
            bootstrap_host["ansible_host"],
            "203.0.113.10",
        )

        self.assertEqual(tailnet_host["ansible_user"], "ops")
        self.assertEqual(
            tailnet_host["ansible_host"],
            "platform-staging.example.invalid",
        )

        self.assertEqual(
            bootstrap_host["platform_environment"],
            tailnet_host["platform_environment"],
        )
        self.assertEqual(
            bootstrap_host["platform_public_interface"],
            tailnet_host["platform_public_interface"],
        )

    def test_vps_group_vars_are_fail_safe(self) -> None:
        group_vars_root = CONSUMER_INFRA_ROOT / "inventory" / "group_vars"

        platform_vars = load_yaml(group_vars_root / "platform_hosts.example.yml")
        local_secrets = load_yaml(group_vars_root / "all" / "local-secrets.example.yml")

        self.assertIs(platform_vars["access_guard_enabled"], True)
        self.assertEqual(platform_vars["users_ops_ssh_keys"], [])
        self.assertEqual(platform_vars["users_deploy_ssh_keys"], [])
        self.assertIs(platform_vars["proxy_acme_enabled"], True)
        self.assertTrue(
            platform_vars["proxy_acme_ca_uri"].startswith("https://acme-staging-")
        )
        self.assertTrue(platform_vars["proxy_acme_email"].endswith(".invalid"))

        self.assertTrue(Path(local_secrets["secrets_age_key_source"]).is_absolute())


if __name__ == "__main__":
    unittest.main()
