import tempfile
import unittest
from pathlib import Path

from platform_automation.operator.context import detect


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


INVENTORY = """\
all:
  children:
    platform_hosts:
      hosts:
        platform-host-1:
          ansible_host: platform-host-1.tailnet.example.net
          ansible_user: ops
        platform-host-2:
          ansible_host: platform-host-2.tailnet.example.net
          ansible_user: ops
"""

REQUIREMENTS = """\
collections:
  - name: >-
      https://github.com/otostogan/platform-automation-core/releases/download/v0.14.0/otostogan-platform-0.14.0.tar.gz
    type: url
"""

MANIFEST = """\
api_version: platform/v1
project: my-app
environment: {environment}
compose_file: deploy/compose.yml
"""

DEPLOY_WORKFLOW = """\
jobs:
  deploy:
    uses: otostogan/platform-automation-core/.github/workflows/reusable-deploy.yml@v0.14.0
    with:
      target_host: platform-host-1.tailnet.example.net
      tailscale_tag: tag:ci-my-app
"""


class DetectContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.no_host = self.root / "no-such-host-marker"

    def test_infrastructure_repository_lists_hosts_and_pin(self) -> None:
        write(self.root / "inventory/hosts.yml", INVENTORY)
        write(self.root / "requirements.yml", REQUIREMENTS)

        context = detect(self.root / "inventory/group_vars", host_marker=self.no_host)

        self.assertEqual(context.kind, "infra")
        self.assertEqual(context.root, self.root.resolve())
        self.assertEqual(
            [host.name for host in context.hosts],
            ["platform-host-1", "platform-host-2"],
        )
        self.assertEqual(
            context.hosts[0].address, "platform-host-1.tailnet.example.net"
        )
        self.assertEqual(context.hosts[0].user, "ops")
        self.assertEqual(context.core_pin, "v0.14.0")

    def test_application_repository_reads_manifests_and_workflow(self) -> None:
        write(self.root / "deploy/platform.lab.yml", MANIFEST.format(environment="lab"))
        write(
            self.root / "deploy/platform.production.yml",
            MANIFEST.format(environment="production"),
        )
        write(self.root / ".github/workflows/deploy.yml", DEPLOY_WORKFLOW)

        context = detect(self.root / "deploy", host_marker=self.no_host)

        self.assertEqual(context.kind, "app")
        self.assertEqual(
            [(item.project, item.environment) for item in context.environments],
            [("my-app", "lab"), ("my-app", "production")],
        )
        self.assertEqual(context.target_host, "platform-host-1.tailnet.example.net")
        self.assertEqual(context.tailscale_tag, "tag:ci-my-app")
        self.assertEqual(context.core_pin, "v0.14.0")

    def test_manifest_without_contract_is_not_an_application(self) -> None:
        write(self.root / "deploy/platform.lab.yml", "project: something\n")

        context = detect(self.root, host_marker=self.no_host)

        self.assertEqual(context.kind, "nowhere")

    def test_requirements_without_the_collection_is_not_infrastructure(self) -> None:
        write(self.root / "inventory/hosts.yml", INVENTORY)
        write(self.root / "requirements.yml", "collections: []\n")

        context = detect(self.root, host_marker=self.no_host)

        self.assertEqual(context.kind, "nowhere")

    def test_host_marker_wins_over_everything(self) -> None:
        marker = self.root / "projects"
        marker.mkdir()
        write(self.root / "inventory/hosts.yml", INVENTORY)
        write(self.root / "requirements.yml", REQUIREMENTS)

        context = detect(self.root, host_marker=marker)

        self.assertEqual(context.kind, "host")


if __name__ == "__main__":
    unittest.main()
