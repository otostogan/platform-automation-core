"""One export, end to end with a fake psql and a fake SMTP: query, CSV, mail, card."""

import gzip
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from platform_automation import export_runtime
from platform_automation.export_runtime import (
    MAX_ATTACHMENT_BYTES,
    MAX_CSV_BYTES,
    ExportRuntimeError,
    bare_query,
    build_message,
    count_rows,
    list_cards,
    read_card,
    render_script,
    run_export,
    run_query,
    send_message,
    smtp_password,
)

MOMENT = datetime(2026, 11, 3, 6, 0, tzinfo=timezone.utc)
CSV = b'company_id,contacts\r\n1,"[{""name"": ""A\nB""}]"\r\n2,[]\r\n'
SMTP = {
    "host": "smtp.example.invalid",
    "port": 587,
    "from": "reports@example.invalid",
    "password_env": "SMTP_PASSWORD",
}
JOB = {
    "name": "clients",
    "schedule": "Mon 06:00",
    "to": ["sales@example.invalid", "boss@example.invalid"],
    "query": "SELECT c.id AS company_id, '[]' AS contacts FROM companies c;",
}


def manifest(job=JOB):
    return {
        "database": {"mode": "docker"},
        "exports": {"smtp": SMTP, "jobs": [job]},
    }


class FakePopen:
    """psql as a process: a script in, bytes out, an exit code; may hang or be killed."""

    def __init__(self, output=b"", errors=b"", code=0, hang=False):
        self.output, self.errors, self.code, self.hang = output, errors, code, hang
        self.killed = False

    def __call__(self, command, stdin=None, stdout=None, stderr=None):
        import io as _io

        self.command = command
        self.stdin = _io.BytesIO()
        self.stdin.close = lambda: None  # keep what was written readable after close
        self.stdout = _io.BytesIO(self.output)
        self.stderr = _io.BytesIO(self.errors)
        self.returncode = None
        return self

    def wait(self, timeout=None):
        if self.hang and not self.killed:
            raise subprocess.TimeoutExpired(self.command, timeout)
        self.returncode = self.code
        return self.code

    def kill(self):
        self.killed = True


class QueryTest(unittest.TestCase):
    def test_trailing_semicolons_go_and_emptiness_is_refused(self) -> None:
        self.assertEqual(bare_query("SELECT 1 ;; \n"), "SELECT 1")
        with self.assertRaisesRegex(ExportRuntimeError, "empty"):
            bare_query(" ; ")

    def test_the_script_is_a_bounded_read_only_copy(self) -> None:
        script = render_script("SELECT 1;")
        self.assertIn("SET statement_timeout TO 600000;", script)
        self.assertIn("START TRANSACTION READ ONLY;", script)
        self.assertIn(
            "COPY (\nSELECT 1\n) TO STDOUT WITH (FORMAT csv, HEADER);", script
        )

    def test_psql_runs_in_the_container_with_the_password_in_its_environment(
        self,
    ) -> None:
        popen = FakePopen(CSV)

        data = run_query("app", "lab", "s3cret", "SELECT 1", Path("docker"), popen)

        self.assertEqual(data, CSV)
        command = popen.command
        self.assertEqual(command[:3], ["docker", "exec", "--interactive"])
        self.assertIn("PGPASSWORD=s3cret", command)
        self.assertIn("platform-db-app-lab-postgres-1", command)
        self.assertIn("ON_ERROR_STOP=1", command)
        self.assertNotIn("SELECT 1", " ".join(command))
        self.assertIn(b"READ ONLY", popen.stdin.getvalue())
        self.assertFalse(popen.killed)

    def test_a_failing_query_is_reported_with_psql_s_last_line(self) -> None:
        popen = FakePopen(
            b"", errors=b'ERROR:  relation "nope" does not exist\n', code=1
        )
        with self.assertRaisesRegex(
            ExportRuntimeError, "relation .nope. does not exist"
        ):
            run_query("app", "lab", "p", "SELECT * FROM nope", Path("docker"), popen)

    def test_a_result_past_the_cap_is_cut_off_while_it_streams(self) -> None:
        popen = FakePopen(b"x" * (MAX_CSV_BYTES + 1))
        with self.assertRaisesRegex(ExportRuntimeError, "larger than"):
            run_query("app", "lab", "p", "SELECT 1", Path("docker"), popen)
        self.assertTrue(popen.killed)
        # Nothing beyond the cap was kept: the read stopped at the first chunk over it.
        self.assertLessEqual(popen.stdout.tell(), MAX_CSV_BYTES + 64 * 1024)

    def test_a_query_that_never_ends_is_killed(self) -> None:
        popen = FakePopen(CSV, hang=True)
        with self.assertRaisesRegex(ExportRuntimeError, "did not finish"):
            run_query("app", "lab", "p", "SELECT 1", Path("docker"), popen)
        self.assertTrue(popen.killed)

    def test_a_huge_field_still_counts_as_one_row(self) -> None:
        data = b"a,b\r\n1," + b"x" * 200000 + b"\r\n"
        self.assertEqual(count_rows(data), 1)

    def test_rows_are_counted_as_csv_not_lines(self) -> None:
        self.assertEqual(count_rows(CSV), 2)
        self.assertEqual(count_rows(b"a,b\r\n"), 0)
        self.assertEqual(count_rows(b""), 0)


class MessageTest(unittest.TestCase):
    def test_the_csv_travels_as_a_named_attachment(self) -> None:
        message = build_message(JOB, SMTP, "app", "lab", CSV, 2, MOMENT)

        self.assertEqual(message["From"], "reports@example.invalid")
        self.assertEqual(message["To"], "sales@example.invalid, boss@example.invalid")
        self.assertEqual(message["Subject"], "app/lab: clients export")
        body, attachment = message.get_payload()
        self.assertIn("Rows: 2", body.get_content())
        self.assertEqual(attachment.get_filename(), "clients-20261103.csv")
        self.assertEqual(attachment.get_content_type(), "text/csv")
        self.assertEqual(attachment.get_payload(decode=True), CSV)

    def test_compress_gzips_and_a_subject_is_kept(self) -> None:
        job = {**JOB, "compress": True, "subject": "Weekly clients"}
        message = build_message(job, SMTP, "app", "lab", CSV, 2, MOMENT)

        self.assertEqual(message["Subject"], "Weekly clients")
        attachment = message.get_payload()[1]
        self.assertEqual(attachment.get_filename(), "clients-20261103.csv.gz")
        self.assertEqual(gzip.decompress(attachment.get_payload(decode=True)), CSV)

    def test_an_attachment_past_the_cap_is_refused(self) -> None:
        with self.assertRaisesRegex(ExportRuntimeError, "compress: true"):
            build_message(
                JOB, SMTP, "app", "lab", b"x" * (MAX_ATTACHMENT_BYTES + 1), 1, MOMENT
            )


class FakeSmtp:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.context = host, port, context
        self.events = []
        FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.events.append("quit")

    def ehlo(self):
        self.events.append("ehlo")

    def starttls(self, context=None):
        self.events.append(("starttls", context is not None))

    def login(self, username, password):
        self.events.append(("login", username, password))

    refused = {}

    def send_message(self, message):
        self.events.append(("send", message["To"]))
        return dict(self.refused)


class SendTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeSmtp.instances.clear()
        self.message = build_message(JOB, SMTP, "app", "lab", CSV, 2, MOMENT)

    def test_starttls_is_demanded_on_the_submission_port(self) -> None:
        send_message(self.message, SMTP, "pw", client=FakeSmtp)

        connection = FakeSmtp.instances[0]
        self.assertEqual(
            (connection.host, connection.port), ("smtp.example.invalid", 587)
        )
        self.assertEqual(
            connection.events,
            [
                "ehlo",
                ("starttls", True),
                "ehlo",
                ("login", "reports@example.invalid", "pw"),
                ("send", "sales@example.invalid, boss@example.invalid"),
                "quit",
            ],
        )

    def test_port_465_is_implicit_tls_and_username_may_differ(self) -> None:
        smtp = {**SMTP, "port": 465, "username": "apikey"}
        send_message(self.message, smtp, "pw", client=FakeSmtp)

        connection = FakeSmtp.instances[0]
        self.assertIsNotNone(connection.context)
        self.assertNotIn(("starttls", True), connection.events)
        self.assertIn(("login", "apikey", "pw"), connection.events)

    def test_partially_refused_recipients_are_a_failure_that_names_them(self) -> None:
        class Partial(FakeSmtp):
            refused = {"boss@example.invalid": (550, b"no such user")}

        with self.assertRaisesRegex(
            ExportRuntimeError, "refused recipient.*boss@example.invalid"
        ):
            send_message(self.message, SMTP, "pw", client=Partial)

    def test_a_refused_login_names_the_server_not_the_password(self) -> None:
        import smtplib

        class Refusing(FakeSmtp):
            def login(self, username, password):
                raise smtplib.SMTPAuthenticationError(535, b"no")

        with self.assertRaisesRegex(ExportRuntimeError, "refused the login") as caught:
            send_message(self.message, SMTP, "pw", client=Refusing)
        self.assertNotIn("pw", str(caught.exception))


class PasswordTest(unittest.TestCase):
    def test_the_variable_is_read_from_the_release_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / "app.env"
            env.write_text('SMTP_PASSWORD="s3cret"\nOTHER=1\n')
            self.assertEqual(smtp_password(env, "SMTP_PASSWORD"), "s3cret")
            with self.assertRaisesRegex(ExportRuntimeError, "SMTP_PASS is not in"):
                smtp_password(env, "SMTP_PASS")
        with self.assertRaisesRegex(ExportRuntimeError, "not on this host"):
            smtp_password(None, "SMTP_PASSWORD")


class RunExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.exports_root = root / "exports"
        self.env = root / "app.env"
        self.env.write_text('SMTP_PASSWORD="s3cret"\n')
        self.sent = []
        self.popen = FakePopen(CSV, errors=b"ERROR: boom\n")
        self.runner = subprocess.run

    def mailer(self, message, smtp, password):
        self.sent.append((message, smtp["host"], password))

    def export(self, name="clients", document=None):
        with mock.patch.object(
            export_runtime, "stored_database_password", return_value="dbpw"
        ):
            return run_export(
                document or manifest(),
                "app",
                "lab",
                name,
                {"release_tag": "lab-v1", "release_id": "r1"},
                self.env,
                Path("/databases"),
                self.exports_root,
                Path("/age.key"),
                Path("/sops"),
                Path("docker"),
                runner=self.runner,
                mailer=self.mailer,
                now=MOMENT,
                popen=self.popen,
            )

    def test_sends_the_mail_and_leaves_a_card(self) -> None:
        card = self.export()

        self.assertEqual(card["outcome"], "sent")
        self.assertEqual(card["rows"], 2)
        self.assertEqual(card["attachment"], "clients-20261103.csv")
        self.assertEqual(card["to"], JOB["to"])
        self.assertEqual(card["release_tag"], "lab-v1")
        self.assertEqual(card["schedule"], "Mon 06:00")
        message, host, password = self.sent[0]
        self.assertEqual((host, password), ("smtp.example.invalid", "s3cret"))
        self.assertEqual(message["To"], "sales@example.invalid, boss@example.invalid")
        stored = read_card(self.exports_root, "app", "lab", "clients")
        self.assertEqual(stored["outcome"], "sent")
        self.assertEqual(
            json.loads(
                (self.exports_root / "app" / "lab" / "clients.json").read_text()
            )["rows"],
            2,
        )
        self.assertEqual(
            (self.exports_root / "app" / "lab" / "clients.json").stat().st_mode & 0o777,
            0o600,
        )
        self.assertEqual(
            [c["name"] for c in list_cards(self.exports_root, "app", "lab")],
            ["clients"],
        )

    def test_a_failed_query_leaves_a_failed_card_and_sends_nothing(self) -> None:
        self.popen.code = 1
        with self.assertRaisesRegex(ExportRuntimeError, "boom"):
            self.export()
        self.assertEqual(self.sent, [])
        card = read_card(self.exports_root, "app", "lab", "clients")
        self.assertEqual(card["outcome"], "failed")
        self.assertIn("boom", card["error"])

    def test_an_unknown_job_and_a_foreign_database_are_refused(self) -> None:
        with self.assertRaisesRegex(ExportRuntimeError, "no export named nope"):
            self.export(name="nope")
        self.assertEqual(
            read_card(self.exports_root, "app", "lab", "nope")["outcome"], "failed"
        )
        foreign = manifest()
        foreign["database"]["mode"] = "external"
        with self.assertRaisesRegex(ExportRuntimeError, "mode: docker"):
            self.export(document=foreign)

    def test_a_missing_password_fails_before_the_database_is_asked(self) -> None:
        self.env.write_text("OTHER=1\n")
        with self.assertRaisesRegex(ExportRuntimeError, "SMTP_PASSWORD"):
            self.export()
        self.assertFalse(hasattr(self.popen, "command"))

    def test_a_relay_that_refuses_a_recipient_fails_the_run(self) -> None:
        def mailer(message, smtp, password):
            raise ExportRuntimeError(
                "the SMTP server x refused recipient(s): b@example.invalid"
            )

        self.mailer = mailer
        with self.assertRaisesRegex(ExportRuntimeError, "refused recipient"):
            self.export()
        self.assertEqual(
            read_card(self.exports_root, "app", "lab", "clients")["outcome"], "failed"
        )

    def test_an_unexpected_error_still_leaves_a_failed_card(self) -> None:
        def mailer(message, smtp, password):
            raise RuntimeError("socket exploded")

        self.mailer = mailer
        with self.assertRaisesRegex(ExportRuntimeError, "unexpected: socket exploded"):
            self.export()
        self.assertEqual(
            read_card(self.exports_root, "app", "lab", "clients")["outcome"], "failed"
        )


if __name__ == "__main__":
    unittest.main()
