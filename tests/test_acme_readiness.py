import io
import json
import subprocess
import unittest
from pathlib import Path


from platform_automation.acme_readiness import (  # noqa: E402
    AcmeReadinessError,
    AuditConfig,
    CertificateInfo,
    HttpResult,
    audit_domain,
    certificate_covers_domain,
    inspect_certificate_der,
    print_report,
    report_document,
    validate_domain,
    validate_expected_addresses,
)


class AcmeReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AuditConfig(
            domain="app.example.com",
            expected_addresses=("203.0.113.10",),
            phase="staging",
            health_path="/healthz",
            timeout_seconds=5,
            staging_issuer_marker="STAGING",
            minimum_validity_hours=24,
        )

    @staticmethod
    def requester(domain, path, tls, timeout):
        del domain, timeout
        if tls:
            return HttpResult(status=200, headers={})
        if path == "/":
            return HttpResult(
                status=308,
                headers={"location": "https://app.example.com/"},
            )
        return HttpResult(status=404, headers={})

    @staticmethod
    def certificate_reader(domain, timeout):
        del domain, timeout
        return CertificateInfo(
            issuer="CN=(STAGING) Pretend Intermediate",
            not_after_epoch=2_000_000_000,
            sans=("app.example.com",),
            fingerprint_sha256="a" * 64,
        )

    def test_staging_audit_passes_complete_public_path(self) -> None:
        results = audit_domain(
            self.config,
            resolver=lambda domain: {"203.0.113.10"},
            requester=self.requester,
            certificate_reader=self.certificate_reader,
            now=lambda: 1_900_000_000,
        )

        self.assertTrue(report_document(self.config, results)["ready"])
        self.assertEqual(
            [result.check for result in results],
            [
                "dns_addresses",
                "http_01_reachability",
                "https_redirect",
                "https_health",
                "certificate_san",
                "certificate_staging_issuer",
                "certificate_validity",
                "certificate_fingerprint",
            ],
        )

    def test_stale_aaaa_record_fails_exact_dns_match(self) -> None:
        results = audit_domain(
            self.config,
            resolver=lambda domain: {"203.0.113.10", "2001:db8::10"},
            requester=self.requester,
            certificate_reader=self.certificate_reader,
            now=lambda: 1_900_000_000,
        )

        dns = next(result for result in results if result.check == "dns_addresses")
        self.assertEqual(dns.status, "fail")
        self.assertFalse(report_document(self.config, results)["ready"])

    def test_wrong_redirect_and_production_issuer_fail_staging(self) -> None:
        def wrong_redirect(domain, path, tls, timeout):
            result = self.requester(domain, path, tls, timeout)
            if not tls and path == "/":
                return HttpResult(
                    status=302,
                    headers={"location": "https://wrong.example.com/"},
                )
            return result

        def production_certificate(domain, timeout):
            certificate = self.certificate_reader(domain, timeout)
            return CertificateInfo(
                issuer="CN=Let's Encrypt Production Intermediate",
                not_after_epoch=certificate.not_after_epoch,
                sans=certificate.sans,
                fingerprint_sha256=certificate.fingerprint_sha256,
            )

        results = audit_domain(
            self.config,
            resolver=lambda domain: {"203.0.113.10"},
            requester=wrong_redirect,
            certificate_reader=production_certificate,
            now=lambda: 1_900_000_000,
        )
        statuses = {result.check: result.status for result in results}

        self.assertEqual(statuses["https_redirect"], "fail")
        self.assertEqual(statuses["certificate_staging_issuer"], "fail")

    def test_pre_phase_does_not_require_https_or_certificate(self) -> None:
        config = AuditConfig(
            **{**self.config.__dict__, "phase": "pre"},
        )

        def unexpected_certificate(domain, timeout):
            raise AssertionError("certificate must not be read during pre phase")

        results = audit_domain(
            config,
            resolver=lambda domain: {"203.0.113.10"},
            requester=self.requester,
            certificate_reader=unexpected_certificate,
        )

        self.assertEqual(len(results), 2)
        self.assertTrue(report_document(config, results)["ready"])

    def test_http_01_forbidden_response_is_not_considered_ready(self) -> None:
        config = AuditConfig(
            **{**self.config.__dict__, "phase": "pre"},
        )

        def forbidden_request(domain, path, tls, timeout):
            del domain, path, tls, timeout
            return HttpResult(status=403, headers={})

        results = audit_domain(
            config,
            resolver=lambda domain: {"203.0.113.10"},
            requester=forbidden_request,
        )

        self.assertEqual(results[1].status, "fail")

    def test_network_errors_are_reported_without_stopping_other_checks(self) -> None:
        def failing_resolver(domain):
            raise AcmeReadinessError("DNS unavailable")

        results = audit_domain(
            self.config,
            resolver=failing_resolver,
            requester=self.requester,
            certificate_reader=self.certificate_reader,
            now=lambda: 1_900_000_000,
        )

        self.assertEqual(results[0].status, "fail")
        self.assertGreater(len(results), 1)

    def test_json_report_is_machine_readable(self) -> None:
        results = audit_domain(
            self.config,
            resolver=lambda domain: {"203.0.113.10"},
            requester=self.requester,
            certificate_reader=self.certificate_reader,
            now=lambda: 1_900_000_000,
        )
        output = io.StringIO()

        print_report(report_document(self.config, results), "json", output)

        document = json.loads(output.getvalue())
        self.assertTrue(document["ready"])
        self.assertEqual(document["domain"], "app.example.com")

    def test_input_validation_rejects_unsafe_values(self) -> None:
        with self.assertRaises(AcmeReadinessError):
            validate_domain("localhost")
        with self.assertRaises(AcmeReadinessError):
            validate_expected_addresses(["not-an-ip"])

    def test_wildcard_covers_only_one_label(self) -> None:
        self.assertTrue(certificate_covers_domain("app.example.com", ["*.example.com"]))
        self.assertFalse(
            certificate_covers_domain("deep.app.example.com", ["*.example.com"])
        )

    def test_openssl_metadata_is_parsed_from_der_certificate(self) -> None:
        output = b"""issuer=CN=(STAGING) Pretend Intermediate
notAfter=May  1 12:00:00 2033 GMT
X509v3 Subject Alternative Name:
    DNS:app.example.com, DNS:www.example.com
"""

        def runner(command, **kwargs):
            self.assertEqual(command[:2], ["openssl", "x509"])
            self.assertEqual(kwargs["input"], b"fixture-der")
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=output,
                stderr=b"",
            )

        certificate = inspect_certificate_der(b"fixture-der", 5, runner=runner)

        self.assertIn("STAGING", certificate.issuer)
        self.assertEqual(
            certificate.sans,
            ("app.example.com", "www.example.com"),
        )
        self.assertEqual(len(certificate.fingerprint_sha256), 64)


if __name__ == "__main__":
    unittest.main()
