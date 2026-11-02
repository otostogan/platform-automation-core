"""``platform verify-backup --from-offsite``: the dump is fetched before it is proven."""

import argparse
import contextlib
import io
import unittest
from pathlib import Path

from platform_automation import platform_cli
from platform_automation.backup_offsite import OffsiteError


def arguments(**overrides) -> argparse.Namespace:
    values = {
        "project": "example",
        "environment": "lab",
        "stamp": "20260101T000000Z-schedule",
        "from_offsite": True,
        "all": False,
        "json": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def run(namespace, downloader, token_stream):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = platform_cli.run_verify_backup(
            namespace,
            Path("/projects"),
            Path("/releases"),
            Path("/locks"),
            Path("/databases"),
            Path("/backups"),
            Path("/runtime"),
            Path("/age.key"),
            Path("sops"),
            Path("age"),
            Path("docker"),
            2,
            verifier=lambda **kwargs: {"stamp": "x", "query": "q", "result": "1"},
            offsite_config=Path("/offsite.json"),
            downloader=downloader,
            token_stream=token_stream,
        )
    return code, err.getvalue()


class VerifyFromOffsiteTest(unittest.TestCase):
    def test_the_parser_accepts_the_flag(self) -> None:
        parsed = platform_cli.parse_arguments(
            [
                "verify-backup",
                "--project",
                "p",
                "--environment",
                "lab",
                "--from",
                "s",
                "--from-offsite",
            ]
        )
        self.assertTrue(parsed.from_offsite)

    def test_without_a_stamp_nothing_is_fetched(self) -> None:
        calls = []
        code, err = run(
            arguments(stamp=None),
            lambda **kwargs: calls.append(kwargs),
            io.BytesIO(b""),
        )
        self.assertEqual(code, 1)
        self.assertIn("--from-offsite needs --from", err)
        self.assertEqual(calls, [])

    def test_the_download_takes_the_reader_key_from_stdin_and_names_the_stamp(
        self,
    ) -> None:
        seen = {}

        def downloader(**kwargs):
            seen.update(kwargs)
            raise OffsiteError("stop here: the rest needs a deployed release")

        stream = io.BytesIO(b"AWS_ACCESS_KEY_ID=reader\nAWS_SECRET_ACCESS_KEY=secret\n")
        code, err = run(arguments(), downloader, stream)

        self.assertEqual(code, 1)
        self.assertIn("verify-backup error: stop here", err)
        self.assertEqual(seen["stamp"], "20260101T000000Z-schedule")
        self.assertEqual(seen["credentials"], ("reader", "secret"))
        self.assertEqual(seen["directory"], Path("/backups/example/lab"))
        self.assertEqual(seen["config_path"], Path("/offsite.json"))


if __name__ == "__main__":
    unittest.main()
