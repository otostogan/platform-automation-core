import json
import stat
import subprocess
import sys
import tempfile
import unittest
import os
from pathlib import Path


from platform_automation.operation_lock import (  # noqa: E402
    OperationLockError,
    project_environment_lock,
)


CHILD_LOCK_SCRIPT = """
import sys
from pathlib import Path

from platform_automation.operation_lock import (
    OperationAlreadyRunningError,
    project_environment_lock,
)

try:
    with project_environment_lock(
        Path(sys.argv[1]),
        "example",
        "lab",
        "deploy",
    ):
        pass
except OperationAlreadyRunningError:
    print("blocked")
    raise SystemExit(23)

print("acquired")
"""


class OperationLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.lock_root = self.base / "locks"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_child(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                CHILD_LOCK_SCRIPT,
                str(self.lock_root),
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_writes_and_clears_lock_metadata(self) -> None:
        with project_environment_lock(
            self.lock_root,
            "example",
            "lab",
            "deploy",
        ) as lock_path:
            metadata = json.loads(lock_path.read_text(encoding="utf-8"))

            self.assertEqual(metadata["project"], "example")
            self.assertEqual(metadata["environment"], "lab")
            self.assertEqual(metadata["operation"], "deploy")
            self.assertEqual(metadata["pid"], os.getpid())

            self.assertEqual(
                stat.S_IMODE(lock_path.stat().st_mode),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE(self.lock_root.stat().st_mode),
                0o700,
            )

        self.assertTrue(lock_path.exists())
        self.assertEqual(lock_path.read_bytes(), b"")

    def test_blocks_competing_process(self) -> None:
        with project_environment_lock(
            self.lock_root,
            "example",
            "lab",
            "deploy",
        ):
            result = self.run_child()

        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stdout.strip(), "blocked")

    def test_allows_operation_after_release(self) -> None:
        with project_environment_lock(
            self.lock_root,
            "example",
            "lab",
            "deploy",
        ):
            pass

        result = self.run_child()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "acquired")

    def test_different_environment_uses_different_lock(self) -> None:
        with project_environment_lock(
            self.lock_root,
            "example",
            "lab",
            "deploy",
        ):
            with project_environment_lock(
                self.lock_root,
                "example",
                "staging",
                "rollback",
            ):
                pass

    def test_rejects_invalid_identity(self) -> None:
        with self.assertRaisesRegex(
            OperationLockError,
            "invalid project",
        ):
            with project_environment_lock(
                self.lock_root,
                "../example",
                "lab",
                "deploy",
            ):
                pass

        self.assertFalse(self.lock_root.exists())

    def test_rejects_symbolic_link_lock_root(self) -> None:
        actual_directory = self.base / "actual-locks"
        actual_directory.mkdir()
        self.lock_root.symlink_to(
            actual_directory,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(
            OperationLockError,
            "lock root cannot be a symbolic link",
        ):
            with project_environment_lock(
                self.lock_root,
                "example",
                "lab",
                "deploy",
            ):
                pass


if __name__ == "__main__":
    unittest.main()
