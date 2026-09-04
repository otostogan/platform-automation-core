import html
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from platform_automation import __version__
from platform_automation.operator.scaffold import (
    AppAnswers,
    ScaffoldError,
    encrypt_secrets,
    render,
    render_app,
    template,
    validate_app,
    validate_answers,
    write_files,
)

HANDBOOK = Path(__file__).parent.parent / "docs" / "handbook.html"

ANSWERS = AppAnswers(
    project="my-app",
    owner="example",
    environments=("lab", "production"),
    domains={"lab": "lab.my-app.example.com", "production": "my-app.example.com"},
    target_host="platform-host-1.tailnet.example.net",
    recipient_host="age1syntheticfixture",
    recipient_recovery="age1recoveryfixture",
)


def handbook_blocks() -> list:
    """The <pre> blocks of the new-application flow, in page order."""
    text = HANDBOOK.read_text(encoding="utf-8")
    start = text.index('id="flow-new-app"')
    end = text.index("<section", start + 10)
    body = text[start:end]
    return [
        html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))
        for m in re.finditer(r"<pre[^>]*><code>(.*?)</code></pre>", body, re.S)
    ]


def handbook_folds() -> dict:
    """Fold summary (a file name) → the code block it holds, on the new-application page."""
    text = HANDBOOK.read_text(encoding="utf-8")
    start = text.index('id="flow-new-app"')
    end = text.index("<section", start + 10)
    body = text[start:end]
    found = {}
    for m in re.finditer(
        r'<details class="fold">\s*<summary>(.*?)</summary>.*?<pre[^>]*><code>(.*?)</code></pre>',
        body,
        re.S,
    ):
        name = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        found[name] = html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
    return found


class RenderTest(unittest.TestCase):
    def test_only_single_word_tokens_are_replaced(self) -> None:
        text = "{{project}} ${{ github.ref }} {{.Manifest.Digest}} {{unknown}}"

        self.assertEqual(
            render(text, {"project": "my-app"}),
            "my-app ${{ github.ref }} {{.Manifest.Digest}} {{unknown}}",
        )


class TemplatesMatchHandbookTest(unittest.TestCase):
    """The handbook shows these files; the package writes them. One source."""

    def setUp(self) -> None:
        self.blocks = handbook_blocks()
        self.defaults = {
            "internal_port": 3000,
            "healthcheck_path": "/",
            "healthcheck_timeout": 120,
            "postgres_major": 18,
            "backup_interval": 15,
            "backup_retain": 3,
            "restore_query": "SELECT 1",
            "recipient_host": "age1<получатель хоста>",
            "recipient_recovery": "age1<получатель recovery>",
            "core_pin": f"v{__version__}",
        }

    def rendered(self, name: str) -> str:
        return render(template(name), self.defaults).rstrip("\n")

    def assert_block(self, index: int, name: str) -> None:
        self.assertEqual(self.blocks[index].rstrip("\n"), self.rendered(name), name)

    def test_every_template_equals_its_handbook_block(self) -> None:
        self.assert_block(0, "sops.yaml")
        self.assert_block(2, "platform.yml")
        self.assert_block(3, "database_docker.yml")
        self.assert_block(4, "database_external.yml")
        self.assert_block(5, "compose.yml")
        folds = handbook_folds()
        for name, template_name in (
            (".github/workflows/build.yml", "workflow_build.yml"),
            (".github/workflows/release.yml", "workflow_release.yml"),
            (".github/workflows/deploy.yml", "workflow_deploy.yml"),
            (".githooks/post-commit", "hook_post-commit"),
            (".githooks/pre-push", "hook_pre-push"),
            (".githooks/pre-commit", "hook_pre-commit"),
            (".gitignore", "gitignore"),
        ):
            self.assertIn(name, folds, f"the handbook shows no fold named {name}")
            self.assertEqual(
                folds[name].rstrip("\n"), self.rendered(template_name), name
            )


class RenderAppTest(unittest.TestCase):
    def test_writes_one_manifest_and_one_secrets_file_per_environment(self) -> None:
        files = render_app(ANSWERS)

        self.assertIn("deploy/platform.lab.yml", files)
        self.assertIn("deploy/platform.production.yml", files)
        self.assertIn(".env.lab", files)
        self.assertIn("deploy/compose.yml", files)
        self.assertIn(".sops.yaml", files)
        self.assertIn(".github/workflows/deploy.yml", files)
        self.assertIn(".githooks/pre-push", files)
        self.assertIn(".githooks/pre-commit", files)
        self.assertIn(".gitignore", files)
        self.assertEqual(len(files), 9 + 2 * 2)

    def test_manifests_carry_the_answers(self) -> None:
        files = render_app(ANSWERS)
        lab = files["deploy/platform.lab.yml"]

        self.assertIn("project: my-app", lab)
        self.assertIn("environment: lab", lab)
        self.assertIn("host: lab.my-app.example.com", lab)
        self.assertIn("repository: ghcr.io/example/my-app", lab)
        self.assertIn("mode: docker", lab)
        self.assertIn("interval_minutes: 15", lab)
        self.assertIn(
            "target_host: platform-host-1.tailnet.example.net",
            files[".github/workflows/deploy.yml"],
        )
        self.assertIn(
            f"reusable-deploy.yml@v{__version__}", files[".github/workflows/deploy.yml"]
        )
        self.assertIn("- age1syntheticfixture", files[".sops.yaml"])
        self.assertEqual(
            files[".env.lab"],
            "API_TOKEN=замените-на-настоящее\nSESSION_SECRET=замените-на-настоящее\n",
        )

    def test_platform_database_without_a_schedule_keeps_the_contract(self) -> None:
        files = render_app(AppAnswers(**{**ANSWERS.__dict__, "backup_enabled": False}))
        manifest = files["deploy/platform.lab.yml"]

        self.assertIn("mode: docker", manifest)
        self.assertIn("backup_enabled: false", manifest)
        self.assertNotIn("backup:", manifest)
        self.assertNotIn("interval_minutes", manifest)
        self.assertIn("restore_validation:", manifest)

    def test_external_database_disables_backups(self) -> None:
        files = render_app(
            AppAnswers(**{**ANSWERS.__dict__, "database_mode": "external"})
        )

        self.assertIn("backup_enabled: false", files["deploy/platform.lab.yml"])
        self.assertNotIn("interval_minutes", files["deploy/platform.lab.yml"])

    def test_invalid_answers_are_named_before_anything_is_written(self) -> None:
        errors = validate_answers(
            AppAnswers(
                **{
                    **ANSWERS.__dict__,
                    "project": "My App",
                    "domains": {"lab": "LAB", "production": "my-app.example.com"},
                    "secret_names": ("DATABASE_URL",),
                }
            )
        )

        self.assertTrue(any("project" in e for e in errors))
        self.assertTrue(any("domain for lab" in e for e in errors))
        self.assertTrue(any("DATABASE_URL" in e for e in errors))
        with self.assertRaises(ScaffoldError):
            render_app(AppAnswers(**{**ANSWERS.__dict__, "project": "My App"}))


class WriteAndValidateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_written_application_passes_the_deployment_checks(self) -> None:
        files = render_app(ANSWERS)

        written = write_files(self.root, files)
        report = validate_app(self.root, files)

        self.assertEqual(sorted(written), sorted(files))
        self.assertEqual({k: v for k, v in report.items() if v}, {}, report)
        self.assertTrue((self.root / ".githooks/pre-push").stat().st_mode & 0o100)

    def test_unscheduled_database_also_passes_the_deployment_checks(self) -> None:
        files = render_app(AppAnswers(**{**ANSWERS.__dict__, "backup_enabled": False}))

        write_files(self.root, files)
        report = validate_app(self.root, files)

        self.assertEqual({k: v for k, v in report.items() if v}, {}, report)

    def test_existing_file_stops_the_whole_scaffold(self) -> None:
        files = render_app(ANSWERS)
        (self.root / "deploy").mkdir()
        (self.root / "deploy/compose.yml").write_text("mine\n", encoding="utf-8")

        with self.assertRaises(ScaffoldError) as raised:
            write_files(self.root, files)

        self.assertIn("deploy/compose.yml", str(raised.exception))
        self.assertFalse((self.root / ".sops.yaml").exists())
        self.assertEqual(
            (self.root / "deploy/compose.yml").read_text(encoding="utf-8"), "mine\n"
        )

    def test_plaintext_secrets_do_not_survive_a_failed_encryption(self) -> None:
        files = render_app(ANSWERS)
        write_files(self.root, files)

        def failing_sops(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 1, b"", b"config file not found\n"
            )

        with self.assertRaises(ScaffoldError) as raised:
            encrypt_secrets(self.root, files, runner=failing_sops)

        self.assertIn("config file not found", str(raised.exception))
        self.assertFalse((self.root / "deploy/secrets.lab.sops.yaml").exists())

    def test_encryption_calls_sops_in_place_for_each_environment(self) -> None:
        files = render_app(ANSWERS)
        write_files(self.root, files)
        calls = []

        def fake_sops(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, b"ciphertext\n", b"")

        encrypted = encrypt_secrets(self.root, files, runner=fake_sops)

        self.assertEqual(
            sorted(encrypted),
            ["deploy/secrets.lab.sops.yaml", "deploy/secrets.production.sops.yaml"],
        )
        self.assertTrue(
            all(
                c[0] == "sops" and "--encrypt" in c and "--filename-override" in c
                for c in calls
            )
        )


if __name__ == "__main__":
    unittest.main()
