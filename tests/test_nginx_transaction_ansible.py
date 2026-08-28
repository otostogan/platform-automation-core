import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class NginxTransactionAnsibleTest(unittest.TestCase):
    def test_watch_process_cannot_write_live_config_or_reload_nginx(self):
        compose = yaml.safe_load(
            (ROOT / "roles/proxy/files/bundle/compose.yml").read_text()
        )
        generator = compose["services"]["docker-gen"]
        self.assertNotIn("pid", generator)
        self.assertNotIn("-notify", generator["command"])
        self.assertEqual(generator["command"][-1], "/tmp/docker-gen-preview.conf")
        self.assertIn("/etc/nginx/conf.d:ro", " ".join(generator["volumes"]))
        acme_policy = (
            ROOT / "roles/proxy/files/bundle/socket-proxy/acme.cfg"
        ).read_text()
        self.assertIn("http-request return status 204 if reload_method", acme_policy)
        self.assertNotIn("http-request allow if reload_method", acme_policy)

    def test_reconciler_is_installed_before_proxy_start_and_has_timer(self):
        roles = yaml.safe_load((ROOT / "playbooks/converge.yml").read_text())[0][
            "roles"
        ]
        names = [role["role"] for role in roles]
        self.assertLess(
            names.index("otostogan.platform.platform_cli"),
            names.index("otostogan.platform.proxy"),
        )
        defaults = yaml.safe_load(
            (ROOT / "roles/platform_cli/defaults/main.yml").read_text()
        )
        self.assertIn("nginx_reconcile.py", defaults["platform_cli_tool_files"])
        templates = ROOT / "roles/proxy/templates"
        proxy = (templates / "platform-proxy.service.j2").read_text()
        self.assertIn(
            "ExecStartPost=/usr/bin/python3 -m platform_automation.nginx_reconcile",
            proxy,
        )
        service = (templates / "platform-nginx-reconcile.service.j2").read_text()
        self.assertIn("User=root", service)
        self.assertIn("TimeoutStartSec=120", service)
        timer = (templates / "platform-nginx-reconcile.timer.j2").read_text()
        self.assertIn("OnUnitInactiveSec=10s", timer)
        tasks = yaml.safe_load((ROOT / "roles/proxy/tasks/configure.yml").read_text())
        timer_task = next(
            task
            for task in tasks
            if task["name"] == "Enable Nginx reconciliation timer"
        )
        self.assertTrue(timer_task["ansible.builtin.systemd_service"]["enabled"])

    def test_platform_cli_installs_transaction_module_and_private_allowlist(self):
        role = ROOT / "roles" / "platform_cli"
        defaults = yaml.safe_load(
            (role / "defaults" / "main.yml").read_text(encoding="utf-8")
        )
        tasks = yaml.safe_load(
            (role / "tasks" / "main.yml").read_text(encoding="utf-8")
        )

        self.assertIn("nginx_transaction.py", defaults["platform_cli_tool_files"])
        allowlist = next(
            task
            for task in tasks
            if task["name"] == "Install raw Nginx project allowlist"
        )["ansible.builtin.copy"]
        self.assertEqual(allowlist["owner"], "root")
        self.assertEqual(allowlist["group"], "root")
        self.assertEqual(allowlist["mode"], "0600")

        rendered_tasks = yaml.safe_dump(tasks)
        self.assertIn("role_path", rendered_tasks)
        self.assertNotIn("playbook_dir", rendered_tasks)

        wrapper = (role / "templates" / "platform.j2").read_text(encoding="utf-8")
        self.assertIn("-m platform_automation.platform_cli", wrapper)

    def test_proxy_assets_are_collection_relative(self):
        defaults = yaml.safe_load(
            (ROOT / "roles/proxy/defaults/main.yml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            defaults["proxy_bundle_source"],
            "{{ role_path }}/files/bundle",
        )

    def test_proxy_creates_private_ownership_registry(self):
        tasks = yaml.safe_load(
            (ROOT / "roles" / "proxy" / "tasks" / "configure.yml").read_text(
                encoding="utf-8"
            )
        )
        ownership = next(
            task
            for task in tasks
            if task["name"] == "Create private managed Nginx ownership directory"
        )["ansible.builtin.file"]
        self.assertTrue(ownership["path"].endswith("/managed-vhosts"))
        self.assertEqual(ownership["mode"], "0700")


if __name__ == "__main__":
    unittest.main()
