import json
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles" / "observability"
BUNDLE = ROLE / "files" / "bundle"
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))
DIGEST = re.compile(r"^[^\s@]+:[^\s@]+@sha256:[0-9a-f]{64}$")


def compose(name: str) -> dict:
    return yaml.safe_load((BUNDLE / name).read_text(encoding="utf-8"))


class ObservabilityBundleTest(unittest.TestCase):
    def test_the_role_installs_exactly_the_files_the_bundle_has(self) -> None:
        present = {str(p.relative_to(BUNDLE)) for p in BUNDLE.rglob("*") if p.is_file()}
        self.assertEqual(set(DEFAULTS["observability_bundle_files"]), present)
        for relative in present:
            parent = str(Path(relative).parent)
            if parent != ".":
                self.assertIn(
                    parent, DEFAULTS["observability_bundle_directories"], relative
                )

    def test_it_is_off_until_the_inventory_turns_it_on(self) -> None:
        self.assertIs(DEFAULTS["observability_enabled"], False)
        self.assertEqual(DEFAULTS["observability_grafana_admin_password_source"], "")

    def test_nothing_is_published_and_every_image_is_pinned(self) -> None:
        for name in ("backend.yml", "collector.yml"):
            for service, spec in compose(name)["services"].items():
                self.assertNotIn(
                    "ports", spec, f"{name}: {service} publishes a host port"
                )
                self.assertNotEqual(spec.get("network_mode"), "host", service)
                self.assertNotIn("privileged", spec, service)
                self.assertRegex(spec["image"], DIGEST, f"{name}: {service}")
                self.assertIn("no-new-privileges:true", spec["security_opt"], service)

    def test_only_the_socket_proxy_touches_the_docker_socket_and_only_to_read(
        self,
    ) -> None:
        holders = [
            service
            for name in ("backend.yml", "collector.yml")
            for service, spec in compose(name)["services"].items()
            if any(
                "docker.sock" in str(v) or "containerd" in str(v)
                for v in spec.get("volumes", [])
            )
        ]
        self.assertEqual(holders, ["docker-socket-collector"])
        policy = (BUNDLE / "socket-proxy/collector.cfg").read_text(encoding="utf-8")
        self.assertIn("acl read_method method GET HEAD", policy)
        allows = [line for line in policy.splitlines() if "http-request allow" in line]
        self.assertTrue(
            allows and all("read_method" in line for line in allows), allows
        )
        self.assertIn("http-request deny", policy)
        for verb in ("exec", "start", "stop", "kill", "create", "delete"):
            self.assertNotRegex(policy, rf"containers/[^\n]*/{verb}\b")

    def test_the_collector_writes_by_url_so_the_backend_can_move(self) -> None:
        alloy = compose("collector.yml")["services"]["alloy"]["environment"]
        self.assertIn("OBSERVABILITY_LOKI_PUSH_URL", alloy["LOKI_PUSH_URL"])
        self.assertIn(
            "OBSERVABILITY_PROMETHEUS_WRITE_URL", alloy["PROMETHEUS_WRITE_URL"]
        )
        config = (BUNDLE / "alloy/config.alloy").read_text(encoding="utf-8")
        self.assertIn('sys.env("LOKI_PUSH_URL")', config)
        self.assertIn('sys.env("PROMETHEUS_WRITE_URL")', config)
        self.assertNotIn("cadvisor", config)
        self.assertNotIn("http://loki", config)

    def test_dashboards_use_the_provisioned_sources(self) -> None:
        sources = yaml.safe_load(
            (BUNDLE / "grafana/provisioning/datasources/platform.yaml").read_text(
                encoding="utf-8"
            )
        )["datasources"]
        uids = {source["uid"] for source in sources}
        self.assertTrue(all(source["editable"] is False for source in sources))
        for path in (BUNDLE / "grafana/dashboards").glob("*.json"):
            board = json.loads(path.read_text(encoding="utf-8"))
            self.assertIs(board["editable"], False, path.name)
            for panel in board["panels"]:
                self.assertIn(
                    panel["datasource"]["uid"], uids, f"{path.name}: {panel['title']}"
                )

    def test_state_directories_belong_to_the_users_the_processes_run_as(self) -> None:
        owners = {
            item["name"]: item["owner"]
            for item in DEFAULTS["observability_state_directories"]
        }
        backend = compose("backend.yml")["services"]
        for service in ("loki", "prometheus", "grafana"):
            self.assertEqual(
                backend[service]["user"].split(":")[0], owners[service], service
            )

    def test_converge_runs_the_role_after_the_proxy(self) -> None:
        roles = [
            entry["role"]
            for entry in yaml.safe_load(
                (ROOT / "playbooks/converge.yml").read_text(encoding="utf-8")
            )[0]["roles"]
        ]
        self.assertGreater(
            roles.index("otostogan.platform.observability"),
            roles.index("otostogan.platform.proxy"),
        )
