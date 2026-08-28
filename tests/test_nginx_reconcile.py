import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


from platform_automation.nginx_reconcile import certificate_fingerprint, main, reconcile
from platform_automation.nginx_transaction import (
    NginxTransactionError,
    NginxTransactionManager,
    build_fragment_plan,
    nginx_transaction_lock,
)


class NginxReconcileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, mode in (
            ("vhosts", 0o755),
            ("ownership", 0o700),
            ("locks", 0o700),
            ("certs", 0o755),
        ):
            (self.root / name).mkdir(mode=mode)
            (self.root / name).chmod(mode)
        self.generated = "server_name app.example.invalid; # backend 172.20.0.2\n"
        self.calls = []
        self.fail_test = False
        self.manager = NginxTransactionManager(
            vhost_root=self.root / "vhosts",
            ownership_root=self.root / "ownership",
            default_config=self.root / "default.conf",
            lock_root=self.root / "locks",
            raw_allowlist=self.root / "allowlist.json",
            docker_executable=Path("docker"),
            nginx_container="platform-nginx",
            runner=self.run_command,
        )

    def run_command(self, command, **kwargs):
        self.calls.append(command)
        self.assertEqual(kwargs["timeout"], 30)
        return subprocess.CompletedProcess(
            command,
            1 if self.fail_test and command[-1] == "-t" else 0,
            stdout=self.generated if "platform-docker-gen" in command else "",
            stderr="invalid configuration" if self.fail_test else "",
        )

    def run_reconcile(self):
        return reconcile(self.manager, self.root / "certs")

    def test_bootstrap_then_noop_without_unnecessary_reload(self):
        self.assertTrue(self.run_reconcile())
        self.assertEqual(self.manager.default_config.read_text(), self.generated)
        self.calls.clear()
        self.assertFalse(self.run_reconcile())
        self.assertEqual(len(self.calls), 1)  # fresh generation, but no reload

    def test_container_ip_change_regenerates_without_deploy(self):
        self.run_reconcile()
        self.generated = "server_name app.example.invalid; # backend 172.20.0.7\n"
        self.assertTrue(self.run_reconcile())
        self.assertEqual(self.manager.default_config.read_text(), self.generated)

    def test_certificate_only_renewal_reloads_without_config_change(self):
        cert = self.root / "certs/example.crt"
        cert.write_text("test fixture, not a real certificate")
        self.run_reconcile()
        cert.write_text("renewed test fixture, not a real certificate")
        self.assertTrue(self.run_reconcile())
        self.assertFalse(self.run_reconcile())

    def test_symlinked_certificate_target_is_monitored(self):
        cert = self.root / "certs/fullchain.pem"
        cert.write_text("test fixture")
        (self.root / "certs/example.crt").symlink_to("fullchain.pem")
        before = certificate_fingerprint(self.root / "certs")
        cert.write_text("renewed fixture")
        self.assertNotEqual(before, certificate_fingerprint(self.root / "certs"))

    def test_certificate_symlink_cannot_escape_root(self):
        (self.root / "certs/escape").symlink_to(self.root / "outside")
        with self.assertRaisesRegex(NginxTransactionError, "escapes"):
            self.run_reconcile()

    def test_busy_deploy_skips_generation_and_reload(self):
        with nginx_transaction_lock(self.manager.lock_root):
            self.assertFalse(self.run_reconcile())
        self.assertEqual(self.calls, [])

    def test_interrupted_deploy_is_not_activated_by_timer(self):
        plan = build_fragment_plan(
            "example", "lab", "a" * 32, {"app.example.invalid": "gzip off;\n"}
        )
        with self.manager.prepare(plan) as transaction:
            transaction.stage()
        with self.assertRaisesRegex(NginxTransactionError, "operator review"):
            self.run_reconcile()
        self.assertEqual(self.calls, [])

    def test_invalid_config_restores_disk_without_reload(self):
        self.run_reconcile()
        previous = self.manager.default_config.read_bytes()
        self.generated = "invalid nginx config;\n"
        self.fail_test = True
        self.calls.clear()
        with self.assertRaises(NginxTransactionError):
            self.run_reconcile()
        self.assertEqual(self.manager.default_config.read_bytes(), previous)
        self.assertFalse(any("reload" in call for call in self.calls))

    def test_certificate_change_during_render_is_retried_next_tick(self):
        with patch(
            "platform_automation.nginx_reconcile.certificate_fingerprint",
            side_effect=["old", "new"],
        ):
            with self.assertRaisesRegex(NginxTransactionError, "certificates changed"):
                self.run_reconcile()
        self.assertFalse(self.manager.default_config.exists())
        self.assertFalse(any("reload" in call for call in self.calls))

    def test_entrypoint_requires_root_and_no_arguments(self):
        with (
            patch("platform_automation.nginx_reconcile.os.geteuid", return_value=1000),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main([]), 2)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--arbitrary-path"]), 2)


if __name__ == "__main__":
    unittest.main()
