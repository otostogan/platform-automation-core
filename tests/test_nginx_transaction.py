import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from platform_automation.nginx_transaction import (  # noqa: E402
    NginxTransactionBusyError,
    NginxTransactionError,
    NginxTransactionManager,
    build_fragment_plan,
    load_raw_project_allowlist,
    nginx_transaction_lock,
)


class NginxTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.vhosts = self.base / "vhost.d"
        self.ownership = self.base / "managed-vhosts"
        self.locks = self.base / "locks"
        self.config = self.base / "default.conf"
        self.allowlist = self.base / "allowlist.json"
        self.vhosts.mkdir(mode=0o755)
        self.ownership.mkdir(mode=0o700)
        self.locks.mkdir(mode=0o700)
        self.config.write_text("server_name app.example.test;\n", encoding="utf-8")
        self.config.chmod(0o644)
        self.allowlist.write_text("[]\n", encoding="utf-8")
        self.allowlist.chmod(0o600)
        self.commands: list[list[str]] = []
        self.fail_reload = False
        self.fail_test = False
        self.generated = "server_name app.example.test;\n"
        self.manager = NginxTransactionManager(
            vhost_root=self.vhosts,
            ownership_root=self.ownership,
            default_config=self.config,
            lock_root=self.locks,
            raw_allowlist=self.allowlist,
            docker_executable=Path("/usr/bin/docker"),
            nginx_container="platform-nginx",
            runner=self.run_command,
            sleeper=lambda _: None,
            convergence_timeout=-1,
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_command(self, command, **kwargs):
        self.commands.append(command)
        failed = (self.fail_reload and command[-3:] == ["nginx", "-s", "reload"]) or (
            self.fail_test and command[-2:] == ["nginx", "-t"]
        )
        return subprocess.CompletedProcess(
            command,
            1 if failed else 0,
            stdout=self.generated if "platform-docker-gen" in command else "",
            stderr="reload failed" if failed else "",
        )

    def plan(self, fragments=None, release_id="a" * 32):
        return build_fragment_plan(
            "example",
            "lab",
            release_id,
            fragments or {"app.example.test": "# managed\n"},
        )

    def test_plan_is_immutable_and_validates_names(self) -> None:
        source = {"app.example.test": "first\n"}
        plan = self.plan(source)
        source["app.example.test"] = "changed\n"
        self.assertEqual(plan.fragments["app.example.test"], "first\n")
        with self.assertRaisesRegex(NginxTransactionError, "invalid nginx"):
            self.plan({"../escape": "bad\n"})
        with self.assertRaisesRegex(NginxTransactionError, "too large"):
            self.plan({"app.example.test": "x" * (64 * 1024 + 1)})

    def test_stage_and_activate_records_ownership(self) -> None:
        plan = self.plan()
        with self.manager.prepare(plan) as transaction:
            transaction.stage()
            self.assertFalse((self.vhosts / "app.example.test").exists())
            self.assertEqual(self.commands, [])
            transaction.activate()

        self.assertEqual((self.vhosts / "app.example.test").read_text(), "# managed\n")
        self.assertFalse(self.manager.pending_path.exists())

        metadata = json.loads(
            (self.ownership / "example--lab.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["release_id"], "a" * 32)
        self.assertEqual(metadata["fragments"], ["app.example.test"])
        self.assertEqual(
            self.commands,
            [
                [
                    "/usr/bin/docker",
                    "exec",
                    "platform-docker-gen",
                    "docker-gen",
                    "-endpoint",
                    "tcp://docker-socket-read:2375",
                    "/app/nginx.tmpl",
                ],
                ["/usr/bin/docker", "exec", "platform-nginx", "nginx", "-t"],
                [
                    "/usr/bin/docker",
                    "exec",
                    "platform-nginx",
                    "nginx",
                    "-s",
                    "reload",
                ],
            ],
        )

    def test_rollback_restores_previous_files_and_metadata(self) -> None:
        old_plan = self.plan({"old.example.test": "old\n"}, "1" * 32)
        self.generated = "server_name old.example.test;\n"
        with self.manager.prepare(old_plan) as transaction:
            transaction.stage()
            transaction.activate()

        new_plan = self.plan({"app.example.test": "new\n"}, "2" * 32)
        self.generated = "server_name app.example.test;\n"
        with self.manager.prepare(new_plan) as transaction:
            transaction.stage()
            transaction.activate()
            self.generated = "server_name old.example.test;\n"
            transaction.rollback()

        self.assertEqual((self.vhosts / "old.example.test").read_text(), "old\n")
        self.assertFalse((self.vhosts / "app.example.test").exists())
        metadata = json.loads((self.ownership / "example--lab.json").read_text())
        self.assertEqual(metadata["release_id"], "1" * 32)

    def test_rejects_unmanaged_and_cross_scope_collisions(self) -> None:
        unmanaged = self.vhosts / "app.example.test"
        unmanaged.write_text("manual\n", encoding="utf-8")
        unmanaged.chmod(0o644)
        with self.assertRaisesRegex(NginxTransactionError, "unmanaged"):
            with self.manager.prepare(self.plan()):
                pass

        unmanaged.unlink()
        metadata = {
            "api_version": "platform.nginx-ownership/v1",
            "environment": "production",
            "fragments": ["app.example.test"],
            "project": "other",
            "release_id": "3" * 32,
        }
        path = self.ownership / "other--production.json"
        path.write_text(json.dumps(metadata), encoding="utf-8")
        path.chmod(0o600)
        managed = self.vhosts / "app.example.test"
        managed.write_text("other\n", encoding="utf-8")
        managed.chmod(0o644)
        with self.assertRaisesRegex(NginxTransactionError, "owned by other"):
            with self.manager.prepare(self.plan()):
                pass

    def test_reload_failure_does_not_commit_metadata(self) -> None:
        self.fail_reload = True
        with self.manager.prepare(self.plan()) as transaction:
            transaction.stage()
            with self.assertRaisesRegex(NginxTransactionError, "reload failed"):
                transaction.activate()
            self.assertFalse((self.ownership / "example--lab.json").exists())
            self.fail_reload = False
            self.generated = "server_name _;\n"
            transaction.rollback()
        self.assertFalse((self.vhosts / "app.example.test").exists())

    def test_wait_requires_generated_host_convergence(self) -> None:
        self.generated = "server_name xapp.example.test;\n"
        with self.manager.prepare(self.plan()) as transaction:
            transaction.stage()
            with self.assertRaisesRegex(NginxTransactionError, "did not converge"):
                transaction.activate()

    def test_wait_ignores_commented_server_name(self) -> None:
        self.generated = "# server_name app.example.test;\n"
        with self.manager.prepare(self.plan()) as transaction:
            transaction.stage()
            with self.assertRaisesRegex(NginxTransactionError, "did not converge"):
                transaction.activate()

    def test_same_host_uses_fresh_upstream_not_old_config(self):
        self.config.write_text("server_name app.example.test; # old backend\n")
        self.generated = "server_name app.example.test; # NEW backend 172.20.0.3\n"
        with self.manager.prepare(self.plan()) as transaction:
            transaction.stage()
            self.assertIn("old backend", self.config.read_text())
            transaction.activate()
        self.assertEqual(self.config.read_text(), self.generated)

    def test_old_matching_host_does_not_hide_generation_failure(self):
        old = self.config.read_bytes()
        with self.manager.prepare(self.plan()) as transaction:
            transaction.stage()
            with patch.object(
                self.manager,
                "runner",
                side_effect=subprocess.TimeoutExpired("docker-gen", 30),
            ):
                with self.assertRaises(NginxTransactionError):
                    transaction.activate()
        self.assertEqual(self.config.read_bytes(), old)
        self.assertTrue(self.manager.pending_path.exists())
        self.assertFalse(any("reload" in command for command in self.commands))

    def test_invalid_config_never_reloads_and_rollback_regenerates(self):
        self.fail_test = True
        with self.manager.prepare(self.plan()) as transaction:
            transaction.stage()
            with self.assertRaises(NginxTransactionError):
                transaction.activate()
            self.assertFalse(any("reload" in command for command in self.commands))
            self.fail_test = False
            self.generated = "server_name _; # previous containers freshly inspected\n"
            transaction.rollback()
        self.assertEqual(self.config.read_text(), self.generated)
        self.assertFalse(self.manager.pending_path.exists())

    def test_interrupted_transaction_blocks_future_activation(self):
        with self.manager.prepare(self.plan()) as transaction:
            transaction.stage()
        with self.assertRaisesRegex(NginxTransactionError, "operator review"):
            with self.manager.prepare(self.plan()):
                pass

    def test_rollback_failure_keeps_pending_marker(self):
        with self.manager.prepare(self.plan()) as transaction:
            transaction.stage()
            self.generated = "server_name app.example.test;\n"
            with self.assertRaises(NginxTransactionError):
                transaction.rollback()
        self.assertTrue(self.manager.pending_path.exists())

    def test_empty_generation_is_rejected(self):
        self.generated = ""
        with self.assertRaisesRegex(NginxTransactionError, "empty"):
            self.manager.render_config()

    def test_global_lock_rejects_concurrent_transaction(self) -> None:
        with nginx_transaction_lock(self.locks):
            with self.assertRaises(NginxTransactionBusyError):
                with nginx_transaction_lock(self.locks):
                    pass

    def test_rejects_symlinked_lock_root(self) -> None:
        real = self.base / "real-locks"
        real.mkdir(mode=0o700)
        linked = self.base / "linked-locks"
        linked.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(NginxTransactionError, "not a directory"):
            with nginx_transaction_lock(linked):
                pass

    def test_rejects_missing_owned_fragment(self) -> None:
        metadata = {
            "api_version": "platform.nginx-ownership/v1",
            "environment": "production",
            "fragments": ["missing.example.test"],
            "project": "other",
            "release_id": "3" * 32,
        }
        path = self.ownership / "other--production.json"
        path.write_text(json.dumps(metadata), encoding="utf-8")
        path.chmod(0o600)
        with self.assertRaisesRegex(NginxTransactionError, "is missing"):
            with self.manager.prepare(self.plan()):
                pass

    def test_raw_allowlist_is_root_only_sorted_and_unique(self) -> None:
        self.allowlist.write_text('["example"]\n', encoding="utf-8")
        self.assertEqual(load_raw_project_allowlist(self.allowlist), {"example"})
        self.allowlist.chmod(0o644)
        with self.assertRaisesRegex(NginxTransactionError, "unexpected mode"):
            load_raw_project_allowlist(self.allowlist)
        self.allowlist.chmod(0o600)
        self.allowlist.write_text('["zeta", "alpha"]\n', encoding="utf-8")
        with self.assertRaisesRegex(NginxTransactionError, "sorted and unique"):
            load_raw_project_allowlist(self.allowlist)


if __name__ == "__main__":
    unittest.main()
