import tempfile
import unittest
from pathlib import Path

from platform_automation.operator.config import (
    config_path,
    forget_infra,
    infra_for_host,
    infras,
    register_infra,
)
from platform_automation.operator.recipients import (
    host_recipient,
    read_recipients,
    recovery_recipient,
)

INVENTORY = """\
all:
  children:
    platform_hosts:
      hosts:
        platform-host-1:
          ansible_host: platform-host-1.tailnet.example.net
          ansible_user: ops
"""

RECIPIENTS = """\
# SOPS recipients

Public keys, committed on purpose.

```
host      age1syntheticfixture
recovery  age1recoveryfixture
```

## Onboarding an application

```yaml
creation_rules:
    - path_regex: deploy/secrets\\..*\\.sops\\.ya?ml$
```
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "home"
        self.infra = Path(self.temporary.name) / "platform-infra"
        write(self.infra / "inventory/hosts.yml", INVENTORY)

    def test_register_is_idempotent_and_survives_a_reload(self) -> None:
        self.assertTrue(register_infra(self.infra, home=self.home))
        self.assertFalse(register_infra(self.infra, home=self.home))

        known = infras(self.home)

        self.assertEqual([i.path for i in known], [self.infra.resolve()])
        self.assertEqual(known[0].name, "platform-infra")
        self.assertTrue(config_path(self.home).is_file())

    def test_keys_directory_is_remembered_once_known(self) -> None:
        register_infra(self.infra, home=self.home)
        register_infra(self.infra, keys=self.home / "keys", home=self.home)

        self.assertEqual(infras(self.home)[0].keys, self.home / "keys")

    def test_legacy_single_infra_key_still_counts(self) -> None:
        write(self.home / ".config/platform/config.yml", f"infra: {self.infra}\n")

        self.assertEqual([i.path for i in infras(self.home)], [self.infra])

    def test_forget_removes_only_that_path(self) -> None:
        other = Path(self.temporary.name) / "other-infra"
        write(other / "inventory/hosts.yml", INVENTORY)
        register_infra(self.infra, home=self.home)
        register_infra(other, home=self.home)

        self.assertTrue(forget_infra(self.infra, home=self.home))
        self.assertFalse(forget_infra(self.infra, home=self.home))
        self.assertEqual([i.path for i in infras(self.home)], [other.resolve()])

    def test_infra_is_found_by_host_name_or_address(self) -> None:
        register_infra(self.infra, home=self.home)

        by_name = infra_for_host("platform-host-1", self.home)
        by_address = infra_for_host("platform-host-1.tailnet.example.net.", self.home)

        self.assertEqual(by_name.path, self.infra.resolve())
        self.assertEqual(by_address.path, self.infra.resolve())
        self.assertIsNone(infra_for_host("platform-host-9", self.home))


class RecipientsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.infra = Path(self.temporary.name)

    def test_first_code_block_gives_both_recipients(self) -> None:
        write(self.infra / "docs/RECIPIENTS.md", RECIPIENTS)

        published = read_recipients(self.infra)

        self.assertEqual(
            host_recipient(published, "platform-host-1"), "age1syntheticfixture"
        )
        self.assertEqual(recovery_recipient(published), "age1recoveryfixture")

    def test_per_host_line_wins_over_the_generic_one(self) -> None:
        write(
            self.infra / "docs/RECIPIENTS.md",
            "```\nhost age1syntheticfixture\nplatform-host-2 age1secondhostfixture\nrecovery age1recoveryfixture\n```\n",
        )

        published = read_recipients(self.infra)

        self.assertEqual(
            host_recipient(published, "platform-host-2"), "age1secondhostfixture"
        )
        self.assertEqual(
            host_recipient(published, "platform-host-1"), "age1syntheticfixture"
        )

    def test_missing_file_is_empty_not_an_error(self) -> None:
        self.assertEqual(read_recipients(self.infra), {})


if __name__ == "__main__":
    unittest.main()
