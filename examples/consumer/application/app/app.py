#!/usr/bin/env python3

import argparse
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_VERSION = "0.1.0"


def json_response(payload: dict) -> bytes:
    return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")


def response_for_path(path: str) -> tuple[int, bytes]:
    if path == "/healthz":
        return 200, json_response({"status": "ok"})

    if path == "/version":
        return 200, json_response({"version": APP_VERSION})

    if path == "/":
        return 200, json_response(
            {
                "application": "platform-example",
                "status": "running",
            }
        )

    return 404, json_response({"error": "not found"})


class ApplicationHandler(BaseHTTPRequestHandler):
    server_version = "platform-example"
    sys_version = ""

    def do_GET(self) -> None:
        status, body = response_for_path(self.path)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *arguments) -> None:
        message = format_string % arguments
        print(
            json.dumps(
                {
                    "component": "web",
                    "message": message,
                },
                sort_keys=True,
            ),
            flush=True,
        )


def serve() -> int:
    port = int(os.environ.get("PORT", "3000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), ApplicationHandler)
    server.timeout = 1
    running = True

    def stop_server(signum, frame) -> None:
        nonlocal running
        del signum, frame
        running = False

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    print(
        json.dumps(
            {
                "component": "web",
                "event": "started",
                "port": port,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    while running:
        server.handle_request()

    server.server_close()
    return 0


def healthcheck() -> int:
    port = int(os.environ.get("PORT", "3000"))
    url = f"http://127.0.0.1:{port}/healthz"

    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return 0 if response.status == 200 else 1
    except (urllib.error.URLError, TimeoutError):
        return 1


def worker() -> int:
    running = True

    def stop_worker(signum, frame) -> None:
        nonlocal running
        del signum, frame
        running = False

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)
    print(
        json.dumps(
            {
                "component": "worker",
                "event": "started",
            },
            sort_keys=True,
        ),
        flush=True,
    )

    while running:
        time.sleep(1)

    return 0


def migrate() -> int:
    print(
        json.dumps(
            {
                "component": "migration",
                "status": "succeeded",
            },
            sort_keys=True,
        )
    )
    return 0


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("serve", "worker", "healthcheck", "migrate"),
        nargs="?",
        default="serve",
    )
    return parser.parse_args()


def main() -> int:
    command = parse_arguments().command

    if command == "serve":
        return serve()

    if command == "worker":
        return worker()

    if command == "healthcheck":
        return healthcheck()

    if command == "migrate":
        return migrate()

    return 2


if __name__ == "__main__":
    sys.exit(main())
