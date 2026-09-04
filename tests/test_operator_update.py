import tempfile
import unittest
from pathlib import Path

from platform_automation import __version__
from platform_automation.operator.context import detect
from platform_automation.operator.scaffold import (
    AppAnswers,
    is_managed,
    marker_version,
    render_app,
    strip_marker,
    with_marker,
    write_files,
)
from platform_automation.operator.update import (
    UpdateError,
    apply,
    behind,
    console_ahead_of_hosts,
    gather_facts,
    plan,
    recipients_changed,
    render_managed,
)

ANSWERS = AppAnswers(
    project="my-app",
    owner="example",
    environments=("lab", "staging", "production"),
    domains={
        "lab": "lab.my-app.example.com",
        "staging": "staging.my-app.example.com",
        "production": "my-app.example.com",
    },
    target_host="platform-host-1.tailnet.example.net",
    recipient_host="age1syntheticfixture",
    recipient_recovery="age1recoveryfixture",
    core_pin="v0.15.1",
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class MarkerTest(unittest.TestCase):
    def test_marker_follows_a_shebang_and_leads_everything_else(self) -> None:
        hook = with_marker("#!/bin/sh\nset -eu\n")
        workflow = with_marker("---\nname: x\n")

        self.assertTrue(hook.startswith("#!/bin/sh\n# platform-managed v"))
        self.assertTrue(workflow.startswith("# platform-managed v"))
        self.assertEqual(marker_version(hook), __version__)
        self.assertEqual(strip_marker(hook), "#!/bin/sh\nset -eu\n")
        self.assertEqual(strip_marker(workflow), "---\nname: x\n")
        self.assertFalse(is_managed("#!/bin/sh\nset -eu\n"))

    def test_new_app_stamps_the_files_it_will_update_and_no_others(self) -> None:
        files = render_app(ANSWERS)

        for path in (
            ".githooks/pre-commit",
            ".github/workflows/deploy.yml",
            ".sops.yaml",
        ):
            self.assertTrue(is_managed(files[path]), path)
        for path in (
            "deploy/compose.yml",
            "deploy/platform.lab.yml",
            ".gitignore",
            ".env.lab",
        ):
            self.assertFalse(is_managed(files[path]), path)

    def test_deploy_offers_exactly_the_chosen_environments(self) -> None:
        deploy = render_app(ANSWERS)[".github/workflows/deploy.yml"]

        self.assertIn(
            "                    - lab\n                    - staging\n                    - production\n",
            deploy,
        )

        two = render_app(
            AppAnswers(
                **{**ANSWERS.__dict__, "environments": ("lab", "production")},
            )
        )[".github/workflows/deploy.yml"]
        self.assertNotIn("- staging", two)

    def test_console_ahead_of_hosts(self) -> None:
        self.assertTrue(console_ahead_of_hosts("v0.15.0", "0.15.1"))
        self.assertFalse(console_ahead_of_hosts("v0.15.1", "0.15.1"))
        self.assertFalse(console_ahead_of_hosts("v0.16.0", "0.15.1"))
        self.assertFalse(console_ahead_of_hosts("main", "0.15.1"))


class UpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root = base / "app"
        self.home = base / "home"
        self.root.mkdir()
        self.home.mkdir()
        self.files = render_app(ANSWERS)
        write_files(self.root, self.files)

    def facts(self):
        context = detect(self.root, host_marker=self.home / "no-host")
        self.assertEqual(context.kind, "app")
        return gather_facts(context, self.home)

    def test_facts_are_read_back_from_the_repository(self) -> None:
        facts = self.facts()

        self.assertEqual(facts.project, "my-app")
        self.assertEqual(facts.owner, "example")
        self.assertEqual(facts.target_host, "platform-host-1.tailnet.example.net")
        self.assertEqual(facts.environments, ("lab", "staging", "production"))
        self.assertEqual(facts.core_pin, "v0.15.1")
        self.assertEqual(facts.pin_source, "deploy.yml")
        self.assertEqual(
            (facts.recipient_host, facts.recipient_recovery),
            ("age1syntheticfixture", "age1recoveryfixture"),
        )
        self.assertEqual(facts.recipients_source, ".sops.yaml")

    def test_a_fresh_application_has_nothing_to_update(self) -> None:
        changes = plan(self.root, render_managed(self.facts()))

        self.assertEqual({change.kind for change in changes}, {"same"})
        self.assertEqual(behind(self.root, render_managed(self.facts())), [])

    def test_registered_infrastructure_supplies_pin_and_recipients(self) -> None:
        infra = self.home / "platform-infra"
        write(
            infra / "requirements.yml",
            "collections:\n  - name: https://github.com/otostogan/platform-automation-core/"
            "releases/download/v0.15.0/otostogan-platform-0.15.0.tar.gz\n    type: url\n",
        )
        write(
            infra / "inventory/hosts.yml",
            "all:\n  children:\n    platform_hosts:\n      hosts:\n        platform-host-1:\n"
            "          ansible_host: platform-host-1.tailnet.example.net\n          ansible_user: ops\n",
        )
        write(
            infra / "docs/RECIPIENTS.md",
            "# Recipients\n\n```\nplatform-host-1 age1syntheticfixture\nrecovery age1othersynthetic\n```\n",
        )
        write(
            self.home / ".config/platform/config.yml",
            f"infras:\n  - path: {infra}\n",
        )

        facts = self.facts()
        self.assertEqual(facts.core_pin, "v0.15.0")
        self.assertEqual(facts.pin_source, "infra:platform-infra")
        self.assertEqual(facts.recipient_recovery, "age1othersynthetic")

        changes = {
            change.path: change for change in plan(self.root, render_managed(facts))
        }
        self.assertEqual(changes[".sops.yaml"].kind, "update")
        self.assertEqual(changes[".github/workflows/deploy.yml"].kind, "update")
        self.assertEqual(changes[".githooks/pre-commit"].kind, "same")
        self.assertTrue(recipients_changed(list(changes.values())))
        self.assertIn(
            "-                - age1recoveryfixture", changes[".sops.yaml"].diff()
        )
        self.assertIn(
            "+                - age1othersynthetic", changes[".sops.yaml"].diff()
        )

    def test_a_managed_file_is_rewritten_and_an_owned_one_is_left(self) -> None:
        hook = self.root / ".githooks/pre-commit"
        hook.write_text(hook.read_text() + "\necho drifted\n", encoding="utf-8")
        push = self.root / ".githooks/pre-push"
        push.write_text(
            strip_marker(push.read_text()) + "\necho mine\n", encoding="utf-8"
        )
        (self.root / ".githooks/post-commit").unlink()
        gitignore = self.root / ".gitignore"
        gitignore.write_text("node_modules/\n", encoding="utf-8")

        files = render_managed(self.facts())
        changes = {change.path: change for change in plan(self.root, files)}
        self.assertEqual(changes[".githooks/pre-commit"].kind, "update")
        self.assertEqual(changes[".githooks/pre-push"].kind, "owned")
        self.assertEqual(changes[".githooks/post-commit"].kind, "create")
        self.assertEqual(changes[".gitignore"].kind, "merge")
        self.assertEqual(
            sorted(behind(self.root, files)),
            [".githooks/post-commit", ".githooks/pre-commit", ".gitignore"],
        )

        written = apply(self.root, list(changes.values()))

        self.assertEqual(
            sorted(written),
            [".githooks/post-commit", ".githooks/pre-commit", ".gitignore"],
        )
        self.assertEqual(
            hook.read_text(encoding="utf-8"), files[".githooks/pre-commit"]
        )
        self.assertTrue(push.read_text(encoding="utf-8").endswith("echo mine\n"))
        self.assertTrue((self.root / ".githooks/post-commit").stat().st_mode & 0o100)
        self.assertTrue(
            gitignore.read_text(encoding="utf-8").startswith("node_modules/\n")
        )
        self.assertIn(".env.*\n", gitignore.read_text(encoding="utf-8"))

    def test_files_copied_from_the_handbook_are_adopted(self) -> None:
        for relative in (".githooks/pre-push", ".github/workflows/build.yml"):
            path = self.root / relative
            path.write_text(
                strip_marker(path.read_text(encoding="utf-8")), encoding="utf-8"
            )

        changes = {
            change.path: change
            for change in plan(self.root, render_managed(self.facts()))
        }

        self.assertEqual(changes[".githooks/pre-push"].kind, "adopt")
        self.assertEqual(changes[".github/workflows/build.yml"].kind, "adopt")
        apply(self.root, list(changes.values()))
        self.assertTrue(
            is_managed((self.root / ".githooks/pre-push").read_text(encoding="utf-8"))
        )

    def test_an_application_without_a_target_host_cannot_be_updated(self) -> None:
        write(self.root / ".github/workflows/deploy.yml", "jobs: {}\n")

        with self.assertRaises(UpdateError):
            self.facts()
