import io
import json
import tempfile
import unittest
from pathlib import Path

from platform_automation.backup_offsite import (  # noqa: E402
    CARD_SUFFIX,
    DUMP_SUFFIX,
    OffsiteError,
    download_backup,
    load_credentials,
    load_offsite_config,
    object_key,
    offsite_status,
    pending_uploads,
    read_operator_credentials,
    upload_backups,
)

OLDER = "20260828T120000Z-schedule"
NEWER = "20260829T140530Z-operator"

SETTINGS = {
    "enabled": True,
    "endpoint": "https://s3.eu-central-1.example.invalid",
    "region": "eu-central-1",
    "bucket": "aiworldhub-platform-backups",
    "prefix": "medkeep-host",
}


class FakeClient:
    """Answer the three calls the platform makes, and record them."""

    def __init__(self, existing=None, fail=None) -> None:
        self.objects: dict[str, bytes] = dict(existing or {})
        self.fail = fail
        self.puts: list[str] = []
        self.list_calls: list[str] = []

    def list_objects_v2(self, **arguments):
        if self.fail == "list":
            raise RuntimeError("AccessDenied")

        self.list_calls.append(arguments["Prefix"])
        keys = [k for k in sorted(self.objects) if k.startswith(arguments["Prefix"])]

        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def put_object(self, **arguments):
        if self.fail == "put":
            raise RuntimeError("AccessDenied")

        self.puts.append(arguments["Key"])
        self.objects[arguments["Key"]] = arguments["Body"].read()

        return {}

    def get_object(self, **arguments):
        if self.fail == "get" or arguments["Key"] not in self.objects:
            raise RuntimeError("NoSuchKey")

        return {"Body": io.BytesIO(self.objects[arguments["Key"]])}


class ObjectKeyTest(unittest.TestCase):
    def test_host_prefix_comes_first(self) -> None:
        """One IAM condition then covers every project on that host."""
        self.assertEqual(
            object_key("medkeep-host", "health-client", "lab", "dump.age"),
            "medkeep-host/health-client/lab/dump.age",
        )

    def test_a_trailing_slash_does_not_double(self) -> None:
        self.assertEqual(
            object_key("medkeep-host/", "app", "lab", "x"),
            "medkeep-host/app/lab/x",
        )


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.config = self.base / "offsite.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write(self, document) -> Path:
        self.config.write_text(json.dumps(document))
        return self.config

    def test_an_absent_file_means_local_only(self) -> None:
        """No configuration is a supported answer, not an error."""
        self.assertIsNone(load_offsite_config(self.base / "missing.json"))

    def test_disabled_means_local_only(self) -> None:
        self.assertIsNone(
            load_offsite_config(self.write({**SETTINGS, "enabled": False}))
        )

    def test_enabled_settings_load(self) -> None:
        self.assertEqual(
            load_offsite_config(self.write(SETTINGS))["bucket"],
            SETTINGS["bucket"],
        )

    def test_incomplete_settings_are_refused(self) -> None:
        for field in ("bucket", "region", "prefix"):
            with self.subTest(field=field):
                document = {**SETTINGS, field: ""}

                with self.assertRaises(OffsiteError):
                    load_offsite_config(self.write(document))

    def test_a_traversing_prefix_is_refused(self) -> None:
        with self.assertRaises(OffsiteError):
            load_offsite_config(self.write({**SETTINGS, "prefix": "../other"}))

    def test_an_invalid_bucket_name_is_refused(self) -> None:
        with self.assertRaises(OffsiteError):
            load_offsite_config(self.write({**SETTINGS, "bucket": "Not_A_Bucket"}))

    def test_credentials_load_from_an_env_file(self) -> None:
        path = self.base / "s3.env"
        path.write_text("AWS_ACCESS_KEY_ID=AKIA\nAWS_SECRET_ACCESS_KEY=secret\n")

        self.assertEqual(load_credentials(path), ("AKIA", "secret"))

    def test_incomplete_credentials_are_refused(self) -> None:
        path = self.base / "s3.env"
        path.write_text("AWS_ACCESS_KEY_ID=AKIA\n")

        with self.assertRaises(OffsiteError):
            load_credentials(path)


class ReconcileTest(unittest.TestCase):
    def test_a_dump_and_its_card_are_both_pending(self) -> None:
        self.assertEqual(
            pending_uploads([NEWER], set()),
            [(NEWER, f"{NEWER}{DUMP_SUFFIX}"), (NEWER, f"{NEWER}{CARD_SUFFIX}")],
        )

    def test_what_is_already_there_is_not_resent(self) -> None:
        remote = {f"{NEWER}{DUMP_SUFFIX}", f"{NEWER}{CARD_SUFFIX}"}

        self.assertEqual(pending_uploads([NEWER], remote), [])

    def test_a_half_uploaded_backup_finishes_next_time(self) -> None:
        """An upload that failed yesterday is retried, not lost."""
        remote = {f"{NEWER}{DUMP_SUFFIX}"}

        self.assertEqual(
            pending_uploads([NEWER], remote),
            [(NEWER, f"{NEWER}{CARD_SUFFIX}")],
        )


class UploadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.directory = self.base / "backups"
        self.directory.mkdir()
        self.config = self.base / "offsite.json"
        self.config.write_text(json.dumps(SETTINGS))
        self.credentials = self.base / "s3.env"
        self.credentials.write_text(
            "AWS_ACCESS_KEY_ID=AKIA\nAWS_SECRET_ACCESS_KEY=secret\n"
        )
        self.client = FakeClient()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def seed(self, *stamps: str) -> None:
        for stamp in stamps:
            (self.directory / f"{stamp}{DUMP_SUFFIX}").write_bytes(b"encrypted")
            (self.directory / f"{stamp}{CARD_SUFFIX}").write_text("{}")

    def upload(self, config_path=None):
        return upload_backups(
            project="health-client",
            environment="lab",
            directory=self.directory,
            config_path=config_path or self.config,
            credentials_path=self.credentials,
            client_factory=lambda *_: self.client,
        )

    def test_no_configuration_uploads_nothing(self) -> None:
        self.seed(NEWER)

        result = self.upload(config_path=self.base / "missing.json")

        self.assertEqual(result["state"], "not-configured")
        self.assertEqual(self.client.puts, [])

    def test_enabling_offsite_carries_existing_dumps_up(self) -> None:
        """The first run reconciles; it does not only send the newest."""
        self.seed(OLDER, NEWER)

        result = self.upload()

        self.assertEqual(len(result["uploaded"]), 4)
        self.assertIn(
            f"medkeep-host/health-client/lab/{OLDER}{DUMP_SUFFIX}",
            self.client.puts,
        )

    def test_a_second_run_sends_only_what_is_new(self) -> None:
        self.seed(OLDER)
        self.upload()
        self.client.puts.clear()

        self.seed(NEWER)
        result = self.upload()

        self.assertEqual(len(result["uploaded"]), 2)
        for key in self.client.puts:
            self.assertIn(NEWER, key)

    def test_listing_stays_inside_the_host_prefix(self) -> None:
        self.seed(NEWER)
        self.upload()

        self.assertEqual(
            self.client.list_calls,
            ["medkeep-host/health-client/lab/"],
        )

    def test_a_denied_upload_is_loud(self) -> None:
        self.seed(NEWER)
        self.client.fail = "put"

        with self.assertRaises(OffsiteError):
            self.upload()

    def test_a_denied_listing_is_loud(self) -> None:
        self.seed(NEWER)
        self.client.fail = "list"

        with self.assertRaises(OffsiteError):
            self.upload()


class StatusTest(UploadTest):
    def status(self):
        return offsite_status(
            project="health-client",
            environment="lab",
            directory=self.directory,
            config_path=self.config,
            credentials_path=self.credentials,
            client_factory=lambda *_: self.client,
        )

    def test_everything_uploaded_reads_as_current(self) -> None:
        self.seed(NEWER)
        self.upload()

        self.assertEqual(self.status()["state"], "current")

    def test_a_host_that_stopped_uploading_reads_as_behind(self) -> None:
        """The failure discovered too late is named, not left to a log."""
        self.seed(OLDER, NEWER)

        result = self.status()

        self.assertEqual(result["state"], "behind")
        self.assertEqual(result["not_uploaded"], [OLDER, NEWER])

    def test_an_unreachable_bucket_is_unknown_not_current(self) -> None:
        self.seed(NEWER)
        self.client.fail = "list"

        self.assertEqual(self.status()["state"], "error")


class OperatorCredentialsTest(unittest.TestCase):
    def test_a_reader_key_arrives_on_stdin(self) -> None:
        stream = io.StringIO(
            "AWS_ACCESS_KEY_ID=AKIAREAD\nAWS_SECRET_ACCESS_KEY=readsecret\n"
        )

        self.assertEqual(
            read_operator_credentials(stream),
            ("AKIAREAD", "readsecret"),
        )

    def test_bytes_on_stdin_are_accepted(self) -> None:
        stream = io.BytesIO(
            b"AWS_ACCESS_KEY_ID=AKIAREAD\nAWS_SECRET_ACCESS_KEY=readsecret\n"
        )

        self.assertEqual(
            read_operator_credentials(stream)[0],
            "AKIAREAD",
        )

    def test_an_empty_stream_is_refused(self) -> None:
        with self.assertRaises(OffsiteError):
            read_operator_credentials(io.StringIO(""))


class DownloadTest(UploadTest):
    def test_a_dump_and_card_come_back(self) -> None:
        self.seed(NEWER)
        self.upload()

        for suffix in (DUMP_SUFFIX, CARD_SUFFIX):
            (self.directory / f"{NEWER}{suffix}").unlink()

        fetched = download_backup(
            project="health-client",
            environment="lab",
            stamp=NEWER,
            directory=self.directory,
            credentials=("AKIAREAD", "readsecret"),
            config_path=self.config,
            client_factory=lambda *_: self.client,
        )

        self.assertEqual(len(fetched), 2)
        self.assertTrue((self.directory / f"{NEWER}{DUMP_SUFFIX}").is_file())
        self.assertEqual(
            (self.directory / f"{NEWER}{DUMP_SUFFIX}").stat().st_mode & 0o777,
            0o600,
        )

    def test_a_missing_object_is_loud(self) -> None:
        with self.assertRaises(OffsiteError):
            download_backup(
                project="health-client",
                environment="lab",
                stamp=NEWER,
                directory=self.directory,
                credentials=("AKIAREAD", "readsecret"),
                config_path=self.config,
                client_factory=lambda *_: self.client,
            )


if __name__ == "__main__":
    unittest.main()
