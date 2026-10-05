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

    def test_the_collector_reads_application_metrics_from_platform_written_targets(
        self,
    ) -> None:
        collector = compose("collector.yml")
        alloy = collector["services"]["alloy"]
        # the proxy's network is the one every web service is on
        self.assertIn("edge", alloy["networks"])
        edge = collector["networks"]["edge"]
        self.assertIs(edge["external"], True)
        self.assertEqual(edge["name"], "${OBSERVABILITY_EDGE_NETWORK:-platform-edge}")
        self.assertEqual(DEFAULTS["observability_edge_network"], "platform-edge")
        mounts = [m for m in alloy["volumes"] if m.endswith(":/host/targets:ro")]
        self.assertEqual(len(mounts), 1, alloy["volumes"])

        config = (BUNDLE / "alloy/config.alloy").read_text(encoding="utf-8")
        self.assertIn('files = ["/host/targets/*.json"]', config)
        self.assertIn('job_name        = "application"', config)
        self.assertIn("sample_limit", config)
        # the host is stamped on the target; an external label would yield
        # to a "host" the application's own series carried
        self.assertRegex(
            config,
            r'target_label = "host"\s+replacement  = sys\.env\("PLATFORM_HOST"\)',
        )
        # an application must not be able to write the names alerts read
        self.assertIn('regex         = "(platform|node)_.*"', config)
        self.assertIn('action        = "drop"', config)

        unit = (ROLE / "templates/platform-metrics.service.j2").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "--targets {{ observability_state_directory }}/targets/applications.json",
            unit,
        )
        self.assertIn(
            "{{ observability_state_directory }}/targets",
            unit.split("ReadWritePaths=")[1],
        )
        owners = {i["name"] for i in DEFAULTS["observability_state_directories"]}
        self.assertIn("targets", owners)

    def test_grafana_reads_the_dashboards_applications_ship(self) -> None:
        grafana = compose("backend.yml")["services"]["grafana"]
        mounts = [
            m
            for m in grafana["volumes"]
            if m.endswith(":/etc/grafana/application-dashboards:ro")
        ]
        self.assertEqual(len(mounts), 1, grafana["volumes"])
        self.assertIn("/dashboards:", mounts[0])

        providers = {
            provider["name"]: provider
            for provider in yaml.safe_load(
                (BUNDLE / "grafana/provisioning/dashboards/platform.yaml").read_text(
                    encoding="utf-8"
                )
            )["providers"]
        }
        applications = providers["applications"]
        self.assertEqual(
            applications["options"]["path"], "/etc/grafana/application-dashboards"
        )
        self.assertIs(applications["options"]["foldersFromFilesStructure"], True)
        # a dashboard leaves with its release, and nobody edits one in the UI
        self.assertIs(applications["disableDeletion"], False)
        self.assertIs(applications["allowUiUpdates"], False)
        # the platform's own dashboard is not something a release can remove
        self.assertIs(providers["platform"]["disableDeletion"], True)

        unit = (ROLE / "templates/platform-metrics.service.j2").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "--dashboards {{ observability_state_directory }}/dashboards", unit
        )
        self.assertIn(
            "{{ observability_state_directory }}/dashboards",
            unit.split("ReadWritePaths=")[1],
        )
        self.assertIn(
            "dashboards",
            {i["name"] for i in DEFAULTS["observability_state_directories"]},
        )

    def test_the_standard_dashboard_ranks_paths_for_every_application(self) -> None:
        board = json.loads(
            (BUNDLE / "grafana/dashboards/application.json").read_text(encoding="utf-8")
        )
        panels = {panel["title"]: panel for panel in board["panels"]}
        ranked = (
            "Most requested paths",
            "Slowest paths, p95",
            "Paths answering 4xx and 5xx",
        )
        for title in ranked:
            panel = panels[title]
            self.assertEqual(panel["type"], "table", title)
            target = panel["targets"][0]
            self.assertEqual(target["queryType"], "instant", title)
            expression = target["expr"]
            # this application's domains only, and only what reached it
            self.assertIn("vhost=~`${domain:regex}`", expression, title)
            self.assertIn('upstream_addr!~"-?"', expression, title)
            self.assertIn('path!=""', expression, title)
            self.assertIn("[$__range]", expression, title)
            self.assertIn("topk(10,", expression, title)
            # /documents/1 and /documents/2 are one address
            self.assertIn('"/:id/"', expression, title)
        # no two panels share a place on the grid
        places = [(p["gridPos"]["x"], p["gridPos"]["y"]) for p in board["panels"]]
        self.assertEqual(len(places), len(set(places)))
        self.assertEqual(len({p["id"] for p in board["panels"]}), len(board["panels"]))

    def test_a_regex_variable_in_logql_sits_in_a_raw_string(self) -> None:
        """Grafana escapes the dots of a domain: ``dev\\.api\\.example``.

        LogQL reads a double-quoted string with Go's escapes, where ``\\.``
        is not one, and answers "parse error". Every panel filtered by domain
        was blank the moment "All" stopped meaning ``.*``. A raw string in
        backticks takes the backslash as it is.
        """
        for path in sorted((BUNDLE / "grafana/dashboards").glob("*.json")):
            board = json.loads(path.read_text(encoding="utf-8"))
            for panel in board["panels"]:
                if panel["datasource"]["type"] != "loki":
                    continue
                for target in panel["targets"]:
                    self.assertNotRegex(
                        target["expr"],
                        r'=~\s*"[^"]*\$\{[^}]*:regex\}',
                        f"{path.name}: {panel['title']}",
                    )

    def test_the_proxy_logs_the_path_without_the_query_string(self) -> None:
        proxy = yaml.safe_load(
            (ROOT / "roles/proxy/files/bundle/compose.yml").read_text(encoding="utf-8")
        )
        formats = [
            service["environment"]["LOG_FORMAT"]
            for service in proxy["services"].values()
            if isinstance(service.get("environment"), dict)
            and "LOG_FORMAT" in service["environment"]
        ]
        self.assertEqual(len(formats), 1)
        self.assertIn('"path":"$$uri"', formats[0])
        self.assertIn('"request":"$$request"', formats[0])

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


class ObservabilityAlertsTest(unittest.TestCase):
    TEMPLATE = (ROLE / "templates/alerting-rules.yaml.j2").read_text(encoding="utf-8")

    def rules(self) -> list:
        rendered = re.sub(
            r"\[\[ (\w+) \]\]",
            lambda match: str(DEFAULTS[match.group(1)]),
            self.TEMPLATE,
        )
        self.assertNotIn("[[", rendered)
        return [
            rule
            for group in yaml.safe_load(rendered)["groups"]
            for rule in group["rules"]
        ]

    def test_every_threshold_is_an_inventory_variable_with_a_default(self) -> None:
        names = set(re.findall(r"\[\[ (\w+) \]\]", self.TEMPLATE))
        self.assertTrue(names)
        for name in names:
            self.assertRegex(name, r"^observability_alert_")
            self.assertGreater(DEFAULTS[name], 0, name)
        # Only variables are Jinja here: Grafana's own templates use braces.
        self.assertNotIn("{%", self.TEMPLATE)
        self.assertNotIn("{#", self.TEMPLATE)

    def test_rules_use_the_provisioned_sources_and_name_what_broke(self) -> None:
        sources = {
            source["uid"]
            for source in yaml.safe_load(
                (BUNDLE / "grafana/provisioning/datasources/platform.yaml").read_text(
                    encoding="utf-8"
                )
            )["datasources"]
        }
        rules = self.rules()
        uids = [rule["uid"] for rule in rules]
        self.assertEqual(len(uids), len(set(uids)))
        for rule in rules:
            self.assertRegex(rule["uid"], r"^platform-[a-z-]+$")
            self.assertIn(rule["labels"]["severity"], {"critical", "warning"})
            self.assertIn(rule["condition"], [query["refId"] for query in rule["data"]])
            for query in rule["data"]:
                self.assertIn(query["datasourceUid"], sources | {"__expr__"})
            for text in rule["annotations"].values():
                # a dollar sign would be eaten by Grafana's file provisioning
                self.assertNotIn("$", text, rule["uid"])
            self.assertTrue(rule["annotations"]["summary"], rule["uid"])

    def test_a_blind_platform_is_the_one_rule_that_fires_on_no_data(self) -> None:
        loud = [rule["uid"] for rule in self.rules() if rule["noDataState"] != "OK"]
        self.assertEqual(loud, ["platform-metrics-stale"])

    def test_the_error_rate_counts_only_requests_that_reached_an_application(
        self,
    ) -> None:
        # Scanners asking for the bare address or a made-up name get 503 from
        # the proxy's default server; that is not an application failing.
        rules = {rule["uid"]: rule for rule in self.rules()}
        expression = rules["platform-http-errors"]["data"][0]["model"]["expr"]
        selectors = expression.count('{container="platform-nginx"}')
        self.assertEqual(selectors, 3)
        # empty with escape=json, a hyphen without it: neither is an upstream
        self.assertEqual(expression.count('upstream_addr!~"-?"'), selectors)

    def test_all_domains_means_the_applications_own_domains(self) -> None:
        board = json.loads(
            (BUNDLE / "grafana/dashboards/application.json").read_text(encoding="utf-8")
        )
        domain = next(v for v in board["templating"]["list"] if v["name"] == "domain")
        self.assertIn("platform_domain_info", domain["definition"])
        self.assertTrue(domain["includeAll"])
        # a catch-all here drew every Host header the internet sent
        self.assertNotIn("allValue", domain)

    def test_the_blind_rule_also_fires_when_its_query_fails(self) -> None:
        # With Prometheus down the sentinel errors instead of returning no data.
        loud = [rule["uid"] for rule in self.rules() if rule["execErrState"] != "OK"]
        self.assertEqual(loud, ["platform-metrics-stale"])

    def test_backup_rules_follow_what_the_release_asks_for(self) -> None:
        rules = {rule["uid"]: rule for rule in self.rules()}
        for uid in ("platform-backup-stale", "platform-backup-missing"):
            expression = rules[uid]["data"][0]["model"]["expr"]
            self.assertIn("platform_backup_scheduled == 1", expression, uid)
            self.assertIn("platform_retired == 0", expression, uid)
        # a release with no dump gets the same grace as an old dump
        self.assertEqual(
            rules["platform-backup-missing"]["for"],
            f"{DEFAULTS['observability_alert_backup_age_hours']}h",
        )

    def test_the_channel_reads_its_secret_from_the_environment(self) -> None:
        channel = yaml.safe_load(
            (ROLE / "files/alerting/telegram.yaml").read_text(encoding="utf-8")
        )
        settings = channel["contactPoints"][0]["receivers"][0]["settings"]
        self.assertEqual(settings["bottoken"], "$OBSERVABILITY_ALERTS_TELEGRAM_TOKEN")
        self.assertEqual(settings["chatid"], "$OBSERVABILITY_ALERTS_TELEGRAM_CHAT_ID")
        self.assertEqual(channel["policies"][0]["receiver"], "platform-telegram")
        grafana = compose("backend.yml")["services"]["grafana"]["environment"]
        for name in (
            "OBSERVABILITY_ALERTS_TELEGRAM_TOKEN",
            "OBSERVABILITY_ALERTS_TELEGRAM_CHAT_ID",
        ):
            self.assertEqual(grafana[name], "${" + name + ":-}")
        environment = (ROLE / "templates/observability.env.j2").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "lookup('ansible.builtin.file', observability_alerts_telegram_token_source)",
            environment,
        )

    def test_without_a_channel_the_rules_stay_and_the_channel_is_removed(self) -> None:
        self.assertEqual(DEFAULTS["observability_alerts_telegram_token_source"], "")
        self.assertEqual(DEFAULTS["observability_alerts_telegram_chat_id"], "")
        none = yaml.safe_load(
            (ROLE / "files/alerting/none.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(none["resetPolicies"], [1])
        self.assertEqual(none["deleteContactPoints"][0]["uid"], "platform-telegram")
        tasks = (ROLE / "tasks/configure.yml").read_text(encoding="utf-8")
        self.assertIn('variable_start_string: "[["', tasks)
        self.assertIn("grafana/provisioning/alerting/notifications.yaml", tasks)
