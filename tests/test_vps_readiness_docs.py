import re
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles" / "vps_readiness"


class VpsReadinessDocsTest(unittest.TestCase):
    def test_every_variable_is_documented(self) -> None:
        """A variable that silently halves the audit must not be discoverable
        only by reading the tasks."""
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text("utf-8"))
        readme = (ROLE / "README.md").read_text("utf-8")

        undocumented = sorted(name for name in defaults if name not in readme)

        self.assertEqual(undocumented, [], f"not in README: {undocumented}")

    def test_documented_defaults_match_the_role(self) -> None:
        """A README that drifts from the defaults is worse than none."""
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text("utf-8"))
        readme = (ROLE / "README.md").read_text("utf-8")

        rows = dict(
            re.findall(r"^\| `(vps_readiness_\w+)` \| `?([^|`]+?)`? \|", readme, re.M)
        )

        for name in ("vps_readiness_phase", "vps_readiness_output"):
            with self.subTest(name):
                self.assertEqual(rows.get(name), str(defaults[name]))

        self.assertEqual(rows.get("vps_readiness_fail_on_error"), "true")
        self.assertIn("The default is `pre`", readme)

    def test_both_phases_are_selectable(self) -> None:
        main = (ROLE / "tasks/main.yml").read_text("utf-8")

        self.assertIn('vps_readiness_phase in ["pre", "post"]', main)
        self.assertIn('when: vps_readiness_phase == "post"', main)
        self.assertTrue((ROLE / "tasks/pre.yml").is_file())
        self.assertTrue((ROLE / "tasks/post.yml").is_file())


if __name__ == "__main__":
    unittest.main()
