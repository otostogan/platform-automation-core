import html
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from platform_automation import __version__
from platform_automation.operator.company import (
    ACME_PRODUCTION,
    CompanyAnswers,
    CompanyError,
    render_company,
    validate_company_answers,
    write_company,
)
from platform_automation.operator.config import infras
from platform_automation.operator.hosts import HostAnswers, template
from platform_automation.operator.scaffold import render

HANDBOOK = Path(__file__).parent.parent / "docs" / "handbook.html"


def fake_runner(command, **kwargs):
    """ssh-keygen, age-keygen and git init, without the real tools."""
    if command[0] == "ssh-keygen":
        path = Path(command[command.index("-f") + 1])
        comment = command[command.index("-C") + 1]
        path.write_text("synthetic private half\n", encoding="utf-8")
        path.with_name(path.name + ".pub").write_text(
            f"ssh-ed25519 AAAAC3synthetic {comment}\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, b"", b"")
    if command[0] == "age-keygen":
        path = Path(command[command.index("--output") + 1])
        path.write_text("# synthetic private half\n", encoding="utf-8")
        stamp = path.stem.replace("-", "")[:12]
        return subprocess.CompletedProcess(
            command, 0, b"", f"Public key: age1{stamp}fixture\n".encode()
        )
    if command[:2] == ["git", "init"]:
        (Path(kwargs["cwd"]) / ".git").mkdir()
        return subprocess.CompletedProcess(command, 0, b"", b"")
    raise AssertionError(f"unexpected command {command}")


def answers(base: Path) -> CompanyAnswers:
    return CompanyAnswers(
        company="example",
        acme_email="ops@example.invalid",
        operator="alex",
        operator_key=str(base / "ssh/example-ops"),
        extra_ops_keys=("ssh-ed25519 AAAAC3other ops:second",),
        host=HostAnswers(
            name="platform-host-1",
            public_address="203.0.113.10",
            tailnet="platform-host-1.tailnet.example.net",
            ssh_key=str(base / "ssh/example-ops"),
            keys_dir=str(base / "keys"),
        ),
    )


class CompanyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "example-infra"
        self.home = self.base / "home"
        self.home.mkdir()
        self.answers = answers(self.base)

    def test_validation_names_every_bad_answer(self) -> None:
        bad = CompanyAnswers(
            company="Ex",
            acme_email="nobody",
            operator=" ",
            operator_key="",
            acme_ca="https://example.invalid/",
            extra_ops_keys=("not a key",),
            host=self.answers.host,
        )
        self.assertEqual(
            len(validate_company_answers(bad)), 6, validate_company_answers(bad)
        )
        self.assertEqual(validate_company_answers(self.answers), [])

    def test_render_pins_this_core_and_lists_every_operator(self) -> None:
        files = render_company(self.answers, "ssh-ed25519 AAAAC3synthetic ops:alex")

        self.assertIn(
            f"download/v{__version__}/otostogan-platform-{__version__}.tar.gz",
            files["requirements.yml"],
        )
        group = yaml.safe_load(files["inventory/group_vars/platform_hosts.yml"])
        self.assertEqual(
            group["users_ops_ssh_keys"],
            [
                "ssh-ed25519 AAAAC3synthetic ops:alex",
                "ssh-ed25519 AAAAC3other ops:second",
            ],
        )
        self.assertEqual(group["users_deploy_ssh_keys"], [])
        self.assertEqual(group["proxy_acme_email"], "ops@example.invalid")
        self.assertIn("acme-staging", group["proxy_acme_ca_uri"])
        self.assertIn(
            f"{self.base}/keys/tailscale-auth.key",
            files["inventory/group_vars/all/local-secrets.yml"],
        )
        self.assertIn("# example platform infrastructure", files["README.md"])

    def test_write_makes_a_registered_git_repository_with_its_first_host(self) -> None:
        result = write_company(
            self.root, self.answers, runner=fake_runner, home=self.home
        )

        self.assertTrue((self.root / ".git").is_dir())
        self.assertTrue(result.git_initialised)
        self.assertEqual(
            result.operator_public_key, "ssh-ed25519 AAAAC3synthetic ops:alex"
        )
        self.assertEqual(sorted(result.recipients), ["platform-host-1", "recovery"])
        hosts = yaml.safe_load((self.root / "inventory/hosts.yml").read_text())
        self.assertEqual(
            hosts["all"]["children"]["platform_hosts"]["hosts"]["platform-host-1"][
                "ansible_host"
            ],
            "platform-host-1.tailnet.example.net",
        )
        boot = yaml.safe_load((self.root / "inventory/bootstrap.yml").read_text())
        self.assertEqual(
            boot["all"]["children"]["platform_hosts"]["hosts"]["platform-host-1"][
                "ansible_user"
            ],
            "root",
        )
        self.assertIn(
            "# Steady-state inventory", (self.root / "inventory/hosts.yml").read_text()
        )
        published = (self.root / "docs/RECIPIENTS.md").read_text()
        self.assertIn(
            f"platform-host-1  {result.recipients['platform-host-1']}", published
        )
        self.assertIn(f"recovery         {result.recipients['recovery']}", published)
        self.assertTrue(
            (
                self.root / "inventory/host_vars/platform-host-1/local-secrets.yml"
            ).is_file()
        )
        self.assertTrue((self.base / "keys/recovery.agekey").is_file())
        self.assertTrue((self.base / "ssh/example-ops.pub").is_file())
        registered = infras(self.home)
        self.assertEqual([i.path for i in registered], [self.root.resolve()])
        self.assertEqual(registered[0].keys, self.base / "keys")

    def test_an_existing_ops_key_is_reused_not_regenerated(self) -> None:
        (self.base / "ssh").mkdir()
        (self.base / "ssh/example-ops").write_text("mine", encoding="utf-8")
        (self.base / "ssh/example-ops.pub").write_text(
            "ssh-ed25519 AAAAC3mine ops:me\n", encoding="utf-8"
        )

        result = write_company(
            self.root, self.answers, runner=fake_runner, home=self.home
        )

        self.assertEqual(result.operator_public_key, "ssh-ed25519 AAAAC3mine ops:me")
        self.assertEqual((self.base / "ssh/example-ops").read_text(), "mine")

    def test_a_directory_that_is_already_an_infrastructure_is_refused(self) -> None:
        (self.root / "inventory").mkdir(parents=True)
        with self.assertRaises(CompanyError):
            write_company(self.root, self.answers, runner=fake_runner, home=self.home)
        self.assertFalse((self.root / "requirements.yml").exists())

    def test_existing_keys_stop_everything_before_a_file_is_written(self) -> None:
        (self.base / "keys").mkdir()
        (self.base / "keys/recovery.agekey").write_text("old", encoding="utf-8")
        with self.assertRaises(CompanyError):
            write_company(self.root, self.answers, runner=fake_runner, home=self.home)
        self.assertFalse((self.root / "requirements.yml").exists())


class TemplatesMatchHandbookTest(unittest.TestCase):
    """The handbook shows these files on #/ref-layout; the package writes them."""

    def folds(self) -> dict:
        text = HANDBOOK.read_text(encoding="utf-8")
        start = text.index('id="ref-layout"')
        end = text.index("<section", start + 10)
        found = {}
        for m in re.finditer(
            r'<details class="fold"><summary>(.*?)</summary>.*?<pre[^>]*><code>(.*?)</code></pre>',
            text[start:end],
            re.S,
        ):
            name = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
            found[name] = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).rstrip("\n")
        return found

    def test_whole_file_templates_equal_their_handbook_fold(self) -> None:
        folds = self.folds()
        values = {
            "core_pin": f"v{__version__}",
            "core_version": __version__,
            "keydir": "{{keydir}}",
        }
        for fold, name in (
            ("requirements.yml", "requirements.yml"),
            (".gitignore", "gitignore"),
            ("inventory/group_vars/all/local-secrets.yml", "all_local_secrets.yml"),
        ):
            self.assertIn(fold, folds)
            self.assertEqual(
                folds[fold], render(template(name), values).rstrip("\n"), fold
            )
