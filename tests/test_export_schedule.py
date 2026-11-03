"""Export timers: one per job, written by a deploy, removed when the job leaves."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from platform_automation.export_schedule import (
    ExportScheduleError,
    check_calendar,
    declared_jobs,
    disable_export_timers,
    existing_instances,
    export_instance,
    find_job,
    reconcile_export_timers,
    render_schedule_override,
    split_export_instance,
)


def manifest(jobs=None, mode="docker"):
    return {
        "database": {"mode": mode},
        "exports": {
            "smtp": {"host": "smtp.example.invalid", "from": "a@example.invalid"},
            "jobs": (
                jobs
                if jobs is not None
                else [
                    {
                        "name": "clients",
                        "schedule": "Mon 06:00",
                        "to": ["b@example.invalid"],
                        "query": "SELECT 1",
                    }
                ]
            ),
        },
    }


class FakeRunner:
    def __init__(self, refuse=()):
        self.commands = []
        self.refuse = set(refuse)

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        code = 1 if any(token in self.refuse for token in command) else 0
        return subprocess.CompletedProcess(command, code, b"", b"bad")


class InstanceTest(unittest.TestCase):
    def test_round_trip_keeps_hyphenated_projects_apart_from_the_name(self) -> None:
        instance = export_instance("health-client", "production", "clients_v2")
        self.assertEqual(instance, "health-client-production-clients_v2")
        self.assertEqual(
            split_export_instance(instance),
            ("health-client", "production", "clients_v2"),
        )

    def test_a_hyphen_in_the_name_is_refused(self) -> None:
        with self.assertRaises(ExportScheduleError):
            export_instance("app", "lab", "my-export")
        with self.assertRaises(ExportScheduleError):
            split_export_instance("app-lab-my-export")
        with self.assertRaises(ExportScheduleError):
            split_export_instance("app-sandbox-clients")


class DeclaredJobsTest(unittest.TestCase):
    def test_only_a_platform_database_has_jobs(self) -> None:
        self.assertEqual(len(declared_jobs(manifest())), 1)
        self.assertEqual(declared_jobs(manifest(mode="external")), [])
        self.assertEqual(declared_jobs({"database": {"mode": "docker"}}), [])

    def test_find_job_names_the_missing_one(self) -> None:
        self.assertEqual(find_job(manifest(), "clients")["schedule"], "Mon 06:00")
        with self.assertRaisesRegex(ExportScheduleError, "no export named nope"):
            find_job(manifest(), "nope")


class CalendarTest(unittest.TestCase):
    def test_systemd_is_asked_and_its_refusal_is_explained(self) -> None:
        runner = FakeRunner()
        check_calendar("Mon 06:00", Path("/usr/bin/systemd-analyze"), runner)
        self.assertEqual(
            runner.commands[0],
            ["/usr/bin/systemd-analyze", "calendar", "--iterations=1", "Mon 06:00"],
        )
        with self.assertRaisesRegex(ExportScheduleError, "does not accept"):
            check_calendar("Mon 25:00", Path("x"), FakeRunner(refuse={"Mon 25:00"}))

    def test_odd_characters_never_reach_systemd(self) -> None:
        runner = FakeRunner()
        with self.assertRaisesRegex(ExportScheduleError, "invalid schedule"):
            check_calendar("daily; rm -rf /", Path("x"), runner)
        self.assertEqual(runner.commands, [])

    def test_the_override_pins_the_calendar_and_catches_up_after_sleep(self) -> None:
        text = render_schedule_override("Mon 06:00").decode()
        self.assertIn("OnCalendar=\nOnCalendar=Mon 06:00\n", text)
        self.assertIn("Persistent=true", text)


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.systemd = Path(self.temporary.name)
        self.runner = FakeRunner()

    def reconcile(self, document):
        return reconcile_export_timers(
            document,
            "app",
            "lab",
            systemd_root=self.systemd,
            systemctl_executable=Path("systemctl"),
            systemd_analyze_executable=Path("systemd-analyze"),
            runner=self.runner,
        )

    def test_a_job_gets_a_drop_in_and_an_enabled_timer(self) -> None:
        result = self.reconcile(manifest())

        self.assertEqual(result["jobs"][0]["state"], "enabled")
        self.assertEqual(
            result["jobs"][0]["unit"], "platform-export@app-lab-clients.timer"
        )
        drop_in = (
            self.systemd / "platform-export@app-lab-clients.timer.d" / "schedule.conf"
        )
        self.assertIn("OnCalendar=Mon 06:00", drop_in.read_text())
        self.assertIn(["systemctl", "daemon-reload"], self.runner.commands)
        self.assertIn(
            ["systemctl", "enable", "--now", "platform-export@app-lab-clients.timer"],
            self.runner.commands,
        )
        self.assertEqual(existing_instances(self.systemd, "app", "lab"), ["clients"])
        self.assertEqual(result["warnings"], [])

    def test_a_job_that_left_the_manifest_takes_its_timer_with_it(self) -> None:
        self.reconcile(manifest())
        self.runner.commands.clear()

        result = self.reconcile(manifest(jobs=[]))

        self.assertEqual(result["removed"], ["platform-export@app-lab-clients.timer"])
        self.assertIn(
            ["systemctl", "disable", "--now", "platform-export@app-lab-clients.timer"],
            self.runner.commands,
        )
        self.assertEqual(existing_instances(self.systemd, "app", "lab"), [])

    def test_one_bad_schedule_costs_that_job_only(self) -> None:
        self.runner = FakeRunner(refuse={"Mon 25:00"})
        jobs = [
            {"name": "bad", "schedule": "Mon 25:00", "to": [], "query": "SELECT 1"},
            {"name": "good", "schedule": "daily", "to": [], "query": "SELECT 1"},
        ]
        result = self.reconcile(manifest(jobs=jobs))

        states = {job["name"]: job["state"] for job in result["jobs"]}
        self.assertEqual(states, {"bad": "unknown", "good": "enabled"})
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("export bad not scheduled", result["warnings"][0])
        self.assertEqual(existing_instances(self.systemd, "app", "lab"), ["good"])

    def test_other_scopes_are_left_alone(self) -> None:
        reconcile_export_timers(
            manifest(),
            "app-lab",
            "lab",
            systemd_root=self.systemd,
            systemctl_executable=Path("systemctl"),
            systemd_analyze_executable=Path("systemd-analyze"),
            runner=self.runner,
        )
        self.assertEqual(existing_instances(self.systemd, "app", "lab"), [])
        removed = disable_export_timers(
            "app", "lab", systemd_root=self.systemd, runner=self.runner
        )
        self.assertEqual(removed, [])
        self.assertEqual(
            existing_instances(self.systemd, "app-lab", "lab"), ["clients"]
        )

    def test_disable_all_removes_every_timer_of_the_scope(self) -> None:
        jobs = [
            {"name": "a", "schedule": "daily", "to": [], "query": "SELECT 1"},
            {"name": "b", "schedule": "daily", "to": [], "query": "SELECT 1"},
        ]
        self.reconcile(manifest(jobs=jobs))
        removed = disable_export_timers(
            "app",
            "lab",
            systemd_root=self.systemd,
            systemctl_executable=Path("systemctl"),
            runner=self.runner,
        )
        self.assertEqual([entry["name"] for entry in removed], ["a", "b"])
        self.assertEqual(existing_instances(self.systemd, "app", "lab"), [])

    def test_a_missing_systemd_root_means_nothing_to_remove(self) -> None:
        self.assertEqual(existing_instances(self.systemd / "nope", "app", "lab"), [])


if __name__ == "__main__":
    unittest.main()
