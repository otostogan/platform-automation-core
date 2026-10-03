import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from platform_automation.operator import bootstrap
from platform_automation.operator.console import bootstrap_action
from platform_automation.operator.context import Host

NAME = "platform-host-2"
TAILNET = "platform-host-2.tailnet.example.net"
PUBLIC = "203.0.113.11"
RECAP = f"PLAY RECAP ****\n{NAME} : ok=5 changed=CHANGED unreachable=0 failed=FAILED\n"


def recap(changed=1, failed=0) -> str:
    return RECAP.replace("CHANGED", str(changed)).replace("FAILED", str(failed))


class Prompts:
    def __init__(self, answers):
        self.answers = list(answers)

    def confirm(self, message, **_):
        value = self.answers.pop(0)
        return SimpleNamespace(ask=lambda: value)


class World:
    """A host that is, or is not, on the tailnet yet, and playbooks that pass or fail."""

    def __init__(self, on_tailnet=False, root_ssh=True, joins=True, failing=()):
        self.on_tailnet = on_tailnet
        self.root_ssh = root_ssh
        self.joins = joins
        self.failing = set(failing)
        self.ssh = []
        self.plays = []

    def runner(self, command, **_):
        target = next(part for part in command if "@" in part)
        self.ssh.append(target)
        if target.startswith("root@"):
            code, out = (0, "") if self.root_ssh else (255, "")
            return subprocess.CompletedProcess(command, code, out.encode(), b"denied")
        if self.on_tailnet:
            return subprocess.CompletedProcess(
                command,
                0,
                b"uid=1000(ops)\nhost\n198.51.100.4 50000 192.0.2.7 22\n192.0.2.7\n",
                b"",
            )
        return subprocess.CompletedProcess(command, 255, b"", b"timed out")

    def play(self, root, command):
        name = command[1].rsplit(".", 1)[1]
        inventory = command[command.index("--inventory") + 1]
        self.plays.append((name, inventory))
        assert command[command.index("--limit") + 1] == NAME
        if name in self.failing:
            return 2, recap(failed=1)
        if name == "bootstrap" and self.joins:
            self.on_tailnet = True
        second = [n for n, _ in self.plays].count("converge") == 2
        return 0, recap(changed=0 if second or name != "converge" else 3)


class BootstrapFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "infra"
        self.key = self.base / "keys/tailscale-auth.key"
        self.write(
            "inventory/bootstrap.yml",
            "all:\n  children:\n    platform_hosts:\n      hosts:\n"
            f"        {NAME}:\n          ansible_host: {PUBLIC}\n"
            "          ansible_user: root\n"
            f"          ansible_ssh_private_key_file: {self.base}/ssh/ops\n",
        )
        self.write(
            "inventory/group_vars/all/local-secrets.yml",
            f"tailscale_auth_key_source: {self.key}\n",
        )
        self.key.parent.mkdir(parents=True)
        self.key.write_text("tskey-auth-fixture\n")
        self.context = SimpleNamespace(root=self.root, core_pin="v0.0.0")
        self.host = Host(NAME, TAILNET, "ops")

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_action(self, world, answers):
        prompts = Prompts(answers)
        action = bootstrap_action(
            self.context,
            self.host,
            (prompts, None),
            runner=world.runner,
            play=world.play,
            sleeper=lambda _: None,
        )
        output = io.StringIO()
        with redirect_stdout(output):
            code = action.run()
        self.assertEqual(prompts.answers, [], "a scripted answer was never asked for")
        return code, output.getvalue()

    def test_a_new_host_goes_from_public_ssh_to_readiness_in_order(self) -> None:
        world = World()
        code, output = self.run_action(world, [True])

        self.assertEqual(code, 0)
        self.assertEqual(
            world.plays,
            [
                ("bootstrap", "inventory/bootstrap.yml"),
                ("preflight", "inventory/hosts.yml"),
                ("converge", "inventory/hosts.yml"),
                ("converge", "inventory/hosts.yml"),
                ("readiness", "inventory/hosts.yml"),
            ],
        )
        # tailnet first (not there yet), root over the public address, tailnet again
        self.assertEqual(
            world.ssh, [f"ops@{TAILNET}", f"root@{PUBLIC}", f"ops@{TAILNET}"]
        )
        self.assertIn("Keep the provider's console open", output)
        self.assertIn("reboot acceptance", output)

    def test_declining_runs_nothing(self) -> None:
        world = World()
        code, _ = self.run_action(world, [False])

        self.assertEqual(code, 130)
        self.assertEqual(world.plays, [])

    def test_a_host_that_never_joins_the_tailnet_is_not_converged(self) -> None:
        world = World(joins=False)
        code, output = self.run_action(world, [True])

        self.assertEqual(code, 1)
        self.assertEqual([name for name, _ in world.plays], ["bootstrap"])
        self.assertIn("convergence would close the public SSH", output)

    def test_a_failed_bootstrap_stops_before_the_handover(self) -> None:
        world = World(failing={"bootstrap"})
        code, _ = self.run_action(world, [True])

        self.assertEqual(code, 1)
        self.assertEqual([name for name, _ in world.plays], ["bootstrap"])
        self.assertEqual(world.ssh.count(f"ops@{TAILNET}"), 1)

    def test_a_failed_preflight_stops_before_convergence(self) -> None:
        world = World(failing={"preflight"})
        code, _ = self.run_action(world, [True])

        self.assertEqual(code, 1)
        self.assertNotIn("converge", [name for name, _ in world.plays])

    def test_root_ssh_that_refuses_the_key_stops_before_any_question(self) -> None:
        world = World(root_ssh=False)
        code, output = self.run_action(world, [])

        self.assertEqual(code, 1)
        self.assertEqual(world.plays, [])
        self.assertIn("install the ops public key", output)

    def test_a_missing_tailnet_key_stops_before_touching_the_host(self) -> None:
        self.key.unlink()
        world = World()
        code, output = self.run_action(world, [])

        self.assertEqual(code, 1)
        self.assertEqual(world.plays, [])
        self.assertNotIn(f"root@{PUBLIC}", world.ssh)
        self.assertIn("tailnet auth key is not at", output)

    def test_a_symlinked_tailnet_key_stops_before_touching_the_host(self) -> None:
        real = self.key.with_name("real.key")
        self.key.rename(real)
        self.key.symlink_to(real)
        world = World()
        code, output = self.run_action(world, [])

        self.assertEqual(code, 1)
        self.assertEqual(world.plays, [])
        self.assertNotIn(f"root@{PUBLIC}", world.ssh)
        self.assertIn("symbolic link", output)

    def test_the_action_waits_for_this_workstation_to_be_on_the_tailnet(self) -> None:
        action = bootstrap_action(self.context, self.host, (Prompts([]), None))
        self.assertTrue(action.remote)

    def test_without_the_key_setting_the_console_does_not_start(self) -> None:
        self.write("inventory/group_vars/all/local-secrets.yml", "other: 1\n")
        world = World()
        code, output = self.run_action(world, [])

        self.assertEqual(code, 1)
        self.assertEqual(world.plays, [])
        self.assertIn("tailscale up on the host by hand", output)

    def test_a_host_already_on_the_tailnet_skips_bootstrap(self) -> None:
        world = World(on_tailnet=True)
        code, output = self.run_action(world, [True])

        self.assertEqual(code, 0)
        self.assertEqual(
            [name for name, _ in world.plays],
            ["preflight", "converge", "converge", "readiness"],
        )
        self.assertNotIn(f"root@{PUBLIC}", world.ssh)
        self.assertIn("bootstrap is behind it", output)

    def test_a_host_missing_from_the_bootstrap_inventory_is_refused(self) -> None:
        self.write("inventory/bootstrap.yml", "all: {}\n")
        world = World()
        code, output = self.run_action(world, [])

        self.assertEqual(code, 1)
        self.assertEqual(world.ssh, [])
        self.assertIn("platform new host", output)


class HandoverTest(unittest.TestCase):
    def test_ssh_that_works_without_a_tailnet_address_is_not_a_handover(self) -> None:
        def runner(command, **_):
            return subprocess.CompletedProcess(
                command, 0, b"uid=1000(ops)\nhost\n", b""
            )

        ok, reason = bootstrap.wait_for_handover(
            ["ssh", "ops@x", "true"], runner, lambda _: None, attempts=2
        )
        self.assertFalse(ok)
        self.assertIn("no address", reason)

    def test_a_session_that_arrived_elsewhere_is_not_a_handover(self) -> None:
        # the host has a tailnet address, but this SSH came in by another one
        answer = "uid=1000(ops)\nhost\n198.51.100.4 50000 203.0.113.11 22\n192.0.2.7\n"
        self.assertIn("not a tailnet address", bootstrap.handover_problem(answer))
        self.assertIsNone(
            bootstrap.handover_problem(
                "uid=1000(ops)\nhost\n198.51.100.4 50000 192.0.2.7 22\n192.0.2.7\n"
            )
        )
        self.assertIn(
            "did not report where it arrived",
            bootstrap.handover_problem("uid=1000(ops)\nhost\n192.0.2.7\n"),
        )

    def test_the_probe_reports_where_the_session_arrived(self) -> None:
        self.assertIn("printenv SSH_CONNECTION", bootstrap.HANDOVER)
        self.assertNotIn("$", bootstrap.HANDOVER)

    def test_the_tailnet_is_asked_again_while_it_learns_the_machine(self) -> None:
        answers = iter([255, 255, 0])
        slept = []

        def runner(command, **_):
            code = next(answers)
            return subprocess.CompletedProcess(
                command,
                code,
                (
                    b"2001:db8::1 50000 2001:db8::9 22\n2001:db8::9\n"
                    if code == 0
                    else b""
                ),
                b"no route",
            )

        ok, _ = bootstrap.wait_for_handover(
            ["ssh", "ops@x", "true"], runner, slept.append, attempts=5, pause=10
        )
        self.assertTrue(ok)
        self.assertEqual(slept, [10, 10])


if __name__ == "__main__":
    unittest.main()
