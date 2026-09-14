import subprocess
import unittest
from pathlib import Path

from platform_automation.operator.core_update import (
    CoreUpdateError,
    artifact_url,
    host_version,
    install_collection,
    parse_recap,
    parse_runtime_version,
    playbook_command,
    rewrite_pin,
    run_playbook,
    verdict,
)

REQUIREMENTS = """---
collections:
    - name: community.general
      version: "8.6.0"

    - name: >-
          https://github.com/otostogan/platform-automation-core/releases/download/v0.16.0/otostogan-platform-0.16.0.tar.gz
      type: url
"""

RECAP = """
PLAY RECAP *********************************************************************
platform-host-1            : ok=116  changed=1    unreachable=0    failed=0    skipped=15   rescued=0    ignored=0
platform-host-2            : ok=0    changed=0    unreachable=1    failed=0    skipped=0    rescued=0    ignored=0
"""


def completed(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout.encode(), stderr.encode())


class PinTest(unittest.TestCase):
    def test_both_halves_of_the_url_move_together(self) -> None:
        text = rewrite_pin(REQUIREMENTS, "v0.16.1")
        self.assertIn("download/v0.16.1/otostogan-platform-0.16.1.tar.gz", text)
        self.assertNotIn("0.16.0", text)
        self.assertIn('version: "8.6.0"', text, "community.general is untouched")

    def test_a_file_without_exactly_one_pin_is_refused(self) -> None:
        with self.assertRaises(CoreUpdateError):
            rewrite_pin("collections: []\n", "v0.16.1")
        with self.assertRaises(CoreUpdateError):
            rewrite_pin(REQUIREMENTS, "0.16.1")

    def test_artifact_url(self) -> None:
        self.assertEqual(
            artifact_url("v0.16.1"),
            "https://github.com/otostogan/platform-automation-core/releases/download/v0.16.1/otostogan-platform-0.16.1.tar.gz",
        )


class HostVersionTest(unittest.TestCase):
    def test_version_is_read_from_the_runtime_file_over_ssh(self) -> None:
        calls = []

        def runner(command, **_):
            calls.append(command)
            return completed('__version__ = "0.16.1"\n')

        found = host_version(
            "platform-host-1.tailnet.example.net", "ops", Path("/k/ops"), runner
        )
        self.assertEqual(found.version, "v0.16.1")
        self.assertIn("-i", calls[0])
        self.assertTrue(any(a.startswith("ops@") for a in calls[0]))
        self.assertIsNone(parse_runtime_version("nothing here"))

    def test_an_unreachable_host_is_reported_not_raised(self) -> None:
        runner = lambda command, **_: completed("", 255, "ssh: connect: no route\n")
        found = host_version("platform-host-1", "ops", None, runner)
        self.assertIsNone(found.version)
        self.assertIn("no route", found.error)


class RecapTest(unittest.TestCase):
    def test_recap_lines_are_parsed_per_host(self) -> None:
        recap = parse_recap(RECAP)
        self.assertEqual(recap["platform-host-1"]["changed"], 1)
        self.assertEqual(recap["platform-host-2"]["unreachable"], 1)
        self.assertEqual(parse_recap("no recap at all"), {})

    def test_verdict_names_the_problem_per_host(self) -> None:
        recap = parse_recap(RECAP)
        self.assertEqual(verdict(recap, ["platform-host-1"], second=False), [])
        self.assertIn(
            "not idempotent", verdict(recap, ["platform-host-1"], second=True)[0]
        )
        self.assertIn(
            "unreachable", verdict(recap, ["platform-host-2"], second=False)[0]
        )
        self.assertIn(
            "no recap line", verdict(recap, ["platform-host-3"], second=False)[0]
        )

    def test_playbook_command_limits_to_the_chosen_hosts(self) -> None:
        command = playbook_command(Path("/infra"), "converge", ["a", "b"])
        self.assertEqual(command[1], "otostogan.platform.converge")
        self.assertEqual(command[-2:], ["--limit", "a,b"])


class InstallTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / ".venv/bin").mkdir(parents=True)
        (self.root / ".venv/bin/ansible-galaxy").write_text("#!/bin/sh\n")
        (self.root / ".venv/bin/ansible-playbook").write_text("#!/bin/sh\n")

    def test_galaxy_timeout_falls_back_to_the_artifact_url(self) -> None:
        calls = []

        def runner(command, **_):
            calls.append(command)
            if command[-1].endswith("requirements.yml"):
                return completed(
                    "ERROR! Failed to download collection tar: The read operation timed out\n",
                    1,
                )
            return completed("otostogan.platform:0.16.1 was installed successfully\n")

        used = install_collection(self.root, "v0.16.1", runner)
        self.assertEqual(len(calls), 2)
        self.assertIn("download/v0.16.1/", used)

    def test_both_attempts_failing_names_the_last_error(self) -> None:
        runner = lambda command, **_: completed("ERROR! nope\n", 1)
        with self.assertRaisesRegex(CoreUpdateError, "nope"):
            install_collection(self.root, "v0.16.1", runner)

    def test_missing_venv_is_named(self) -> None:
        (self.root / ".venv/bin/ansible-galaxy").unlink()
        with self.assertRaisesRegex(CoreUpdateError, "venv"):
            install_collection(self.root, "v0.16.1", lambda command, **_: completed())

    def test_playbook_output_is_streamed_and_kept(self) -> None:
        class Process:
            stdout = iter(
                [
                    "TASK [x]\n",
                    "PLAY RECAP\n",
                    "h : ok=1 changed=0 unreachable=0 failed=0\n",
                ]
            )

            def wait(self):
                return 0

        echoed = []
        code, output = run_playbook(
            self.root,
            playbook_command(self.root, "converge", ["h"]),
            spawn=lambda *a, **k: Process(),
            echo=echoed.append,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(echoed), 3)
        self.assertEqual(parse_recap(output)["h"]["ok"], 1)
