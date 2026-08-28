import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "roles"


def apt_tasks(path: Path):
    for task in yaml.safe_load(path.read_text("utf-8")) or []:
        if isinstance(task, dict) and "ansible.builtin.apt" in task:
            yield task


class ConvergeIdempotenceTest(unittest.TestCase):
    def test_cache_refresh_alone_never_reports_changed(self) -> None:
        """Converge is accepted by reporting no change on a second run. A task
        that refreshes the package index reports changed once the cache ages
        out, which would make that signal meaningless."""
        offenders = []

        for path in sorted(ROLES.glob("*/tasks/*.yml")):
            for task in apt_tasks(path):
                module = task["ansible.builtin.apt"]
                if not isinstance(module, dict):
                    continue
                refreshes = module.get("update_cache") is True
                installs = "name" in module or "package" in module
                if refreshes and not installs and task.get("changed_when") is not False:
                    offenders.append(f"{path.relative_to(ROOT)}: {task.get('name')}")

        self.assertEqual(offenders, [], f"cache refresh reports changed: {offenders}")


if __name__ == "__main__":
    unittest.main()
