import subprocess
import tempfile
import unittest
from pathlib import Path

from platform_automation.backup_schedule import (  # noqa: E402
    BackupScheduleError,
    backups_are_scheduled,
    disable_backup_timer,
    enable_backup_timer,
    reconcile_backup_timer,
    render_interval_override,
    resolve_interval,
    split_instance,
    timer_instance,
)


def manifest(
    mode: str = "docker",
    enabled: bool = True,
    interval: int = 360,
) -> dict:
    database = {
        "mode": mode,
        "postgres_major": 18,
        "backup_enabled": enabled,
    }

    if enabled:
        database["backup"] = {"interval_minutes": interval, "retain": 14}

    return {"database": database}


class InstanceNameTest(unittest.TestCase):
    def test_project_and_environment_make_one_name(self) -> None:
        self.assertEqual(
            timer_instance("health-client", "production"),
            "health-client-production",
        )

    def test_a_hyphenated_project_still_splits_back(self) -> None:
        """Environment never contains a hyphen, so the last one is the seam."""
        for project, environment in (
            ("health-client", "lab"),
            ("app", "production"),
            ("my-lab", "staging"),
            ("app-lab", "lab"),
        ):
            with self.subTest(project=project, environment=environment):
                instance = timer_instance(project, environment)

                self.assertEqual(
                    split_instance(instance),
                    (project, environment),
                )

    def test_an_unknown_environment_is_refused(self) -> None:
        with self.assertRaises(BackupScheduleError):
            timer_instance("example", "sandbox")

    def test_a_malformed_instance_is_refused(self) -> None:
        for instance in ("lab", "-lab", "Example-lab", "../etc-lab"):
            with self.subTest(instance=instance):
                with self.assertRaises(BackupScheduleError):
                    split_instance(instance)


class IntervalTest(unittest.TestCase):
    def test_interval_comes_from_the_manifest(self) -> None:
        self.assertEqual(resolve_interval(manifest(interval=45)), 45)

    def test_absent_backup_block_falls_back_to_the_default(self) -> None:
        self.assertEqual(resolve_interval(manifest(enabled=False)), 360)

    def test_out_of_range_intervals_are_refused(self) -> None:
        for interval in (0, 14, 1441, True, "60"):
            with self.subTest(interval=interval):
                with self.assertRaises(BackupScheduleError):
                    resolve_interval(manifest(interval=interval))

    def test_override_pins_the_cadence_and_spreads_the_load(self) -> None:
        content = render_interval_override(60).decode("utf-8")

        self.assertIn("OnUnitActiveSec=60min", content)
        self.assertIn("RandomizedDelaySec=6min", content)
        # Clearing the inherited value first stops systemd merging two.
        self.assertIn("OnUnitActiveSec=\n", content)

    def test_a_short_interval_still_gets_a_delay(self) -> None:
        self.assertIn(
            "RandomizedDelaySec=1min",
            render_interval_override(15).decode("utf-8"),
        )


class ScheduledTest(unittest.TestCase):
    def test_docker_mode_with_backups_is_scheduled(self) -> None:
        self.assertTrue(backups_are_scheduled(manifest()))

    def test_backups_off_is_not_scheduled(self) -> None:
        self.assertFalse(backups_are_scheduled(manifest(enabled=False)))

    def test_external_database_is_never_scheduled(self) -> None:
        self.assertFalse(
            backups_are_scheduled(manifest(mode="external", enabled=False))
        )


class TimerLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.systemd_root = Path(self.temporary_directory.name)
        self.calls: list[list[str]] = []
        self.failing: set[str] = set()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def runner(self, command, **options):
        self.calls.append(list(command))

        return subprocess.CompletedProcess(
            args=command,
            returncode=1 if command[1] in self.failing else 0,
            stdout=b"",
            stderr=b"",
        )

    def verbs(self) -> list[str]:
        return [call[1] for call in self.calls]

    def override(self) -> Path:
        return (
            self.systemd_root / "platform-backup@example-lab.timer.d" / "interval.conf"
        )

    def reconcile(self, document=None):
        return reconcile_backup_timer(
            manifest=document or manifest(),
            project="example",
            environment="lab",
            systemd_root=self.systemd_root,
            systemctl_executable=Path("systemctl"),
            runner=self.runner,
        )

    def test_enabling_writes_the_cadence_and_starts_the_timer(self) -> None:
        result = self.reconcile()

        self.assertEqual(result["unit"], "platform-backup@example-lab.timer")
        self.assertEqual(result["state"], "enabled")
        self.assertEqual(result["interval_minutes"], 360)
        self.assertIn("OnUnitActiveSec=360min", self.override().read_text())
        self.assertEqual(self.verbs(), ["daemon-reload", "enable"])

    def test_an_unchanged_cadence_does_not_reload_systemd(self) -> None:
        self.reconcile()
        self.calls.clear()

        self.reconcile()

        self.assertEqual(self.verbs(), ["enable"])

    def test_a_changed_cadence_reloads(self) -> None:
        self.reconcile()
        self.calls.clear()

        self.reconcile(manifest(interval=60))

        self.assertEqual(self.verbs(), ["daemon-reload", "enable"])
        self.assertIn("OnUnitActiveSec=60min", self.override().read_text())

    def test_turning_backups_off_removes_the_timer(self) -> None:
        self.reconcile()
        self.calls.clear()

        result = self.reconcile(manifest(enabled=False))

        self.assertEqual(result["state"], "disabled")
        self.assertFalse(self.override().parent.exists())
        self.assertEqual(self.verbs(), ["disable", "daemon-reload"])

    def test_an_external_database_never_schedules(self) -> None:
        result = self.reconcile(manifest(mode="external", enabled=False))

        self.assertEqual(result["state"], "disabled")

    def test_disabling_a_timer_that_was_never_there_is_quiet(self) -> None:
        disable_backup_timer(
            "example",
            "lab",
            systemd_root=self.systemd_root,
            systemctl_executable=Path("systemctl"),
            runner=self.runner,
        )

        self.assertEqual(self.verbs(), ["disable"])

    def test_a_failing_systemctl_is_loud(self) -> None:
        self.failing.add("enable")

        with self.assertRaises(BackupScheduleError):
            enable_backup_timer(
                "example",
                "lab",
                360,
                systemd_root=self.systemd_root,
                systemctl_executable=Path("systemctl"),
                runner=self.runner,
            )


if __name__ == "__main__":
    unittest.main()
