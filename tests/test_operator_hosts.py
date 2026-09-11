import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from platform_automation.operator.hosts import (
    HostAnswers,
    HostError,
    add_recipient,
    checked_insert,
    defaults_from,
    generate_age_key,
    insert_host,
    plan_host,
    validate_host_answers,
    write_host,
)

HOSTS = """---
# Steady-state inventory. Reached over the tailnet as the ops user.
all:
    children:
        platform_hosts:
            hosts:
                platform-host-1:
                    # Tailscale MagicDNS name, not a public address.
                    ansible_host: platform-host-1.tailnet.example.net
                    ansible_user: ops
                    ansible_ssh_private_key_file: ~/.ssh/platform-host-1-ops
                    ansible_port: 22
                    platform_public_interface: eth0

# trailing comment stays where it was
"""

BOOTSTRAP = """---
all:
  children:
    platform_hosts:
      hosts:
        platform-host-1:
          ansible_host: 203.0.113.10
          ansible_user: root
"""

RECIPIENTS = """# SOPS recipients

```
platform-host-1  age1syntheticfixture
recovery         age1recoveryfixture
```

## Why two
"""

ENTRY = "platform-host-2:\n    ansible_host: 203.0.113.11\n    ansible_user: root\n"

ANSWERS = HostAnswers(
    name="platform-host-2",
    public_address="203.0.113.11",
    tailnet="platform-host-2.tailnet.example.net",
    ssh_key="~/.ssh/platform-host-2-ops",
    keys_dir="~/.config/platform-keys/example",
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def fake_keygen(command, **_):
    """Writes a synthetic private half and prints a recipient like age-keygen."""
    path = Path(command[command.index("--output") + 1])
    path.write_text("# synthetic private half\n", encoding="utf-8")
    stamp = path.stem.replace("-", "")[:12]
    return subprocess.CompletedProcess(
        command, 0, b"", f"Public key: age1{stamp}fixture\n".encode()
    )


class InsertTest(unittest.TestCase):
    def test_entry_lands_after_the_last_host_with_the_file_indentation(self) -> None:
        text = insert_host(HOSTS, ENTRY)

        self.assertIn(
            "                    platform_public_interface: eth0\n"
            "                platform-host-2:\n"
            "                    ansible_host: 203.0.113.11\n",
            text,
        )
        self.assertTrue(text.endswith("# trailing comment stays where it was\n"))
        self.assertIn("# Tailscale MagicDNS name", text, "comments survive")

    def test_two_space_files_get_two_space_entries(self) -> None:
        text = insert_host(BOOTSTRAP, ENTRY)

        self.assertIn(
            "          ansible_user: root\n        platform-host-2:\n          ansible_host: 203.0.113.11\n",
            text,
        )
        self.assertEqual(
            set(yaml.safe_load(text)["all"]["children"]["platform_hosts"]["hosts"]),
            {"platform-host-1", "platform-host-2"},
        )

    def test_empty_hosts_mapping_derives_its_step_from_the_file(self) -> None:
        text = insert_host(
            "all:\n  children:\n    platform_hosts:\n      hosts:\n", ENTRY
        )

        self.assertEqual(
            yaml.safe_load(text)["all"]["children"]["platform_hosts"]["hosts"][
                "platform-host-2"
            ]["ansible_user"],
            "root",
        )

    def test_a_file_without_the_group_is_refused(self) -> None:
        with self.assertRaises(HostError):
            insert_host("all:\n  hosts:\n    x: {}\n", ENTRY)

    def test_checked_insert_refuses_a_duplicate_and_proves_the_others_unchanged(
        self,
    ) -> None:
        with self.assertRaises(HostError):
            checked_insert(
                HOSTS, "platform-host-1:\n    ansible_user: root\n", "platform-host-1"
            )

        text = checked_insert(HOSTS, ENTRY, "platform-host-2")
        hosts = yaml.safe_load(text)["all"]["children"]["platform_hosts"]["hosts"]
        self.assertEqual(hosts["platform-host-1"]["ansible_user"], "ops")


class RecipientsTest(unittest.TestCase):
    def test_line_is_appended_inside_the_fence_and_aligned(self) -> None:
        text = add_recipient(RECIPIENTS, "platform-host-2", "age1othersynthetic")

        self.assertIn(
            "platform-host-1  age1syntheticfixture\nrecovery         age1recoveryfixture\n"
            "platform-host-2  age1othersynthetic\n```",
            text,
        )

    def test_without_a_fence_it_refuses(self) -> None:
        with self.assertRaises(HostError):
            add_recipient("# nothing here\n", "x", "age1othersynthetic")


class PlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "infra"
        self.home = Path(self.temporary.name) / "home"
        write(self.root / "inventory/hosts.yml", HOSTS)
        write(self.root / "inventory/bootstrap.yml", BOOTSTRAP)
        write(self.root / "docs/RECIPIENTS.md", RECIPIENTS)
        write(
            self.root / "inventory/host_vars/platform-host-1/local-secrets.yml",
            "secrets_age_key_source: ~/.config/platform-keys/example/platform-host-1.agekey\n",
        )
        self.answers = HostAnswers(
            **{**ANSWERS.__dict__, "keys_dir": str(self.home / "keys")}
        )

    def test_validation_names_every_bad_answer(self) -> None:
        bad = HostAnswers(
            name="Host_1",
            public_address="not an address",
            tailnet="nodots",
            ssh_key="",
            keys_dir="",
        )
        errors = validate_host_answers(bad)
        self.assertEqual(len(errors), 5, errors)
        self.assertEqual(validate_host_answers(ANSWERS), [])

    def test_defaults_come_from_the_existing_host(self) -> None:
        found = defaults_from(self.root)

        self.assertEqual(found["keys_dir"], "~/.config/platform-keys/example")
        self.assertEqual(found["ssh_key"], "~/.ssh/platform-host-1-ops")
        self.assertEqual(found["suffix"], "tailnet.example.net")
        self.assertEqual(found["interface"], "eth0")

    def test_plan_touches_both_inventories_and_host_vars_only(self) -> None:
        plan = plan_host(self.root, self.answers)

        self.assertEqual(
            sorted(plan.files),
            [
                "inventory/bootstrap.yml",
                "inventory/host_vars/platform-host-2/local-secrets.yml",
                "inventory/host_vars/platform-host-2/local-secrets.yml.example",
                "inventory/hosts.yml",
            ],
        )
        self.assertEqual([label for label, _ in plan.keys], ["platform-host-2"])
        self.assertIn(
            "platform-host-2.tailnet.example.net", plan.files["inventory/hosts.yml"]
        )
        self.assertIn("203.0.113.11", plan.files["inventory/bootstrap.yml"])
        self.assertNotIn("offsite", plan.files["inventory/hosts.yml"])

    def test_offsite_adds_the_prefix_and_the_credentials_path(self) -> None:
        write(
            self.root / "inventory/group_vars/platform_hosts.yml",
            "platform_cli_offsite_enabled: true\n",
        )
        self.answers.offsite = True
        plan = plan_host(self.root, self.answers)

        self.assertIn(
            "platform_cli_offsite_prefix: platform-host-2",
            plan.files["inventory/hosts.yml"],
        )
        self.assertIn(
            "platform_cli_offsite_credentials_source",
            plan.files["inventory/host_vars/platform-host-2/local-secrets.yml"],
        )

    def test_nothing_is_written_when_anything_clashes(self) -> None:
        write(
            self.root / "inventory/host_vars/platform-host-2/local-secrets.yml",
            "x: 1\n",
        )
        with self.assertRaises(HostError):
            plan_host(self.root, self.answers)
        self.assertNotIn(
            "platform-host-2", (self.root / "inventory/hosts.yml").read_text()
        )

        (self.root / "inventory/host_vars/platform-host-2/local-secrets.yml").unlink()
        write(
            self.home / "keys/platform-host-2.agekey",
            "old",
        )
        with self.assertRaises(HostError):
            plan_host(self.root, self.answers)

    def test_recovery_is_required_unless_generated(self) -> None:
        write(
            self.root / "docs/RECIPIENTS.md",
            "```\nplatform-host-1  age1syntheticfixture\n```\n",
        )
        with self.assertRaises(HostError):
            plan_host(self.root, self.answers)
        self.answers.recovery_needed = True
        plan = plan_host(self.root, self.answers)
        self.assertEqual(
            [label for label, _ in plan.keys], ["platform-host-2", "recovery"]
        )

    def test_write_generates_the_key_publishes_it_and_edits_every_file(self) -> None:
        plan = plan_host(self.root, self.answers)

        recipients = write_host(self.root, self.answers, plan, runner=fake_keygen)

        self.assertEqual(recipients, {"platform-host-2": "age1platformhostfixture"})
        key = self.home / "keys/platform-host-2.agekey"
        self.assertTrue(key.is_file())
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertEqual(key.parent.stat().st_mode & 0o777, 0o700)
        published = (self.root / "docs/RECIPIENTS.md").read_text(encoding="utf-8")
        self.assertIn("platform-host-2  age1platformhostfixture\n```", published)
        self.assertIn("recovery         age1recoveryfixture", published)
        hosts = yaml.safe_load((self.root / "inventory/hosts.yml").read_text())
        self.assertEqual(
            hosts["all"]["children"]["platform_hosts"]["hosts"]["platform-host-2"][
                "ansible_user"
            ],
            "ops",
        )
        secrets = (
            self.root / "inventory/host_vars/platform-host-2/local-secrets.yml"
        ).read_text()
        self.assertIn(
            f"secrets_age_key_source: {self.home}/keys/platform-host-2.agekey", secrets
        )

    def test_missing_recipients_file_is_created_from_the_template(self) -> None:
        (self.root / "docs/RECIPIENTS.md").unlink()
        self.answers.recovery_needed = True
        plan = plan_host(self.root, self.answers)
        self.assertTrue(plan.recipients_missing)

        recipients = write_host(self.root, self.answers, plan, runner=fake_keygen)

        published = (self.root / "docs/RECIPIENTS.md").read_text(encoding="utf-8")
        self.assertIn(f"platform-host-2  {recipients['platform-host-2']}", published)
        self.assertIn(f"recovery  {recipients['recovery']}", published)

    def test_keygen_refuses_to_overwrite(self) -> None:
        write(self.home / "keys/x.agekey", "old")
        with self.assertRaises(HostError):
            generate_age_key(self.home / "keys/x.agekey", runner=fake_keygen)
