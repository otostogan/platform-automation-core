"""The console's deployment without GitHub, driven end to end with fake tools.

docker, ssh, the bundle builder and git are all stand-ins; what is checked
is the order of the three steps, what each is told, what the operator sees
before anything runs, and that the registry token never reaches the screen.
"""

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from platform_automation.operator import console, manual

COMMIT = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "cd" * 32
BUNDLE_BYTES = b"bundle-bytes"
BUNDLE_DIGEST = hashlib.sha256(BUNDLE_BYTES).hexdigest()


class Answer:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value


class FakeQuestionary:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    class Choice:
        def __init__(self, title, value):
            self.title, self.value = title, value

    def _next(self, kind, message):
        self.asked.append((kind, message))
        return Answer(self.answers.pop(0))

    def text(self, message, **kwargs):
        return self._next("text", message)

    def select(self, message, choices, **kwargs):
        return self._next("select", message)

    def confirm(self, message, **kwargs):
        return self._next("confirm", message)


class ManualDeployActionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        (root / "deploy").mkdir()
        (root / "deploy" / "platform.lab.yml").write_text(
            "image:\n    repository: ghcr.io/org/example\n"
            "service:\n    web: web\ncompose_file: deploy/compose.yml\n"
        )
        (root / ".github" / "workflows").mkdir(parents=True)
        (root / ".github/workflows/build.yml").write_text(
            "    PLATFORMS: linux/amd64\n"
        )
        (root / ".github/workflows/deploy.yml").write_text(
            "    minimum_age_recipients: 2\n"
        )
        self.root = root
        self.context = SimpleNamespace(
            root=root, target_host="host.example.invalid", core_pin="v0.31.1"
        )
        self.scope = SimpleNamespace(
            project="example",
            environment="lab",
            manifest=Path("deploy/platform.lab.yml"),
        )
        self.running = SimpleNamespace(
            available=True, backend_state="Running", error=None
        )
        self.calls = []

    def fake_call(self, command, **kwargs):
        """docker: records the build line and writes the metadata buildx would."""
        self.calls.append(("docker", command))
        metadata = Path(command[command.index("--metadata-file") + 1])
        metadata.write_text(json.dumps({"containerimage.digest": DIGEST}))
        return 0

    def fake_bundle(
        self, manifest_path, output_path, app_root=None, minimum_age_recipients=1
    ):
        self.calls.append(
            ("bundle", manifest_path.name, app_root, minimum_age_recipients)
        )
        output_path.write_bytes(BUNDLE_BYTES)
        return BUNDLE_DIGEST

    def fake_ssh(self, command, stdin=None, **kwargs):
        """ssh: answers the transfer, the digest check, the deploy and the cleanup."""
        remote = command[-1]
        received = stdin.read() if hasattr(stdin, "read") else None
        self.calls.append(("ssh", remote, received))
        if remote.startswith("sha256sum"):
            return subprocess.CompletedProcess(
                command, 0, f"{BUNDLE_DIGEST}\n".encode(), b""
            )
        if "platform deploy" in remote:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"status": "deployed"}).encode(), b""
            )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def drive(self, questionary, github=False, workspace=None):
        workspace = workspace or manual.Workspace(COMMIT, "v0.1.2", False)
        status = SimpleNamespace(command="ssh … status", run=lambda: 0)
        output = io.StringIO()
        with (
            mock.patch.object(console, "read_tailnet", return_value=self.running),
            mock.patch.object(manual, "workspace", return_value=workspace),
            mock.patch.object(
                manual, "registry_credentials", return_value=("me", "s3cret-token")
            ),
            mock.patch.object(manual, "github_reachable", return_value=github),
            mock.patch.object(console.subprocess, "call", side_effect=self.fake_call),
            mock.patch(
                "platform_automation.build_bundle.create_bundle",
                side_effect=self.fake_bundle,
            ),
            mock.patch(
                "platform_automation.operator.remote.checked_run",
                side_effect=self.fake_ssh,
            ),
            mock.patch.object(console, "remote_action", return_value=status),
            redirect_stdout(output),
        ):
            action = console.manual_deploy_action(
                self.context, self.scope, (questionary, None)
            )
            code = action.run()
        return code, output.getvalue()

    def test_builds_pushes_bundles_transfers_and_deploys_in_order(self) -> None:
        questionary = FakeQuestionary([True])
        code, shown = self.drive(questionary)

        self.assertEqual(code, 0, shown)
        kinds = [c[0] for c in self.calls]
        self.assertEqual(kinds, ["docker", "bundle", "ssh", "ssh", "ssh", "ssh"])

        build = self.calls[0][1]
        self.assertIn("ghcr.io/org/example:v0.1.2", build)
        self.assertIn(f"ghcr.io/org/example:{COMMIT}", build)
        self.assertEqual(self.calls[1][1:], ("platform.lab.yml", self.root, 2))

        transfer, digest, deploy, cleanup = self.calls[2:]
        self.assertTrue(
            transfer[1].startswith(
                "umask 077 && cat > /tmp/example-lab-lab-v0.1.2-manual-"
            )
        )
        self.assertEqual(transfer[2], BUNDLE_BYTES)
        self.assertTrue(digest[1].startswith("sha256sum /tmp/example-lab-"))
        self.assertIn(f"--image ghcr.io/org/example@{DIGEST}", deploy[1])
        self.assertIn("--registry-username me --registry-token-stdin --json", deploy[1])
        self.assertEqual(deploy[2], b"s3cret-token\n")
        self.assertTrue(cleanup[1].startswith("rm -f /tmp/example-lab-"))

        self.assertNotIn("s3cret-token", shown)
        self.assertIn("deployed", shown)
        self.assertIn("manual-", shown)

    def test_production_needs_its_name_typed(self) -> None:
        self.scope.environment = "production"
        questionary = FakeQuestionary(["nope"])
        code, shown = self.drive(questionary)
        self.assertEqual(code, 130)
        self.assertEqual(self.calls, [])
        self.assertIn("nothing built", shown)

    def test_while_github_answers_the_operator_must_spell_break_glass(self) -> None:
        questionary = FakeQuestionary([""])
        code, shown = self.drive(questionary, github=True)
        self.assertEqual(code, 130)
        self.assertEqual(self.calls, [])
        self.assertIn("use Deploy", shown)
        self.assertEqual(questionary.asked[0][0], "text")
        self.assertIn("break-glass", questionary.asked[0][1])

    def test_break_glass_typed_goes_on(self) -> None:
        questionary = FakeQuestionary(["break-glass", True])
        code, shown = self.drive(questionary, github=True)
        self.assertEqual(code, 0, shown)
        self.assertEqual([c[0] for c in self.calls][:2], ["docker", "bundle"])

    def test_a_dirty_tree_stops_before_any_question(self) -> None:
        questionary = FakeQuestionary([])
        code, shown = self.drive(
            questionary, workspace=manual.Workspace(COMMIT, "v0.1.2", True)
        )
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes", shown)
        self.assertEqual(self.calls, [])

    def test_a_digest_mismatch_removes_the_bundle_and_deploys_nothing(self) -> None:
        def ssh(command, stdin=None, **kwargs):
            remote = command[-1]
            self.calls.append(("ssh", remote, None))
            if remote.startswith("sha256sum"):
                return subprocess.CompletedProcess(command, 0, b"deadbeef\n", b"")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        questionary = FakeQuestionary([True])
        with mock.patch.object(self, "fake_ssh", side_effect=ssh):
            code, shown = self.drive(questionary)
        self.assertEqual(code, 1)
        remotes = [c[1] for c in self.calls if c[0] == "ssh"]
        self.assertTrue(any(r.startswith("rm -f") for r in remotes))
        self.assertFalse(any("platform deploy" in r for r in remotes))
        self.assertIn("does not match", shown)


if __name__ == "__main__":
    unittest.main()
