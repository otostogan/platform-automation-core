#!/usr/bin/env python3

import argparse
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import Callable, Optional, Sequence, TextIO
from urllib.parse import urlsplit


DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
REDIRECT_STATUSES = {301, 302, 307, 308}
HTTP_01_REACHABLE_STATUSES = {200, 204, 301, 302, 307, 308, 404}


class AcmeReadinessError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckResult:
    check: str
    status: str
    message: str


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]


@dataclass(frozen=True)
class CertificateInfo:
    issuer: str
    not_after_epoch: float
    sans: tuple[str, ...]
    fingerprint_sha256: str


@dataclass(frozen=True)
class AuditConfig:
    domain: str
    expected_addresses: tuple[str, ...]
    phase: str
    health_path: str
    timeout_seconds: float
    staging_issuer_marker: str
    minimum_validity_hours: int


Resolver = Callable[[str], set[str]]
Requester = Callable[[str, str, bool, float], HttpResult]
CertificateReader = Callable[[str, float], CertificateInfo]


def validate_domain(domain: str) -> str:
    normalized = domain.rstrip(".").lower()
    if not DOMAIN_PATTERN.fullmatch(normalized):
        raise AcmeReadinessError(f"invalid public domain: {domain}")
    return normalized


def validate_expected_addresses(addresses: Sequence[str]) -> tuple[str, ...]:
    if not addresses:
        raise AcmeReadinessError("at least one --expected-address is required")

    normalized = []
    for address in addresses:
        try:
            normalized.append(str(ipaddress.ip_address(address)))
        except ValueError as error:
            raise AcmeReadinessError(
                f"invalid expected IP address: {address}"
            ) from error
    return tuple(sorted(set(normalized)))


def resolve_addresses(domain: str) -> set[str]:
    try:
        records = socket.getaddrinfo(
            domain,
            None,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise AcmeReadinessError(f"DNS lookup failed: {error}") from error

    addresses = set()
    for record in records:
        try:
            addresses.add(str(ipaddress.ip_address(record[4][0])))
        except ValueError as error:
            raise AcmeReadinessError("DNS returned an invalid IP address") from error
    return addresses


def request_http(
    domain: str,
    path: str,
    tls: bool,
    timeout_seconds: float,
) -> HttpResult:
    connection: http.client.HTTPConnection
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        connection = http.client.HTTPSConnection(
            domain,
            443,
            timeout=timeout_seconds,
            context=context,
        )
    else:
        connection = http.client.HTTPConnection(
            domain,
            80,
            timeout=timeout_seconds,
        )

    try:
        connection.request(
            "GET",
            path,
            headers={"User-Agent": "platform-automation-acme-readiness/1"},
        )
        response = connection.getresponse()
        response.read(1024)
        return HttpResult(
            status=response.status,
            headers={key.lower(): value for key, value in response.getheaders()},
        )
    except (OSError, http.client.HTTPException) as error:
        scheme = "HTTPS" if tls else "HTTP"
        raise AcmeReadinessError(f"{scheme} request failed: {error}") from error
    finally:
        connection.close()


def read_certificate(domain: str, timeout_seconds: float) -> CertificateInfo:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    try:
        with socket.create_connection(
            (domain, 443),
            timeout=timeout_seconds,
        ) as connection:
            with context.wrap_socket(
                connection,
                server_hostname=domain,
            ) as tls_connection:
                certificate_der = tls_connection.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as error:
        raise AcmeReadinessError(
            f"TLS certificate connection failed: {error}"
        ) from error

    if not certificate_der:
        raise AcmeReadinessError("TLS peer did not return a certificate")

    return inspect_certificate_der(certificate_der, timeout_seconds)


def inspect_certificate_der(
    certificate_der: bytes,
    timeout_seconds: float,
    runner: Callable = subprocess.run,
) -> CertificateInfo:
    try:
        result = runner(
            [
                "openssl",
                "x509",
                "-inform",
                "DER",
                "-noout",
                "-issuer",
                "-enddate",
                "-ext",
                "subjectAltName",
            ],
            input=certificate_der,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AcmeReadinessError(f"openssl inspection failed: {error}") from error

    if result.returncode != 0:
        raise AcmeReadinessError("openssl rejected the server certificate")

    output = result.stdout.decode("utf-8", errors="replace")
    issuer_match = re.search(r"^issuer=(.+)$", output, flags=re.MULTILINE)
    expiry_match = re.search(r"^notAfter=(.+)$", output, flags=re.MULTILINE)
    sans = tuple(sorted(set(re.findall(r"DNS:([^,\s]+)", output))))

    if issuer_match is None or expiry_match is None or not sans:
        raise AcmeReadinessError("openssl returned incomplete certificate metadata")

    try:
        not_after_epoch = ssl.cert_time_to_seconds(expiry_match.group(1).strip())
    except ValueError as error:
        raise AcmeReadinessError("certificate expiry is invalid") from error

    return CertificateInfo(
        issuer=issuer_match.group(1).strip(),
        not_after_epoch=not_after_epoch,
        sans=sans,
        fingerprint_sha256=hashlib.sha256(certificate_der).hexdigest(),
    )


def certificate_covers_domain(domain: str, sans: Sequence[str]) -> bool:
    for name in sans:
        normalized = name.lower()
        if normalized == domain:
            return True
        if normalized.startswith("*."):
            suffix = normalized[1:]
            if domain.endswith(suffix) and domain.count(".") == normalized.count("."):
                return True
    return False


def failure(check: str, error: Exception) -> CheckResult:
    return CheckResult(check=check, status="fail", message=str(error))


def audit_domain(
    config: AuditConfig,
    resolver: Resolver = resolve_addresses,
    requester: Requester = request_http,
    certificate_reader: CertificateReader = read_certificate,
    now: Callable[[], float] = time.time,
) -> list[CheckResult]:
    results = []
    expected = set(config.expected_addresses)

    try:
        resolved = resolver(config.domain)
        results.append(
            CheckResult(
                check="dns_addresses",
                status="pass" if resolved == expected else "fail",
                message=(
                    f"resolved={','.join(sorted(resolved)) or 'none'}; "
                    f"expected={','.join(sorted(expected))}"
                ),
            )
        )
    except AcmeReadinessError as error:
        results.append(failure("dns_addresses", error))

    challenge_path = "/.well-known/acme-challenge/platform-readiness-probe"
    try:
        challenge = requester(
            config.domain,
            challenge_path,
            False,
            config.timeout_seconds,
        )
        results.append(
            CheckResult(
                check="http_01_reachability",
                status=(
                    "pass" if challenge.status in HTTP_01_REACHABLE_STATUSES else "fail"
                ),
                message=f"HTTP {challenge.status} on {challenge_path}",
            )
        )
    except AcmeReadinessError as error:
        results.append(failure("http_01_reachability", error))

    if config.phase == "pre":
        return results

    try:
        redirect = requester(
            config.domain,
            "/",
            False,
            config.timeout_seconds,
        )
        location = redirect.headers.get("location", "")
        parsed_location = urlsplit(location)
        redirect_ok = (
            redirect.status in REDIRECT_STATUSES
            and parsed_location.scheme == "https"
            and parsed_location.hostname == config.domain
        )
        results.append(
            CheckResult(
                check="https_redirect",
                status="pass" if redirect_ok else "fail",
                message=f"HTTP {redirect.status}; Location={location or 'missing'}",
            )
        )
    except AcmeReadinessError as error:
        results.append(failure("https_redirect", error))

    try:
        health = requester(
            config.domain,
            config.health_path,
            True,
            config.timeout_seconds,
        )
        results.append(
            CheckResult(
                check="https_health",
                status="pass" if health.status == 200 else "fail",
                message=f"HTTPS {health.status} on {config.health_path}",
            )
        )
    except AcmeReadinessError as error:
        results.append(failure("https_health", error))

    try:
        certificate = certificate_reader(config.domain, config.timeout_seconds)
        results.extend(
            [
                CheckResult(
                    check="certificate_san",
                    status=(
                        "pass"
                        if certificate_covers_domain(config.domain, certificate.sans)
                        else "fail"
                    ),
                    message=f"SANs={','.join(certificate.sans)}",
                ),
                CheckResult(
                    check="certificate_staging_issuer",
                    status=(
                        "pass"
                        if config.staging_issuer_marker.lower()
                        in certificate.issuer.lower()
                        else "fail"
                    ),
                    message=f"issuer={certificate.issuer}",
                ),
                CheckResult(
                    check="certificate_validity",
                    status=(
                        "pass"
                        if certificate.not_after_epoch
                        >= now() + config.minimum_validity_hours * 3600
                        else "fail"
                    ),
                    message=(f"expires_at_epoch={int(certificate.not_after_epoch)}"),
                ),
                CheckResult(
                    check="certificate_fingerprint",
                    status="info",
                    message=f"sha256={certificate.fingerprint_sha256}",
                ),
            ]
        )
    except AcmeReadinessError as error:
        results.append(failure("certificate", error))

    return results


def report_document(config: AuditConfig, results: Sequence[CheckResult]) -> dict:
    return {
        "domain": config.domain,
        "phase": config.phase,
        "ready": not any(result.status == "fail" for result in results),
        "checks": [asdict(result) for result in results],
    }


def print_report(
    document: dict,
    output_format: str,
    stdout: TextIO,
) -> None:
    if output_format == "json":
        print(json.dumps(document, sort_keys=True), file=stdout)
        return

    for result in document["checks"]:
        print(
            f"[{result['status'].upper()}] " f"{result['check']}: {result['message']}",
            file=stdout,
        )
    print(f"ready: {str(document['ready']).lower()}", file=stdout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit public DNS and ACME staging readiness without changes",
    )
    parser.add_argument("--domain", required=True)
    parser.add_argument(
        "--expected-address",
        action="append",
        required=True,
        help="Expected public IPv4 or IPv6 address; repeat for every DNS record",
    )
    parser.add_argument("--phase", choices=("pre", "staging"), default="pre")
    parser.add_argument("--health-path", default="/healthz")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--staging-issuer-marker", default="STAGING")
    parser.add_argument("--minimum-validity-hours", type=int, default=24)
    parser.add_argument("--output", choices=("human", "json"), default="human")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)

    try:
        domain = validate_domain(arguments.domain)
        expected_addresses = validate_expected_addresses(arguments.expected_address)
        if not arguments.health_path.startswith("/"):
            raise AcmeReadinessError("--health-path must start with /")
        if arguments.timeout_seconds <= 0:
            raise AcmeReadinessError("--timeout-seconds must be positive")
        if arguments.minimum_validity_hours < 1:
            raise AcmeReadinessError("--minimum-validity-hours must be positive")

        config = AuditConfig(
            domain=domain,
            expected_addresses=expected_addresses,
            phase=arguments.phase,
            health_path=arguments.health_path,
            timeout_seconds=arguments.timeout_seconds,
            staging_issuer_marker=arguments.staging_issuer_marker,
            minimum_validity_hours=arguments.minimum_validity_hours,
        )
        results = audit_domain(config)
    except AcmeReadinessError as error:
        print(f"acme readiness error: {error}", file=stderr)
        return 2

    document = report_document(config, results)
    print_report(document, arguments.output, stdout)
    return 0 if document["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
