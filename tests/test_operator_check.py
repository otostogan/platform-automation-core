"""Tailscale SSH under ``action: check`` holds the connection for a browser.

The console must show the link, wait without its usual patience, and go on
once Tailscale reports the check done — never a traceback.
"""

import io
from pathlib import Path
import subprocess
import threading
import unittest

from platform_automation.operator import bootstrap, core_update, logs
from platform_automation.operator.remote import (
    CHECK_TIMEOUT_SECONDS,
    CheckExpired,
    checked_run,
    run_platform,
    strip_check_lines,
)

PROMPT = b"# Tailscale SSH requires an additional check.\n"
LINK = b"# To authenticate, visit: https://login.tailscale.com/a/0123abcd\n"
DONE = b"# Authentication checked with Tailscale SSH.\n"


class Stream:
    """stderr as ssh writes it: a few lines, a pause for the operator, the rest."""

    def __init__(self, before, released, after, drained):
        self.before = list(before)
        self.after = list(after)
        self.released = released
        self.drained = drained

    def readline(self) -> bytes:
        if self.before:
            line = self.before.pop(0)
            if not self.before:
                self.drained.set()
            return line
        self.released.wait()
        if self.after:
            return self.after.pop(0)
        return b""


class FakeSsh:
    """A Popen that stops for the check until ``wait`` has been asked ``waits`` times."""

    def __init__(self, before, after, stdout=b"", returncode=0, waits=3):
        self.released = threading.Event()
        self.prompt_read = threading.Event()
        self.stdout = io.BytesIO(stdout)
        self.stderr = Stream(before, self.released, after, self.prompt_read)
        self.code = returncode
        self.waits = waits
        self.asked = 0
        self.returncode = None
        self.killed = False

    def wait(self, timeout=None):
        if self.killed:
            return self.returncode
        self.prompt_read.wait(1)
        self.asked += 1
        if self.asked < self.waits:
            raise subprocess.TimeoutExpired("ssh", timeout)
        self.released.set()
        self.returncode = self.code
        return self.code

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.released.set()
        self.returncode = -9


def fake_popen(process):
    def popen(command, **kwargs):
        popen.commands.append(command)
        return process

    popen.commands = []
    return popen


class Clock:
    """Advances by ``step`` seconds every time it is read."""

    def __init__(self, step):
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class CheckedRunTest(unittest.TestCase):
    def test_the_link_is_shown_and_the_command_completes_after_the_check(self):
        said = []
        process = FakeSsh([PROMPT, LINK], [DONE], stdout=b'{"ok": true}\n')

        result = checked_run(
            ["ssh", "ops@h", "--", "true"],
            timeout=30,
            popen=fake_popen(process),
            notify=said.append,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'{"ok": true}\n')
        self.assertEqual(result.stderr, b"")
        self.assertIn("  https://login.tailscale.com/a/0123abcd", said)
        self.assertTrue(any("checkPeriod" in line for line in said))
        self.assertEqual(said[-1], "  confirmed; continuing")
        self.assertFalse(process.killed)

    def test_the_usual_patience_does_not_count_while_the_check_is_pending(self):
        # Every read of the clock is a minute; a 30 second timeout would have
        # fired on the first poll if the pending check did not suspend it.
        process = FakeSsh([PROMPT, LINK], [DONE], stdout=b"ok\n", waits=5)

        result = checked_run(
            ["ssh"],
            timeout=30,
            popen=fake_popen(process),
            notify=lambda _: None,
            clock=Clock(60),
        )

        self.assertEqual(result.stdout, b"ok\n")
        self.assertFalse(process.killed)

    def test_an_unconfirmed_check_expires_with_a_plain_message(self):
        process = FakeSsh([PROMPT, LINK], [], waits=100)

        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            checked_run(
                ["ssh"],
                timeout=30,
                popen=fake_popen(process),
                notify=lambda _: None,
                clock=Clock(CHECK_TIMEOUT_SECONDS / 2),
            )

        self.assertIsInstance(caught.exception, CheckExpired)
        self.assertIn("not confirmed within 15 minutes", str(caught.exception))
        self.assertTrue(process.killed)

    def test_without_a_check_the_command_times_out_as_before(self):
        process = FakeSsh([b"ssh: slow\n"], [], waits=100)

        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            checked_run(
                ["ssh"],
                timeout=30,
                popen=fake_popen(process),
                notify=lambda _: None,
                clock=Clock(20),
            )

        self.assertNotIsInstance(caught.exception, CheckExpired)
        self.assertTrue(process.killed)

    def test_other_stderr_is_kept_and_the_check_lines_are_not(self):
        process = FakeSsh(
            [PROMPT, LINK], [DONE, b"status error: no such project\n"], returncode=1
        )

        result = checked_run(["ssh"], popen=fake_popen(process), notify=lambda _: None)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, b"status error: no such project")


class ConsoleTest(unittest.TestCase):
    def test_the_grafana_action_shows_the_link_instead_of_a_traceback(self):
        import contextlib
        import functools
        from types import SimpleNamespace
        from unittest import mock

        from platform_automation.operator import console
        from platform_automation.operator.context import Host

        # Grafana is not running: the inspect answers nothing after the check,
        # so the action stops before it would open a tunnel.
        process = FakeSsh([PROMPT, LINK], [DONE], stdout=b"\n")
        said = []
        runner = functools.partial(
            checked_run, popen=fake_popen(process), notify=said.append
        )
        host = Host("platform-host-1", "platform-host-1.tailnet.example.net", "ops")
        context = SimpleNamespace(root=Path("/nonexistent"), core_pin=None)
        action = console.grafana_action(context, host, (None, None))

        out = io.StringIO()
        with mock.patch.object(console, "checked_run", runner):
            with contextlib.redirect_stdout(out):
                code = action.run()

        self.assertEqual(code, 1)
        self.assertIn("  https://login.tailscale.com/a/0123abcd", said)
        self.assertIn("Grafana is not running", out.getvalue())
        self.assertFalse(process.killed)


class CallersTest(unittest.TestCase):
    """Every helper that captures ssh output goes through the checked runner."""

    def test_run_platform_ignores_the_check_lines_in_stderr(self):
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 0, b'{"projects": []}', PROMPT + LINK + DONE
            )

        result = run_platform("h", "ops", ["projects"], runner=runner)
        self.assertTrue(result.ok)

        def refusing(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 1, b"", PROMPT + LINK + DONE + b"status error: nope\n"
            )

        result = run_platform("h", "ops", ["status"], runner=refusing)
        self.assertEqual(result.error, "status error: nope")

    def test_the_helpers_default_to_the_checked_runner(self):
        import inspect

        for function in (
            run_platform,
            logs.list_containers,
            core_update.host_version,
            bootstrap.answers,
            bootstrap.wait_for_handover,
        ):
            with self.subTest(function=function.__name__):
                default = inspect.signature(function).parameters["runner"].default
                self.assertIs(default, checked_run)

    def test_strip_check_lines(self):
        text = (PROMPT + LINK + DONE + b"real error\n").decode()
        self.assertEqual(strip_check_lines(text), "real error")


if __name__ == "__main__":
    unittest.main()
