import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "reusable-deploy.yml"
EXAMPLE = (
    ROOT
    / "examples"
    / "consumer"
    / "application"
    / ".github"
    / "workflows"
    / "deploy.yml"
)


class ReusableDeployWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        cls.raw = WORKFLOW.read_text(encoding="utf-8")
        cls.steps = cls.document["jobs"]["deploy"]["steps"]

    def call_inputs(self) -> dict:
        # PyYAML reads the bare `on:` key as the boolean True.
        trigger = self.document.get("on", self.document.get(True))
        return trigger["workflow_call"]["inputs"]

    def test_application_commit_is_required(self) -> None:
        """Image and bundle must come from one revision, by construction."""
        commit = self.call_inputs()["application_commit"]

        self.assertTrue(commit["required"])
        self.assertEqual(commit["type"], "string")

    def test_checkout_uses_the_supplied_commit(self) -> None:
        checkout = [
            step
            for step in self.steps
            if isinstance(step.get("uses"), str)
            and step["uses"].startswith("actions/checkout@")
        ]

        self.assertEqual(len(checkout), 1)
        self.assertEqual(
            checkout[0]["with"]["ref"],
            "${{ inputs.application_commit }}",
        )

    def test_a_movable_reference_is_rejected(self) -> None:
        """A branch or tag can move between resolving the image and building
        the bundle; only a full commit SHA cannot."""
        self.assertIn("^[0-9a-f]{40}$", self.raw)

    def test_example_consumer_passes_a_commit(self) -> None:
        example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
        call = example["jobs"]["deploy"]["with"]

        self.assertEqual(
            call["application_commit"],
            "${{ inputs.application_commit }}",
        )


if __name__ == "__main__":
    unittest.main()
