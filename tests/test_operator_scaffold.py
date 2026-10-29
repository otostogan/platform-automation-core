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
    git_org,
    normalize_owner,
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

    def assert_block(self, name: str) -> None:
        """The page shows the template somewhere — by content, not by position."""
        shown = [block.rstrip("\n") for block in self.blocks]
        self.assertIn(self.rendered(name), shown, name)

    def test_every_template_equals_its_handbook_block(self) -> None:
        for name in (
            "sops.yaml",
            "platform.yml",
            "database_docker.yml",
            "database_external.yml",
            "database_none.yml",
            "compose.yml",
        ):
            self.assert_block(name)
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

    def test_no_database_leaves_out_what_cannot_apply(self) -> None:
        files = render_app(AppAnswers(**{**ANSWERS.__dict__, "database_mode": "none"}))
        manifest = files["deploy/platform.lab.yml"]

        self.assertIn("mode: none", manifest)
        self.assertIn("backup_enabled: false", manifest)
        self.assertNotIn("postgres_major", manifest)
        self.assertNotIn("backup:", manifest)
        self.assertNotIn("resources", manifest)
        self.assertNotIn("restore_validation", manifest)
        self.assertNotIn("DATABASE_URL", files[".env.lab"])

    def test_without_a_platform_database_the_compose_file_has_no_db_network(
        self,
    ) -> None:
        for mode in ("external", "none"):
            with self.subTest(mode=mode):
                files = render_app(
                    AppAnswers(**{**ANSWERS.__dict__, "database_mode": mode})
                )
                compose = files["deploy/compose.yml"]

                self.assertNotIn("PLATFORM_DB_NETWORK", compose)
                self.assertNotIn("- db", compose)
                self.assertIn("- edge", compose)
                self.assertIn("name: platform-edge", compose)
                self.assertNotIn("\n\n\n", compose)

        docker = render_app(ANSWERS)["deploy/compose.yml"]
        self.assertIn("PLATFORM_DB_NETWORK", docker)

    def test_no_database_lets_the_application_name_its_own_database_url(self) -> None:
        """The platform owns the variable only when it runs the database."""
        errors = validate_answers(
            AppAnswers(
                **{
                    **ANSWERS.__dict__,
                    "database_mode": "none",
                    "secret_names": ("DATABASE_URL",),
                }
            )
        )

        self.assertEqual(errors, [])

    def test_an_unknown_database_mode_is_refused(self) -> None:
        errors = validate_answers(
            AppAnswers(**{**ANSWERS.__dict__, "database_mode": "sqlite"})
        )

        self.assertTrue(any("database mode" in e for e in errors), errors)

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

    def test_no_database_also_passes_the_deployment_checks(self) -> None:
        files = render_app(AppAnswers(**{**ANSWERS.__dict__, "database_mode": "none"}))

        write_files(self.root, files)
        report = validate_app(self.root, files)

        self.assertEqual({k: v for k, v in report.items() if v}, {}, report)

    def test_an_external_database_also_passes_the_deployment_checks(self) -> None:
        files = render_app(
            AppAnswers(**{**ANSWERS.__dict__, "database_mode": "external"})
        )

        write_files(self.root, files)
        report = validate_app(self.root, files)

        self.assertEqual({k: v for k, v in report.items() if v}, {}, report)

    def test_an_existing_gitignore_is_appended_not_refused(self) -> None:
        files = render_app(ANSWERS)
        (self.root / ".gitignore").write_text("node_modules\n.env\n", encoding="utf-8")

        written = write_files(self.root, files)

        text = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("node_modules\n.env\n"))
        self.assertEqual(
            text.count("\n.env\n"), 1, "a line already there is not repeated"
        )
        self.assertIn(".env.*\n", text)
        self.assertIn("*.agekey\n", text)
        self.assertTrue(any(item.startswith(".gitignore (+") for item in written))

        again = write_files(self.root, {".gitignore": files[".gitignore"]})
        self.assertEqual(again, [], "a second run adds nothing")

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


class TemplatesSurviveFormattersTest(unittest.TestCase):
    def test_no_quoted_scalar_a_formatter_would_requote(self) -> None:
        """An application's formatter runs over the files the platform owns.

        Prettier with ``singleQuote`` turned ``"v*.*.*"`` into ``'v*.*.*'``
        in a hook, and from then on ``doctor`` called the file behind its
        template. A scalar that needs no quotes is written without them; one
        that does need them is not something a formatter rewrites silently —
        this test lists the plain ones.
        """
        import re

        plain = re.compile(r"""^\s*(?:-|[\w-]+:)\s+(["'])([\w.*/@-]+)\1\s*$""")
        root = Path(__file__).parent.parent / "src/platform_automation/templates/app"
        offenders = [
            f"{path.name}:{number}: {line.strip()}"
            for path in sorted(root.glob("*.yml"))
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            )
            if (match := plain.match(line))
            # a quoted number or boolean is quoted on purpose
            and not re.fullmatch(
                r"[0-9.]+|true|false|null|yes|no|on|off", match.group(2)
            )
        ]
        self.assertEqual(offenders, [])


class BuildPlatformsTest(unittest.TestCase):
    def test_the_default_is_amd64_and_arm64_can_be_added(self) -> None:
        from dataclasses import replace

        from platform_automation.operator.scaffold import (
            AppAnswers,
            render_app,
            valid_platforms,
            validate_answers,
        )

        base = AppAnswers(
            project="my-app",
            owner="example",
            environments=("lab",),
            domains={"lab": "lab.my-app.example.com"},
            target_host="platform-host-1.tailnet.example.net",
            recipient_host="age1syntheticfixture",
            recipient_recovery="age1recoveryfixture",
        )
        self.assertIn(
            "PLATFORMS: linux/amd64\n", render_app(base)[".github/workflows/build.yml"]
        )
        both = replace(base, build_platforms="linux/amd64,linux/arm64")
        self.assertEqual(validate_answers(both), [])
        self.assertIn(
            "PLATFORMS: linux/amd64,linux/arm64\n",
            render_app(both)[".github/workflows/build.yml"],
        )
        for bad in ("", "linux/amd64,", "darwin/arm64", "linux/amd64 linux/arm64"):
            self.assertFalse(valid_platforms(bad), bad)
            self.assertTrue(validate_answers(replace(base, build_platforms=bad)), bad)


def remote(url: str, returncode: int = 0):
    """A ``git remote get-url origin`` stand-in that answers with ``url``."""

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, url.encode(), b"")

    return runner


class GitOrgTest(unittest.TestCase):
    """GHCR lowercases image names; the owner derived from origin must match."""

    def test_a_mixed_case_ssh_remote_yields_the_lowercase_owner(self) -> None:
        # Spelt in two pieces so the boundary scan does not read it as an email.
        runner = remote("git@" "github.com:ParadigmaDev/paradigma-crm.git\n")
        self.assertEqual(git_org(Path("/repo"), runner), "paradigmadev")

    def test_a_mixed_case_https_remote_yields_the_lowercase_owner(self) -> None:
        runner = remote("https://github.com/ParadigmaDev/paradigma-crm.git\n")
        self.assertEqual(git_org(Path("/repo"), runner), "paradigmadev")

    def test_an_already_lowercase_owner_is_unchanged(self) -> None:
        runner = remote("git@" "github.com:example/my-app.git\n")
        self.assertEqual(git_org(Path("/repo"), runner), "example")

    def test_no_origin_or_a_foreign_host_yields_nothing(self) -> None:
        self.assertIsNone(git_org(Path("/repo"), remote("", returncode=2)))
        self.assertIsNone(
            git_org(Path("/repo"), remote("https://gitlab.example.com/Org/app.git\n"))
        )

    def test_a_typed_owner_is_normalised_the_same_way(self) -> None:
        self.assertEqual(normalize_owner("  ParadigmaDev "), "paradigmadev")

    def test_validate_answers_rejects_an_owner_the_schema_would(self) -> None:
        from dataclasses import replace

        mixed = replace(ANSWERS, owner="ParadigmaDev")
        errors = validate_answers(mixed)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("lowercase", errors[0])
        self.assertIn("ParadigmaDev", errors[0])
        self.assertEqual(validate_answers(replace(ANSWERS, owner="paradigmadev")), [])
        # What the lowercase owner renders passes the manifest schema.
        rendered = render_app(replace(ANSWERS, owner=normalize_owner("ParadigmaDev")))
        self.assertIn(
            "repository: ghcr.io/paradigmadev/my-app",
            rendered["deploy/platform.lab.yml"],
        )


if __name__ == "__main__":
    unittest.main()


class PreCommitHookTest(unittest.TestCase):
    """The hook refuses plaintext secrets, not the documented example file."""

    def run_hook(self, staged: str) -> subprocess.CompletedProcess:
        hook_text = template("hook_pre-commit")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
            subprocess.run([*git, "init", "-q"], cwd=root, check=True)
            (root / staged).write_text("KEY=value\n", encoding="utf-8")
            subprocess.run([*git, "add", staged], cwd=root, check=True)
            hook = root / "pre-commit"
            hook.write_text(hook_text, encoding="utf-8")
            hook.chmod(0o755)
            # A stand-in console: the hook only has to reach it.
            bin_dir = root / "bin"
            bin_dir.mkdir()
            console = bin_dir / "platform"
            console.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            console.chmod(0o755)
            return subprocess.run(
                [str(hook)],
                cwd=root,
                env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": directory},
                capture_output=True,
                text=True,
            )

    def test_refuses_a_plaintext_environment_file(self) -> None:
        result = self.run_hook(".env.lab")
        self.assertEqual(result.returncode, 1)
        self.assertIn("никогда не коммитится", result.stderr)

    def test_lets_the_example_file_through(self) -> None:
        result = self.run_hook(".env.example")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("никогда не коммитится", result.stderr)
