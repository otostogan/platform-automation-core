"""Explicit opt-in real-Docker acceptance. No host ports, no lab/VPS writes.

Run: .venv/bin/python tests/integration/nginx_activation.py
Creates uniquely named disposable containers/networks and a temporary state tree.
Uses the repository's pinned images, upstream template and real socket policies.
"""

import json
import hashlib
import shutil
import ssl
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

from platform_automation.nginx_reconcile import reconcile
from platform_automation.nginx_transaction import (
    NginxTransactionError,
    NginxTransactionManager,
    build_fragment_plan,
)


def run(*arguments, check=True, timeout=180):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"command failed: {arguments}\n{result.stderr}")
    return result


def main():
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("Docker is required for the explicit integration test")
    run(docker, "info")
    prefix = "platform-activation-test-" + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="platform-nginx-test-") as temporary:
        base = Path(temporary)
        state = base / "state"
        state.mkdir()
        for name in (
            "conf.d",
            "vhost.d",
            "html",
            "certs",
            "htpasswd",
            "managed-vhosts",
            "locks",
        ):
            path = state / name
            path.mkdir()
            path.chmod(0o700 if name in ("managed-vhosts", "locks") else 0o755)
        shutil.copyfile(
            ROOT / "roles/proxy/files/bundle/conf.d/00-security.conf",
            state / "conf.d/00-security.conf",
        )
        (base / "allowlist.json").write_text("[]\n")
        (base / "allowlist.json").chmod(0o600)
        compose = yaml.safe_load(
            (ROOT / "roles/proxy/files/bundle/compose.yml").read_text()
        )
        image = compose["services"]["nginx"]["image"]
        keep = {"nginx", "docker-gen", "docker-socket-read", "docker-socket-acme"}
        compose["name"] = prefix
        compose["services"] = {
            name: spec for name, spec in compose["services"].items() if name in keep
        }
        for name, spec in compose["services"].items():
            spec["container_name"] = prefix + "-" + name
            spec.pop("ports", None)
            spec.pop("profiles", None)
            spec["restart"] = "no"
            spec["volumes"] = [
                mount.replace(
                    "${PLATFORM_PROXY_STATE_DIR:-/var/lib/platform/proxy}", str(state)
                ).replace(
                    "./socket-proxy/",
                    str(ROOT / "roles/proxy/files/bundle/socket-proxy") + "/",
                )
                for mount in spec.get("volumes", [])
            ]
        compose["services"]["docker-gen"]["environment"]["DOCKER_CONTAINER_FILTERS"] = (
            "network=" + prefix + "-edge"
        )
        compose["networks"] = {
            "edge": {"name": prefix + "-edge"},
            "control": {"internal": True},
            "acme-control": {"internal": True},
        }
        config = base / "compose.yml"
        config.write_text(yaml.safe_dump(compose, sort_keys=False))
        command = [docker, "compose", "--file", str(config)]
        backends = [prefix + "-web-a", prefix + "-web-b"]

        def backend(name, body):
            content = base / name
            content.mkdir()
            (content / "index.html").write_text(body + "\n")
            run(
                docker,
                "run",
                "--detach",
                "--name",
                name,
                "--network",
                prefix + "-edge",
                "--env",
                "VIRTUAL_HOST=example.test",
                "--env",
                "VIRTUAL_PORT=80",
                "--volume",
                f"{content}:/usr/share/nginx/html:ro",
                image,
            )

        def response():
            return run(
                docker,
                "exec",
                prefix + "-nginx",
                "wget",
                "-q",
                "-O",
                "-",
                "--header=Host: example.test",
                "http://127.0.0.1",
            ).stdout.strip()

        def wait_response(expected):
            deadline = time.monotonic() + 15
            while True:
                try:
                    if response() == expected:
                        return
                except RuntimeError:
                    pass
                if time.monotonic() >= deadline:
                    raise AssertionError(f"proxy did not serve {expected}")
                time.sleep(0.25)

        manager = NginxTransactionManager(
            vhost_root=state / "vhost.d",
            ownership_root=state / "managed-vhosts",
            default_config=state / "conf.d/default.conf",
            lock_root=state / "locks",
            raw_allowlist=base / "allowlist.json",
            docker_executable=Path(docker),
            nginx_container=prefix + "-nginx",
            docker_gen_container=prefix + "-docker-gen",
        )
        try:
            run(
                *command,
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "90",
                timeout=240,
            )
            reconcile(manager, state / "certs")
            before = manager.default_config.read_bytes()
            backend(backends[0], "version-a")
            deadline = time.monotonic() + 45
            while True:
                preview = run(
                    docker,
                    "exec",
                    prefix + "-docker-gen",
                    "cat",
                    "/tmp/docker-gen-preview.conf",
                ).stdout
                if "server_name example.test;" in preview:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("watcher did not generate preview")
                time.sleep(1)
            assert (
                manager.default_config.read_bytes() == before
            ), "watcher modified live config"
            reconcile(manager, state / "certs")
            wait_response("version-a")
            print(
                "PASS: Docker event generates preview, only reconciler activates it",
                flush=True,
            )

            old = build_fragment_plan(
                "example",
                "lab",
                "a" * 32,
                {"example.test": "client_max_body_size 1m;\n"},
            )
            with manager.prepare(old) as transaction:
                transaction.stage()
                assert not (state / "vhost.d/example.test").exists()
                assert reconcile(manager, state / "certs") is False
                transaction.activate()
            candidate = build_fragment_plan(
                "example",
                "lab",
                "b" * 32,
                {"example.test": "client_max_body_size 2m;\n"},
            )
            with manager.prepare(candidate) as transaction:
                transaction.stage()
                previous_config = manager.default_config.read_bytes()
                backend(backends[1], "version-b")
                run(docker, "rm", "--force", backends[0])
                assert manager.default_config.read_bytes() == previous_config
                assert (state / "vhost.d/example.test").read_text() == old.fragments[
                    "example.test"
                ]
                transaction.activate()
            wait_response("version-b")
            backend_info = json.loads(run(docker, "inspect", backends[1]).stdout)[0]
            ip = backend_info["NetworkSettings"]["Networks"][prefix + "-edge"][
                "IPAddress"
            ]
            assert f"server {ip}:80" in manager.default_config.read_text()
            print(
                "PASS: same hostname, recreated backend, deferred fragments and fresh upstream",
                flush=True,
            )

            broken = build_fragment_plan(
                "example",
                "lab",
                "c" * 32,
                {"example.test": "invalid_platform_directive on;\n"},
            )
            with manager.prepare(broken) as transaction:
                transaction.stage()
                try:
                    transaction.activate()
                except NginxTransactionError:
                    pass
                else:
                    raise AssertionError("invalid nginx directive was accepted")
                assert (
                    response() == "version-b"
                ), "invalid candidate changed loaded nginx"
                transaction.rollback()
            assert response() == "version-b"
            assert not manager.pending_path.exists()
            assert (state / "vhost.d/example.test").read_text() == candidate.fragments[
                "example.test"
            ]
            print(
                "PASS: real nginx -t rejects candidate, rollback restores files and HTTP",
                flush=True,
            )

            # A nonexistent target proves the request wasn't forwarded (Docker
            # would return 404). GET must still reach Docker through the proxy.
            result = run(
                docker,
                "exec",
                prefix + "-docker-socket-acme",
                "wget",
                "-S",
                "-O",
                "-",
                "--post-data=",
                "http://127.0.0.1:2375/containers/nonexistent/kill?signal=SIGHUP",
            )
            assert "204" in result.stderr
            assert (
                "OK"
                in run(
                    docker,
                    "exec",
                    prefix + "-docker-socket-acme",
                    "wget",
                    "-q",
                    "-O",
                    "-",
                    "http://127.0.0.1:2375/_ping",
                ).stdout
            )
            print(
                "PASS: real ACME socket policy acknowledges HUP without forwarding it",
                flush=True,
            )

            # Real TLS certificate-only renewal: no container event or config
            # change is required for the reconciler to reload the new cert.
            python_image = (
                (ROOT / "examples/consumer/application/Dockerfile")
                .read_text()
                .splitlines()[0]
                .split()[1]
            )
            tls_client = (
                "import hashlib,socket,ssl; "
                f"s=socket.create_connection(('{prefix}-nginx',443),timeout=5); "
                "t=ssl._create_unverified_context().wrap_socket(s,server_hostname='example.test'); "
                "print(hashlib.sha256(t.getpeercert(binary_form=True)).hexdigest()); t.close()"
            )

            def issue_fixture_certificate(common_name):
                run(
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-days",
                    "1",
                    "-subj",
                    "/CN=" + common_name,
                    "-keyout",
                    str(state / "certs/example.test.key"),
                    "-out",
                    str(state / "certs/example.test.crt"),
                )
                pem = (state / "certs/example.test.crt").read_text()
                return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()

            def wait_certificate(expected):
                deadline = time.monotonic() + 30
                while True:
                    result = run(
                        docker,
                        "run",
                        "--rm",
                        "--network",
                        prefix + "-edge",
                        "--entrypoint",
                        "python",
                        python_image,
                        "-c",
                        tls_client,
                    )
                    if result.stdout.strip() == expected:
                        return
                    if time.monotonic() >= deadline:
                        raise AssertionError("nginx did not load renewed certificate")
                    time.sleep(0.25)

            first_cert = issue_fixture_certificate("activation-fixture-one")
            assert reconcile(manager, state / "certs")
            wait_certificate(first_cert)
            tls_config = manager.default_config.read_bytes()
            second_cert = issue_fixture_certificate("activation-fixture-two")
            assert first_cert != second_cert
            assert reconcile(manager, state / "certs")
            assert manager.default_config.read_bytes() == tls_config
            wait_certificate(second_cert)
            print(
                "PASS: real TLS certificate renewed with unchanged config and no deploy",
                flush=True,
            )
        except Exception:
            # These are isolated fixtures, never production configs or secrets.
            print(
                "Fixture config:",
                (
                    manager.default_config.read_text()
                    if manager.default_config.exists()
                    else "missing"
                ),
            )
            print(run(docker, "logs", prefix + "-nginx", check=False).stderr)
            raise
        finally:
            for name in backends:
                run(docker, "rm", "--force", name, check=False)
            run(*command, "down", "--remove-orphans")


if __name__ == "__main__":
    main()
