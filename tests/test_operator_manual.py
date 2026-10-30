"""The pieces of a deployment without GitHub, each against a fake tool."""

import base64
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from platform_automation.operator.manual import (
    DEFAULT_PLATFORMS,
    ManualError,
    Workspace,
    build_command,
    build_platforms,
    bundle_minimum_recipients,
    deploy_command,
    digest_command,
    github_reachable,
    image_repository,
    manual_label,
    pushed_digest,
    registry_credentials,
    remote_bundle_path,
    remove_command,
    require_deployable,
    transfer_command,
    workspace,
)

COMMIT = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "ab" * 32


def fake_git(answers: dict):
    """Answers ``git`` by its first verb; a missing verb fails as git would."""

    def runner(command, **kwargs):
        verb = command[1]
        if verb in answers:
            code, out = answers[verb]
            return subprocess.CompletedProcess(command, code, out.encode(), b"")
        return subprocess.CompletedProcess(command, 128, b"", b"fatal: no")

    return runner


class WorkspaceTest(unittest.TestCase):
    def test_reads_commit_tag_and_cleanliness(self) -> None:
        state = workspace(
            Path("/repo"),
            fake_git(
                {
                    "rev-parse": (0, COMMIT + "\n"),
                    "status": (0, ""),
                    "describe": (0, "v0.1.2\n"),
                }
            ),
        )
        self.assertEqual(state, Workspace(COMMIT, "v0.1.2", False))
        require_deployable(state)

    def test_a_tag_that_is_not_a_release_does_not_count(self) -> None:
        state = workspace(
            Path("/repo"),
            fake_git(
                {
                    "rev-parse": (0, COMMIT),
                    "status": (0, ""),
                    "describe": (0, "nightly"),
                }
            ),
        )
        self.assertIsNone(state.tag)
        with self.assertRaisesRegex(ManualError, "not at a release tag"):
            require_deployable(state)

    def test_a_dirty_tree_is_refused_before_the_tag_is_looked_at(self) -> None:
        state = workspace(
            Path("/repo"),
            fake_git(
                {
                    "rev-parse": (0, COMMIT),
                    "status": (0, " M src/app.py\n"),
                    "describe": (0, "v0.1.2"),
                }
            ),
        )
        with self.assertRaisesRegex(ManualError, "uncommitted changes"):
            require_deployable(state)

    def test_no_repository_is_an_error(self) -> None:
        with self.assertRaisesRegex(ManualError, "not a git repository"):
            workspace(Path("/nowhere"), fake_git({}))


class WorkflowReadingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / ".github" / "workflows").mkdir(parents=True)

    def test_platforms_come_from_the_build_workflow(self) -> None:
        (self.root / ".github/workflows/build.yml").write_text(
            "env:\n    REPOSITORY: ghcr.io/org/app\n    PLATFORMS: linux/amd64,linux/arm64\n"
        )
        self.assertEqual(build_platforms(self.root), "linux/amd64,linux/arm64")

    def test_platforms_default_without_a_workflow(self) -> None:
        self.assertEqual(build_platforms(self.root), DEFAULT_PLATFORMS)

    def test_recipients_come_from_the_deploy_workflow(self) -> None:
        (self.root / ".github/workflows/deploy.yml").write_text(
            "        with:\n            minimum_age_recipients: 2\n"
        )
        self.assertEqual(bundle_minimum_recipients(self.root), 2)

    def test_recipients_default_to_one(self) -> None:
        self.assertEqual(bundle_minimum_recipients(self.root), 1)


class ImageTest(unittest.TestCase):
    def test_repository_must_be_on_ghcr(self) -> None:
        self.assertEqual(
            image_repository({"image": {"repository": "ghcr.io/org/app"}}),
            "ghcr.io/org/app",
        )
        with self.assertRaisesRegex(ManualError, "ghcr.io"):
            image_repository({"image": {"repository": "docker.io/org/app"}})
        with self.assertRaisesRegex(ManualError, "image.repository"):
            image_repository({})

    def test_build_tags_by_commit_and_by_release_tag(self) -> None:
        command = build_command(
            "ghcr.io/org/app",
            Workspace(COMMIT, "v0.1.2", False),
            "linux/amd64",
            Path("/tmp/meta.json"),
        )
        self.assertEqual(command[:3], ["docker", "buildx", "build"])
        self.assertIn("--push", command)
        self.assertIn("--provenance=false", command)
        self.assertIn(f"ghcr.io/org/app:{COMMIT}", command)
        self.assertIn("ghcr.io/org/app:v0.1.2", command)
        self.assertEqual(command[command.index("--platform") + 1], "linux/amd64")
        self.assertEqual(command[-1], ".")

    def test_digest_is_read_from_the_metadata_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "meta.json"
            metadata.write_text(json.dumps({"containerimage.digest": DIGEST}))
            self.assertEqual(pushed_digest(metadata), DIGEST)
            metadata.write_text(json.dumps({"containerimage.digest": "latest"}))
            with self.assertRaisesRegex(ManualError, "no image digest"):
                pushed_digest(metadata)
            with self.assertRaisesRegex(ManualError, "cannot read"):
                pushed_digest(Path(directory) / "missing.json")


class CredentialsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name) / "config.json"

    def test_a_credential_helper_is_asked_for_the_registry(self) -> None:
        self.config.write_text(json.dumps({"credsStore": "osxkeychain"}))
        asked = []

        def helper(command, **kwargs):
            asked.append((command, kwargs.get("input")))
            answer = json.dumps({"Username": "me", "Secret": "tok"}).encode()
            return subprocess.CompletedProcess(command, 0, answer, b"")

        self.assertEqual(
            registry_credentials(self.config, runner=helper), ("me", "tok")
        )
        self.assertEqual(
            asked, [(["docker-credential-osxkeychain", "get"], b"ghcr.io\n")]
        )

    def test_an_older_https_login_is_found_on_the_second_try(self) -> None:
        self.config.write_text(json.dumps({"credsStore": "osxkeychain"}))
        asked = []

        def helper(command, **kwargs):
            key = kwargs.get("input")
            asked.append(key)
            if key == b"https://ghcr.io\n":
                answer = json.dumps({"Username": "me", "Secret": "tok"}).encode()
                return subprocess.CompletedProcess(command, 0, answer, b"")
            return subprocess.CompletedProcess(
                command, 1, b"", b"credentials not found"
            )

        self.assertEqual(
            registry_credentials(self.config, runner=helper), ("me", "tok")
        )
        self.assertEqual(asked, [b"ghcr.io\n", b"https://ghcr.io\n"])

    def test_a_per_registry_helper_wins_over_the_store(self) -> None:
        self.config.write_text(
            json.dumps({"credsStore": "osxkeychain", "credHelpers": {"ghcr.io": "gh"}})
        )

        def helper(command, **kwargs):
            self.assertEqual(command[0], "docker-credential-gh")
            answer = json.dumps({"Username": "me", "Secret": "tok"}).encode()
            return subprocess.CompletedProcess(command, 0, answer, b"")

        self.assertEqual(
            registry_credentials(self.config, runner=helper), ("me", "tok")
        )

    def test_no_login_in_the_helper_says_how_to_log_in(self) -> None:
        self.config.write_text(json.dumps({"credsStore": "osxkeychain"}))

        def helper(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 1, b"", b"credentials not found"
            )

        with self.assertRaisesRegex(ManualError, "docker login ghcr.io"):
            registry_credentials(self.config, runner=helper)

    def test_a_plain_auths_entry_is_decoded(self) -> None:
        encoded = base64.b64encode(b"me:tok").decode()
        self.config.write_text(json.dumps({"auths": {"ghcr.io": {"auth": encoded}}}))
        self.assertEqual(registry_credentials(self.config), ("me", "tok"))

    def test_without_a_config_the_operator_is_told_to_log_in(self) -> None:
        with self.assertRaisesRegex(ManualError, "docker login ghcr.io"):
            registry_credentials(self.config)


class CommandsTest(unittest.TestCase):
    def test_label_names_environment_tag_and_the_hand(self) -> None:
        when = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        self.assertEqual(
            manual_label("production", "v0.1.2", when),
            "production-v0.1.2-manual-20260102T0304Z",
        )

    def test_remote_paths_and_commands_quote_what_they_carry(self) -> None:
        path = remote_bundle_path("app", "lab", "lab-v0.1.2-manual-20260102T0304Z")
        self.assertEqual(
            path, "/tmp/app-lab-lab-v0.1.2-manual-20260102T0304Z.bundle.tar.gz"
        )
        self.assertEqual(transfer_command(path), f"umask 077 && cat > {path}")
        self.assertEqual(digest_command(path), f"sha256sum {path} | cut -d ' ' -f 1")
        self.assertEqual(remove_command(path), f"rm -f {path}")

    def test_deploy_line_carries_the_username_but_never_the_token(self) -> None:
        line = deploy_command(
            "app",
            "lab",
            "/tmp/b.tar.gz",
            f"ghcr.io/org/app@{DIGEST}",
            "lab-v0.1.2-manual-x",
            "me",
        )
        self.assertTrue(
            line.startswith("sudo -n platform deploy --project app --environment lab")
        )
        self.assertIn("--registry-username me --registry-token-stdin --json", line)
        self.assertIn(f"--image ghcr.io/org/app@{DIGEST}", line)
        self.assertNotIn("tok", line.split("--registry-username")[1].split()[0:1])


class GithubTest(unittest.TestCase):
    def test_reachable_when_gh_is_logged_in(self) -> None:
        def gh(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, b"", b"")

        self.assertTrue(github_reachable(Path("/repo"), gh))

    def test_unreachable_when_gh_fails_or_is_missing(self) -> None:
        def failing(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, b"", b"not logged in")

        def missing(command, **kwargs):
            raise OSError("no gh")

        self.assertFalse(github_reachable(Path("/repo"), failing))
        self.assertFalse(github_reachable(Path("/repo"), missing))


if __name__ == "__main__":
    unittest.main()
