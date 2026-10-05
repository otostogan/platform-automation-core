"""Explicit opt-in real-Docker acceptance of the observability bundle.

Run: .venv/bin/python tests/integration/observability_stack.py
Brings the collector and the storage up from the role's own Compose files
under unique names, starts one workload container named the way a deployed
application is, and proves the three things the role promises: its log
lines arrive in Loki, its metrics in Prometheus, both under the platform's
labels, and Grafana serves the provisioned sources and dashboard. Then the
alert rules: every one evaluates without an error, a stopped container raises
its alert with the annotation filled in, and the notification channel can be
provisioned and taken away again.
"""

import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles/observability"
BUNDLE = ROLE / "files/bundle"
ALERTING = ROLE / "files/alerting"
STATE = "${PLATFORM_OBSERVABILITY_STATE_DIR:-/var/lib/platform/observability}"
PASSWORD = "integration-only"


def run(*arguments, check=True, timeout=300):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(
            f"command failed: {arguments}\n{result.stdout}\n{result.stderr}"
        )
    return result


def rewrite(source: Path, prefix: str, state: Path, destination: Path) -> None:
    compose = yaml.safe_load(source.read_text())
    compose["name"] = prefix + "-" + source.stem
    for name, spec in compose["services"].items():
        spec["container_name"] = prefix + "-" + name
        spec["restart"] = "no"
        spec["volumes"] = [
            mount.replace(STATE, str(state)).replace("./", str(BUNDLE) + "/")
            for mount in spec.get("volumes", [])
        ]
    for key, network in compose["networks"].items():
        network["name"] = prefix + "-" + key
    destination.write_text(yaml.safe_dump(compose, sort_keys=False))


def render_rules() -> str:
    """The role's template with its defaults, the way Ansible renders it."""
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    text = (ROLE / "templates/alerting-rules.yaml.j2").read_text()
    rendered = re.sub(
        r"\[\[ (\w+) \]\]", lambda match: str(defaults[match.group(1)]), text
    )
    assert "[[" not in rendered
    return rendered


def wait_for(description, probe, seconds=90):
    deadline = time.monotonic() + seconds
    last = None
    while True:
        try:
            last = probe()
            if last:
                return last
        except (RuntimeError, ValueError, KeyError, IndexError) as error:
            last = error
        if time.monotonic() >= deadline:
            raise AssertionError(f"{description}: never became true; last: {last}")
        time.sleep(2)


def main():
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker is required for the explicit integration test")
    run(docker, "info")
    prefix = "platform-obs-test-" + uuid.uuid4().hex[:8]
    marker = "hello-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="platform-obs-test-") as temporary:
        base = Path(temporary)
        state = base / "state"
        for name in (
            "loki",
            "prometheus",
            "grafana",
            "alloy",
            "textfile",
            "targets",
            "dashboards",
        ):
            (state / name).mkdir(parents=True)
            (state / name).chmod(0o777)
        backend, collector = base / "backend.yml", base / "collector.yml"
        rewrite(BUNDLE / "backend.yml", prefix, state, backend)
        rewrite(BUNDLE / "collector.yml", prefix, state, collector)
        # The role installs the bundle's provisioning plus one notification
        # file chosen by the inventory; do the same in a private copy.
        provisioning = base / "provisioning"
        shutil.copytree(BUNDLE / "grafana/provisioning", provisioning)
        notifications = provisioning / "alerting/notifications.yaml"
        notifications.parent.mkdir()
        shutil.copy(ALERTING / "telegram.yaml", notifications)
        rules_text = render_rules()
        (provisioning / "alerting/rules.yaml").write_text(rules_text)
        backend.write_text(
            backend.read_text().replace(
                str(BUNDLE) + "/grafana/provisioning", str(provisioning)
            )
        )
        assert str(provisioning) in backend.read_text()
        environment = [
            "env",
            f"OBSERVABILITY_GRAFANA_ADMIN_PASSWORD={PASSWORD}",
            "OBSERVABILITY_HOST_LABEL=integration-host",
            "OBSERVABILITY_ALERTS_TELEGRAM_TOKEN=1:integration-only",
            "OBSERVABILITY_ALERTS_TELEGRAM_CHAT_ID=-1000000000001",
        ]
        workload = prefix + "-web"
        network = prefix + "-observability"
        edge = prefix + "-edge"
        instrumented = prefix + "-instrumented"

        def inside(container, *command):
            return run(docker, "exec", prefix + "-" + container, *command).stdout

        def fetch(url):
            # prometheus carries busybox wget and sits on the shared network
            return inside("prometheus", "wget", "-q", "-O", "-", url)

        try:
            run(docker, "network", "create", network)
            run(docker, "network", "create", edge)
            run(
                *environment,
                docker,
                "compose",
                "--file",
                str(backend),
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "180",
                timeout=400,
            )
            run(
                *environment,
                docker,
                "compose",
                "--file",
                str(collector),
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "180",
                timeout=400,
            )
            print(
                "PASS: storage and collector start from the role's Compose files",
                flush=True,
            )

            run(
                docker,
                "run",
                "--detach",
                "--name",
                workload,
                "--label",
                "com.docker.compose.project=example-lab",
                "--label",
                "com.docker.compose.service=web",
                "alpine:3.20",
                "sh",
                "-c",
                f'while true; do echo \'{{"level":"info","msg":"{marker}"}}\'; sleep 1; done',
            )

            selector = '{project="example",environment="lab",service="web",host="integration-host"}'

            def logged():
                body = fetch(
                    "http://loki:3100/loki/api/v1/query_range?limit=20&query="
                    + selector.replace("{", "%7B")
                    .replace("}", "%7D")
                    .replace('"', "%22")
                )
                streams = json.loads(body)["data"]["result"]
                return any(
                    marker in line for stream in streams for _, line in stream["values"]
                )

            wait_for("the workload's log line in Loki under platform labels", logged)
            print(
                "PASS: container logs reach Loki labelled host/project/environment/service",
                flush=True,
            )

            def measured(query):
                def probe():
                    body = fetch("http://prometheus:9090/api/v1/query?query=" + query)
                    return json.loads(body)["data"]["result"]

                return probe

            # The platform's own exporter, run for real against this Docker:
            # the role runs it from a timer, the test runs it once.
            from platform_automation.metrics_export import main as export

            assert (
                export(
                    [
                        "--output",
                        str(state / "textfile/platform.prom"),
                        "--docker",
                        docker,
                        "--projects-root",
                        str(base / "none"),
                        "--releases-root",
                        str(base / "none"),
                        "--backups-root",
                        str(base / "none"),
                    ]
                )
                == 0
            )
            wait_for(
                "container metrics in Prometheus under platform labels",
                measured(
                    "platform_container_memory_bytes%7Bproject=%22example%22,environment=%22lab%22,service=%22web%22,host=%22integration-host%22%7D"
                ),
                seconds=150,
            )
            # An application that declares service.metrics: a web container on
            # the proxy's network serving the text format. The targets are
            # built by the exporter's own code from this Docker.
            exposition = (
                "# TYPE example_orders_total counter\\n"
                'example_orders_total{host="forged-host",project="forged"} 7\\n'
                "# TYPE platform_container_up gauge\\n"
                'platform_container_up{container="forged"} 1\\n'
            )
            run(
                docker,
                "run",
                "--detach",
                "--name",
                instrumented,
                "--network",
                edge,
                "--label",
                "com.docker.compose.project=example-lab",
                "--label",
                "com.docker.compose.service=web",
                "busybox:1.36",
                "sh",
                "-c",
                f"mkdir -p /www && printf '{exposition}' > /www/metrics.txt"
                " && httpd -f -p 9464 -h /www",
            )
            from platform_automation.metrics_export import (
                edge_addresses,
                scrape_targets,
            )

            declared = {
                ("example", "lab"): {
                    "web": "web",
                    "metrics": {"path": "/metrics.txt", "port": 9464},
                }
            }
            targets = scrape_targets(declared, edge_addresses(Path(docker), edge))
            assert [t["labels"]["container"] for t in targets] == [
                instrumented
            ], targets
            (state / "targets/applications.json").write_text(json.dumps(targets))

            own = wait_for(
                "the application's own metric in Prometheus under platform labels",
                measured(
                    "example_orders_total%7Bproject=%22example%22,environment=%22lab%22,service=%22web%22,host=%22integration-host%22,job=%22application%22%7D"
                ),
                seconds=180,
            )
            assert own[0]["value"][1] == "7", own
            assert own[0]["metric"]["instance"] == instrumented, own
            # what the application said about itself is kept, but set aside
            assert own[0]["metric"]["exported_host"] == "forged-host", own
            assert own[0]["metric"]["exported_project"] == "forged", own
            assert measured("up%7Bjob=%22application%22%7D")()[0]["value"][1] == "1"
            forged = measured("platform_container_up%7Bcontainer=%22forged%22%7D")()
            assert forged == [], f"an application wrote a platform metric: {forged}"
            print(
                "PASS: a declared metrics endpoint is scraped under platform labels,"
                " and cannot write platform metrics",
                flush=True,
            )

            # The stamp must survive the trip digit for digit: a rounded one
            # reads as hours old and raises "Platform metrics stopped".
            age = wait_for(
                "the export stamp in Prometheus",
                measured("time()-platform_metrics_export_timestamp_seconds"),
                seconds=150,
            )
            assert -5 < float(age[0]["value"][1]) < 200, age
            wait_for(
                "host metrics in Prometheus",
                measured("node_memory_MemTotal_bytes%7Bhost=%22integration-host%22%7D"),
                seconds=150,
            )
            print(
                "PASS: container and host metrics reach Prometheus by remote write",
                flush=True,
            )

            def grafana(path):
                return json.loads(
                    inside(
                        "grafana",
                        "curl",
                        "-fsS",
                        "-u",
                        f"admin:{PASSWORD}",
                        "http://127.0.0.1:3000" + path,
                    )
                )

            sources = {
                s["uid"]
                for s in wait_for("Grafana API", lambda: grafana("/api/datasources"))
            }
            assert sources == {"platform-loki", "platform-prometheus"}, sources
            boards = {d["uid"] for d in grafana("/api/search?type=dash-db")}
            assert "platform-application" in boards, boards
            for uid in sorted(sources):
                health = grafana(f"/api/datasources/uid/{uid}/health")
                assert health.get("status") == "OK", (uid, health)
            print(
                "PASS: Grafana serves the provisioned sources and dashboard, and both sources answer",
                flush=True,
            )

            # A dashboard an application ships: written by the exporter's own
            # code, picked up by the real Grafana into a folder named after
            # the application, and gone again when the release stops shipping it.
            from platform_automation.metrics_export import sync_dashboards
            from platform_automation.observability_bundle import provisioned

            shipped = {
                "title": "Orders",
                "uid": "the-authors-own-uid",
                "panels": [
                    {
                        "id": 1,
                        "type": "timeseries",
                        "title": "Orders per second",
                        "datasource": {
                            "type": "prometheus",
                            "uid": "platform-prometheus",
                        },
                        "targets": [{"expr": "rate(example_orders_total[5m])"}],
                    }
                ],
            }
            folder = state / "dashboards"
            sync_dashboards(
                folder,
                {
                    "example-lab": {
                        "orders.json": json.dumps(
                            provisioned("example", "lab", "orders", shipped)
                        )
                    }
                },
            )

            def shown():
                found = grafana("/api/search?type=dash-db")
                return {d["uid"]: d.get("folderTitle") for d in found}

            listed = wait_for(
                "the application's dashboard in Grafana",
                lambda: "example-lab-orders" in shown() and shown(),
                seconds=120,
            )
            assert listed["example-lab-orders"] == "example-lab", listed
            assert "the-authors-own-uid" not in listed, listed
            assert "platform-application" in listed, listed
            loaded = grafana("/api/dashboards/uid/example-lab-orders")
            assert loaded["dashboard"]["editable"] is False, loaded["dashboard"]
            assert loaded["meta"]["provisioned"] is True, loaded["meta"]

            sync_dashboards(folder, {})
            wait_for(
                "the dashboard to leave with its release",
                lambda: "example-lab-orders" not in shown(),
                seconds=120,
            )
            assert "platform-application" in shown()
            print(
                "PASS: a shipped dashboard appears in its application's folder under"
                " the platform's uid, and leaves with the release",
                flush=True,
            )

            # Every expression of every provisioned dashboard must be accepted
            # by the engine it is addressed to: a typo in LogQL or PromQL is a
            # blank panel on the host, found only when someone needs it.
            from urllib.parse import quote

            checked = 0
            for board in sorted((BUNDLE / "grafana/dashboards").glob("*.json")):
                document = json.loads(board.read_text())
                targets = [
                    (
                        panel["datasource"]["type"],
                        target["expr"],
                        f"{board.name}: {panel['title']}",
                    )
                    for panel in document["panels"]
                    for target in panel.get("targets", [])
                ] + [
                    (
                        note["datasource"]["type"],
                        note["expr"],
                        f"{board.name}: annotation {note['name']}",
                    )
                    for note in document["annotations"]["list"]
                ]
                for kind, expression, where in targets:
                    for token, value in (
                        # what Grafana really substitutes for two selected
                        # domains: escaped dots, which is exactly what a
                        # double-quoted LogQL string cannot hold
                        (
                            "${domain:regex}",
                            "(app\\.example\\.test|mail\\.example\\.test)",
                        ),
                        ("$project", "example"),
                        ("$environment", "lab"),
                        ("$service", ".*"),
                        ("$search", ""),
                        ("$__auto", "5m"),
                        ("$__range", "1h"),
                        ("$__rate_interval", "5m"),
                    ):
                        expression = expression.replace(token, value)
                    assert (
                        "$" not in expression
                    ), f"{where}: unresolved variable in {expression}"
                    base_url = (
                        "http://prometheus:9090/api/v1/query?query="
                        if kind == "prometheus"
                        else "http://loki:3100/loki/api/v1/query_range?limit=1&query="
                    )
                    try:
                        answer = json.loads(
                            fetch(base_url + quote(expression, safe=""))
                        )
                    except RuntimeError as error:
                        raise AssertionError(
                            f"{where}: rejected: {expression}\n{error}"
                        ) from error
                    assert answer["status"] == "success", (where, answer)
                    checked += 1
            assert checked >= 10, checked
            print(
                f"PASS: all {checked} dashboard expressions are accepted by Loki and Prometheus",
                flush=True,
            )

            # ---------------------------------------------------- alerts
            expected = {
                rule["uid"]
                for group in yaml.safe_load(rules_text)["groups"]
                for rule in group["rules"]
            }
            provisioned = grafana("/api/v1/provisioning/alert-rules")
            assert {rule["uid"] for rule in provisioned} == expected, provisioned
            for rule in provisioned:
                for query in rule["data"]:
                    text = json.dumps(query["model"])
                    assert "[[" not in text, text
            points = grafana("/api/v1/provisioning/contact-points")
            telegram = [p for p in points if p["uid"] == "platform-telegram"]
            # the chat comes from the environment; the token is never echoed
            assert (
                telegram and str(telegram[0]["settings"]["chatid"]) == "-1000000000001"
            ), points
            assert "integration-only" not in json.dumps(points), points
            assert (
                grafana("/api/v1/provisioning/policies")["receiver"]
                == "platform-telegram"
            )

            def evaluated():
                groups = grafana("/api/prometheus/grafana/api/v1/rules")["data"][
                    "groups"
                ]
                rules = [rule for group in groups for rule in group["rules"]]
                bad = [
                    (rule["name"], rule.get("lastError"))
                    for rule in rules
                    if rule["health"] == "error"
                ]
                assert not bad, f"alert rules fail to evaluate: {bad}"
                done = all(
                    rule.get("lastEvaluation", "")[:4] > "0001" for rule in rules
                )
                return rules if done and len(rules) == len(expected) else None

            wait_for("every alert rule to evaluate once", evaluated, seconds=180)
            print(
                f"PASS: all {len(expected)} alert rules are provisioned and evaluate without error",
                flush=True,
            )

            run(docker, "stop", "--time", "1", workload)
            assert (
                export(
                    [
                        "--output",
                        str(state / "textfile/platform.prom"),
                        "--docker",
                        docker,
                        "--projects-root",
                        str(base / "none"),
                        "--releases-root",
                        str(base / "none"),
                        "--backups-root",
                        str(base / "none"),
                    ]
                )
                == 0
            )

            def raised():
                for rule in evaluated() or []:
                    for alert in rule.get("alerts", []):
                        if alert["labels"].get("container") == workload:
                            return alert
                return None

            alert = wait_for("the stopped container's alert", raised, seconds=240)
            assert alert["labels"]["alertname"] == "Container down", alert
            assert alert["labels"]["severity"] == "critical", alert
            assert (
                alert["annotations"]["summary"]
                == f"integration-host: {workload} is not running"
            ), alert
            print(
                "PASS: a stopped container raises its alert with the annotation filled in",
                flush=True,
            )

            shutil.copy(ALERTING / "none.yaml", notifications)
            run(docker, "restart", prefix + "-grafana")
            # an empty list is the expected answer, so wrap it to stay truthy
            (points,) = wait_for(
                "Grafana after the channel is removed",
                lambda: (grafana("/api/v1/provisioning/contact-points"),),
            )
            assert all(point["uid"] != "platform-telegram" for point in points), points
            assert grafana("/api/v1/provisioning/policies")["receiver"] != (
                "platform-telegram"
            )
            assert {
                rule["uid"] for rule in grafana("/api/v1/provisioning/alert-rules")
            } == expected
            print(
                "PASS: the notification channel can be taken away and the rules stay",
                flush=True,
            )

            ports = run(
                docker, "ps", "--filter", f"name={prefix}", "--format", "{{.Ports}}"
            ).stdout
            assert "->" not in ports, f"the bundle published a host port: {ports!r}"
            print("PASS: nothing is published on the host", flush=True)
        except Exception:
            for name in (
                "alloy",
                "loki",
                "grafana",
                "prometheus",
                "docker-socket-collector",
            ):
                logs = run(
                    docker, "logs", "--tail", "40", prefix + "-" + name, check=False
                )
                print(
                    f"--- {name}\n{logs.stdout[-3000:]}{logs.stderr[-3000:]}",
                    flush=True,
                )
            raise
        finally:
            run(docker, "rm", "--force", workload, check=False)
            run(docker, "rm", "--force", instrumented, check=False)
            run(
                *environment,
                docker,
                "compose",
                "--file",
                str(collector),
                "down",
                "--volumes",
                check=False,
                timeout=180,
            )
            run(
                *environment,
                docker,
                "compose",
                "--file",
                str(backend),
                "down",
                "--volumes",
                check=False,
                timeout=180,
            )
            run(docker, "network", "rm", network, check=False)
            run(docker, "network", "rm", edge, check=False)
            # containers wrote as their own users
            run(
                docker,
                "run",
                "--rm",
                "--volume",
                f"{state}:/state",
                "alpine:3.20",
                "sh",
                "-c",
                "rm -rf /state/*",
                check=False,
            )


if __name__ == "__main__":
    main()
