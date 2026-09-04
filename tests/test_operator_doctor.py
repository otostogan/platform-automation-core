import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from platform_automation.operator.context import detect
from platform_automation.operator.doctor import diagnose, version_satisfies
from platform_automation.operator.tailnet import parse_status

FIXTURE_APP = Path(__file__).parent / "fixtures" / "app-contract"

TAILNET = parse_status(
    {
        "BackendState": "Running",
        "MagicDNSSuffix": "tailnet.example.net",
        "Self": {"DNSName": "laptop.tailnet.example.net."},
        "Peer": {
            "n1": {
                "DNSName": "platform-host-1.tailnet.example.net.",
                "HostName": "platform-host-1",
                "Online": True,
                "Tags": ["tag:server-platform"],
            }
        },
    }
)
STOPPED = parse_status({"BackendState": "Stopped"})


def write(path: Path, text: str, mode: int = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        os.chmod(path, mode)
    return path


def runner_for(
    age_output=b"age1syntheticfixture\n", ansible_output=b"ansible [core 2.15.13]\n"
):
    def runner(command, **kwargs):
        name = Path(command[0]).name
        if name == "age-keygen":
            return subprocess.CompletedProcess(command, 0, age_output, b"")
        if name == "ansible":
            return subprocess.CompletedProcess(command, 0, ansible_output, b"")
        raise AssertionError(f"unexpected command {command}")

    return runner


def by_title(findings, prefix):
    return [f for f in findings if f.title.startswith(prefix)]


class VersionSatisfiesTest(unittest.TestCase):
    def test_range(self) -> None:
        self.assertTrue(version_satisfies("2.15.13", ">=2.15.13,<2.16.0"))
        self.assertFalse(version_satisfies("2.16.0", ">=2.15.13,<2.16.0"))
        self.assertFalse(version_satisfies("2.15.12", ">=2.15.13,<2.16.0"))
        self.assertIsNone(version_satisfies("2.15.13", "~=2.15"))


class InfraDoctorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root = base / "infra"
        self.home = base / "home"
        self.collections = base / "collections"
        self.marker = base / "no-host"

        ssh_key = write(self.home / "keys/platform-host-1-ops", "ssh", 0o600)
        age_key = write(
            self.home / "keys/platform-host-1.agekey",
            "synthetic private half",
            0o600,
        )
        write(
            self.root / "inventory/hosts.yml",
            "all:\n  children:\n    platform_hosts:\n      hosts:\n        platform-host-1:\n"
            "          ansible_host: platform-host-1.tailnet.example.net\n"
            "          ansible_user: ops\n"
            f"          ansible_ssh_private_key_file: {ssh_key}\n",
        )
        write(
            self.root / "requirements.yml",
            "collections:\n  - name: https://github.com/otostogan/platform-automation-core/"
            "releases/download/v0.14.0/otostogan-platform-0.14.0.tar.gz\n    type: url\n",
        )
        write(
            self.root / "inventory/host_vars/platform-host-1/local-secrets.yml",
            f"secrets_age_key_source: {age_key}\n",
        )
        write(self.root / ".venv/bin/ansible", "#!/bin/sh\n", 0o755)
        collection = self.collections / "ansible_collections/otostogan/platform"
        write(
            collection / "MANIFEST.json",
            json.dumps({"collection_info": {"version": "0.14.0"}}),
        )
        write(
            collection / "meta/runtime.yml", 'requires_ansible: ">=2.15.13,<2.16.0"\n'
        )

    def diagnose(self, tailnet=TAILNET, runner=None):
        context = detect(self.root, host_marker=self.marker)
        self.assertEqual(context.kind, "infra")
        return diagnose(
            context,
            tailnet,
            runner=runner or runner_for(),
            home=self.home,
            collections_root=self.collections,
        )

    def test_healthy_infrastructure_passes_everything(self) -> None:
        findings = self.diagnose()

        failed = [f for f in findings if f.failed]
        self.assertEqual(failed, [], [f"{f.title}: {f.detail}" for f in failed])
        self.assertIn(
            "v0.14.0 installed, matches",
            by_title(findings, "Core collection")[0].detail,
        )
        self.assertIn("age1", by_title(findings, "platform-host-1: age key")[0].detail)

    def test_stopped_tailscale_fails_and_skips_host_reachability(self) -> None:
        findings = self.diagnose(tailnet=STOPPED)

        self.assertEqual(by_title(findings, "Tailscale")[0].status, "fail")
        self.assertEqual(
            by_title(findings, "platform-host-1: tailnet")[0].status, "skip"
        )

    def test_pin_drift_names_both_versions(self) -> None:
        collection = self.collections / "ansible_collections/otostogan/platform"
        write(
            collection / "MANIFEST.json",
            json.dumps({"collection_info": {"version": "0.13.3"}}),
        )

        finding = by_title(self.diagnose(), "Core collection")[0]

        self.assertTrue(finding.failed)
        self.assertIn(
            "v0.13.3 installed but requirements.yml pins v0.14.0", finding.detail
        )
        self.assertEqual(finding.anchor, "#/flow-core-update")

    def test_loose_key_permissions_fail_with_the_mode(self) -> None:
        os.chmod(self.home / "keys/platform-host-1.agekey", 0o644)

        finding = by_title(self.diagnose(), "platform-host-1: age key")[0]

        self.assertTrue(finding.failed)
        self.assertIn("0644", finding.detail)
        self.assertEqual(finding.anchor, "#/ref-keys")

    def test_wrong_ansible_user_is_the_convergence_refusal(self) -> None:
        write(
            self.root / "inventory/hosts.yml",
            "all:\n  children:\n    platform_hosts:\n      hosts:\n        platform-host-1:\n"
            "          ansible_host: platform-host-1.tailnet.example.net\n"
            "          ansible_user: root\n",
        )

        finding = by_title(self.diagnose(), "platform-host-1: ansible_user")[0]

        self.assertTrue(finding.failed)
        self.assertEqual(finding.anchor, "#/flow-incidents")

    def test_old_ansible_core_is_outside_the_requirement(self) -> None:
        finding = by_title(
            self.diagnose(runner=runner_for(ansible_output=b"ansible [core 2.14.2]\n")),
            "ansible-core",
        )[0]

        self.assertTrue(finding.failed)
        self.assertIn("outside", finding.detail)


class AppDoctorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root = base / "app"
        self.home = base / "home"
        self.marker = base / "no-host"
        shutil.copytree(FIXTURE_APP, self.root)
        write(
            self.root / ".github/workflows/deploy.yml",
            "jobs:\n  deploy:\n    uses: otostogan/platform-automation-core/.github/workflows/"
            "reusable-deploy.yml@v0.14.0\n    with:\n"
            "      target_host: platform-host-1.tailnet.example.net\n"
            "      tailscale_tag: tag:ci-example\n",
        )

    def diagnose(self, tailnet=TAILNET):
        context = detect(self.root, host_marker=self.marker)
        self.assertEqual(context.kind, "app")
        return diagnose(context, tailnet, home=self.home)

    def test_fixture_application_is_healthy_except_the_unknown_infra_pin(self) -> None:
        findings = self.diagnose()

        failed = [f for f in findings if f.failed]
        self.assertEqual(failed, [], [f"{f.title}: {f.detail}" for f in failed])
        self.assertIn(
            "2 recipient(s)",
            by_title(findings, "deploy/platform.yml: secrets")[0].detail,
        )
        self.assertEqual(by_title(findings, "Core pin")[0].status, "skip")
        self.assertIn("platform infra add", by_title(findings, "Core pin")[0].detail)

    def register_infra(self, pin="v0.13.3", recipients=None) -> Path:
        infra = self.home / "platform-infra"
        write(
            infra / "requirements.yml",
            "collections:\n  - name: https://github.com/otostogan/platform-automation-core/"
            f"releases/download/{pin}/otostogan-platform-{pin[1:]}.tar.gz\n    type: url\n",
        )
        write(
            infra / "inventory/hosts.yml",
            "all:\n  children:\n    platform_hosts:\n      hosts:\n        platform-host-1:\n"
            "          ansible_host: platform-host-1.tailnet.example.net\n          ansible_user: ops\n",
        )
        if recipients is not None:
            write(infra / "docs/RECIPIENTS.md", "```\n" + recipients + "```\n")
        write(
            self.home / ".config/platform/config.yml", f"infras:\n  - path: {infra}\n"
        )
        return infra

    def test_templates_are_checked_without_a_registered_infrastructure(self) -> None:
        write(
            self.root / ".sops.yaml",
            "creation_rules:\n  - key_groups:\n      - age:\n"
            "          - age1syntheticfixture\n          - age1recoveryfixture\n",
        )
        write(
            self.root / ".github/workflows/build.yml",
            "jobs:\n  build:\n    env:\n      REPOSITORY: ghcr.io/example/example\n",
        )
        findings = self.diagnose()
        self.assertEqual(by_title(findings, "Core pin")[0].status, "skip")
        finding = by_title(findings, "Templates")[0]
        self.assertTrue(finding.failed, finding.detail)
        self.assertIn("platform update", finding.detail)

    def test_templates_behind_the_console_are_reported(self) -> None:
        from platform_automation.operator.scaffold import strip_marker

        self.register_infra(
            pin="v0.14.0",
            recipients="host age1syntheticfixture\nrecovery age1recoveryfixture\n",
        )
        write(
            self.root / ".github/workflows/build.yml",
            "jobs:\n  build:\n    env:\n      REPOSITORY: ghcr.io/example/example\n",
        )
        finding = by_title(self.diagnose(), "Templates")[0]
        self.assertTrue(finding.failed, finding.detail)
        self.assertIn("platform update", finding.detail)

        from platform_automation.operator.update import (
            apply,
            gather_facts,
            plan,
            render_managed,
        )

        context = detect(self.root, host_marker=self.marker)
        apply(
            self.root, plan(self.root, render_managed(gather_facts(context, self.home)))
        )
        finding = by_title(self.diagnose(), "Templates")[0]
        self.assertEqual(finding.status, "ok", finding.detail)

        hook = self.root / ".githooks/pre-push"
        hook.write_text(
            strip_marker(hook.read_text()) + "echo mine\n", encoding="utf-8"
        )
        finding = by_title(self.diagnose(), "Templates")[0]
        self.assertEqual(finding.status, "ok")
        self.assertIn("owned by the application", finding.detail)
        self.assertIn(".githooks/pre-push", finding.detail)

    def test_registered_infrastructure_is_found_by_the_target_host(self) -> None:
        self.register_infra(pin="v0.13.3")

        finding = by_title(self.diagnose(), "Core pin")[0]

        self.assertTrue(finding.failed)
        self.assertIn(
            "application uses v0.14.0, platform-infra pins v0.13.3", finding.detail
        )

    def declare_recipients(self, *recipients) -> None:
        lines = "".join(f"                - {r}\n" for r in recipients)
        write(
            self.root / ".sops.yaml",
            "creation_rules:\n    - path_regex: deploy/secrets\\..*\\.sops\\.ya?ml$\n"
            "      key_groups:\n          - age:\n" + lines,
        )

    def test_recipients_are_compared_with_what_the_infrastructure_publishes(
        self,
    ) -> None:
        self.declare_recipients("age1syntheticfixture", "age1recoveryfixture")
        self.register_infra(
            pin="v0.14.0",
            recipients="host      age1syntheticfixture\nrecovery  age1recoveryfixture\n",
        )

        findings = self.diagnose()

        self.assertEqual(by_title(findings, "Core pin")[0].status, "ok")
        self.assertEqual(by_title(findings, "Recipients")[0].status, "ok")

    def test_a_missing_published_recipient_is_a_failure(self) -> None:
        self.declare_recipients("age1syntheticfixture", "age1recoveryfixture")
        self.register_infra(
            pin="v0.14.0",
            recipients="host      age1syntheticfixture\nrecovery  age1othersynthetic\n",
        )

        finding = by_title(self.diagnose(), "Recipients")[0]

        self.assertTrue(finding.failed)
        self.assertIn("lacks 1 recipient", finding.detail)

    def test_broken_compose_contract_is_reported_with_the_field(self) -> None:
        compose = self.root / "deploy/compose.yml"
        compose.write_text(
            compose.read_text(encoding="utf-8").replace("platform-edge", "my-edge"),
            encoding="utf-8",
        )

        finding = by_title(self.diagnose(), "deploy/platform.yml: compose")[0]

        self.assertTrue(finding.failed)
        self.assertEqual(finding.anchor, "#/ref-compose")

    def test_offline_target_host_fails(self) -> None:
        offline = parse_status(
            {
                "BackendState": "Running",
                "Peer": {
                    "n1": {
                        "DNSName": "platform-host-1.tailnet.example.net.",
                        "Online": False,
                    }
                },
            }
        )

        finding = by_title(self.diagnose(tailnet=offline), "Target host")[0]

        self.assertTrue(finding.failed)
        self.assertIn("offline", finding.detail)


if __name__ == "__main__":
    unittest.main()
