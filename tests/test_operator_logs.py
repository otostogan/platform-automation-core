import re
import subprocess
import unittest

from platform_automation.operator.logs import (
    LogsError,
    follow_container,
    follow_nginx_for,
    follow_project,
    list_containers,
    parse_containers,
    ps_remote,
    ssh_command,
)

PS = (
    "my-app-lab-web-1\tweb\tmy-app-lab\tUp 3 hours (healthy)\n"
    "my-app-lab-mailpit-1\tmailpit\tmy-app-lab\tUp 3 hours\n"
    "platform-db-my-app-lab-postgres-1\tpostgres\tplatform-db-my-app-lab\tExited (1) 2 minutes ago\n"
    "my-app-lab-web-1\tweb\tmy-app-lab\tUp 3 hours (healthy)\n"
    "garbage line\n"
)


class LogsTest(unittest.TestCase):
    def test_containers_are_parsed_once_each_with_their_state(self) -> None:
        containers = parse_containers(PS)
        self.assertEqual(
            [c.service for c in containers], ["web", "mailpit", "postgres"]
        )
        self.assertTrue(containers[0].running)
        self.assertFalse(containers[2].running)

    def test_only_ps_and_logs_ever_reach_the_host(self) -> None:
        remotes = [
            ps_remote(["my-app-lab", "platform-db-my-app-lab"]),
            ps_remote(),
            follow_container("my-app-lab-web-1"),
            follow_project("my-app-lab"),
            follow_nginx_for(["lab.my-app.example.com"]),
        ]
        for remote in remotes:
            verbs = re.findall(r"docker (?:compose --project-name \S+ )?(\w+)", remote)
            self.assertTrue(verbs and set(verbs) <= {"ps", "logs"}, remote)
            self.assertNotIn("inspect", remote)
            self.assertNotIn("exec", remote)

    def test_the_proxy_log_is_narrowed_to_the_application_hosts(self) -> None:
        remote = follow_nginx_for(["lab.my-app.example.com", "mail.my-app.example.com"])
        self.assertIn("grep --line-buffered -E", remote)
        self.assertIn("lab\\.my-app\\.example\\.com", remote)
        pattern = re.search(r"-E '(.*)'$", remote).group(1)
        self.assertRegex(
            'lab.my-app.example.com 203.0.113.9 - - [x] "GET / HTTP/2.0" 200', pattern
        )
        self.assertNotRegex(
            'other.example.com 203.0.113.9 - - [x] "GET /" 200', pattern
        )
        self.assertNotRegex("labXmy-app.example.com 203.0.113.9", pattern)
        with self.assertRaises(LogsError):
            follow_nginx_for(["bad host; rm -rf /"])

    def test_listing_goes_over_the_operators_ssh_and_names_failures(self) -> None:
        calls = []

        def runner(command, **_):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, PS.encode(), b"")

        containers = list_containers(
            "platform-host-1.tailnet.example.net", "ops", ["my-app-lab"], runner=runner
        )
        self.assertEqual(len(containers), 3)
        self.assertEqual(calls[0][0], "ssh")
        self.assertIn("-oBatchMode=yes", calls[0])

        failing = lambda command, **_: subprocess.CompletedProcess(
            command, 255, b"", b"ssh: no route\n"
        )
        with self.assertRaisesRegex(LogsError, "no route"):
            list_containers("platform-host-1", "ops", runner=failing)

    def test_following_needs_a_terminal(self) -> None:
        self.assertIn("-t", ssh_command("h", "ops", "x", tty=True))
        self.assertNotIn("-t", ssh_command("h", "ops", "x"))
