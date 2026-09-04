import subprocess
import tempfile
import unittest
from pathlib import Path

from platform_automation.operator.secrets import (
    SecretsError,
    encrypt_env,
    filter_for_manifest,
    is_stale,
    parse_dotenv,
    pull_env,
    render_dotenv,
    sync,
)

MANIFEST = "api_version: platform/v1\nproject: my-app\nenvironment: {env}\ndatabase:\n    mode: {mode}\n"


class ParseDotenvTest(unittest.TestCase):
    def test_quotes_comments_and_export_are_handled(self) -> None:
        values = parse_dotenv(
            '# comment\n\nAPI_TOKEN=plain\nexport SESSION_SECRET="with spaces and #hash"\n'
            "SINGLE='quoted'\nEMPTY=\n"
        )

        self.assertEqual(
            values,
            {
                "API_TOKEN": "plain",
                "SESSION_SECRET": "with spaces and #hash",
                "SINGLE": "quoted",
                "EMPTY": "",
            },
        )

    def test_bad_names_and_lines_are_refused_with_the_line_number(self) -> None:
        with self.assertRaises(SecretsError) as raised:
            parse_dotenv("GOOD=1\n1BAD=2\n")
        self.assertIn("line 2", str(raised.exception))
        with self.assertRaises(SecretsError):
            parse_dotenv("no equals sign\n")

    def test_render_round_trips(self) -> None:
        self.assertEqual(render_dotenv({"A": "1", "B": "x y"}), "A=1\nB=x y\n")


class FilterTest(unittest.TestCase):
    def test_platform_database_drops_the_url_the_host_provides(self) -> None:
        values, dropped = filter_for_manifest(
            {"DATABASE_URL": "postgres://local", "API_TOKEN": "x"},
            {"database": {"mode": "docker"}},
        )

        self.assertEqual(values, {"API_TOKEN": "x"})
        self.assertEqual(dropped, ("DATABASE_URL",))

    def test_external_database_requires_the_url(self) -> None:
        with self.assertRaises(SecretsError):
            filter_for_manifest({"API_TOKEN": "x"}, {"database": {"mode": "external"}})
        values, dropped = filter_for_manifest(
            {"DATABASE_URL": "postgres://elsewhere"}, {"database": {"mode": "external"}}
        )
        self.assertEqual(dropped, ())
        self.assertIn("DATABASE_URL", values)


class EncryptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "deploy").mkdir()
        (self.root / "deploy/platform.lab.yml").write_text(
            MANIFEST.format(env="lab", mode="docker"), encoding="utf-8"
        )
        (self.root / ".env.lab").write_text(
            'API_TOKEN="real"\nDATABASE_URL=postgres://local\n', encoding="utf-8"
        )
        self.calls = []

    def fake_sops(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == "sops" and "--encrypt" in command:
            source = Path(kwargs["cwd"]) / command[-1]
            plaintext = source.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(
                command, 0, f"# ciphertext of: {plaintext}".encode(), b""
            )
        if command[0] == "sops" and "--decrypt" in command:
            return subprocess.CompletedProcess(command, 0, b"API_TOKEN=real\n", b"")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def test_encrypts_the_filtered_plaintext_under_the_ciphertext_name(self) -> None:
        result = encrypt_env(self.root, "lab", runner=self.fake_sops)

        command = self.calls[0][0]
        self.assertEqual(
            command[:5], ["sops", "--input-type", "dotenv", "--output-type", "yaml"]
        )
        self.assertIn("deploy/secrets.lab.sops.yaml", command)
        written = (self.root / "deploy/secrets.lab.sops.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("API_TOKEN=real", written)
        self.assertNotIn("DATABASE_URL", written)
        self.assertEqual(result.dropped, ("DATABASE_URL",))
        self.assertFalse(
            list(self.root.glob(".env-*")), "temporary plaintext must not survive"
        )

    def test_stale_detection_and_sync_skip(self) -> None:
        encrypt_env(self.root, "lab", runner=self.fake_sops)
        self.assertFalse(is_stale(self.root, "lab"))

        import os, time

        later = time.time() + 5
        os.utime(self.root / ".env.lab", (later, later))
        self.assertTrue(is_stale(self.root, "lab"))

        results = sync(self.root, stale_only=True, runner=self.fake_sops)
        self.assertTrue(results[0].written)
        results = sync(self.root, stale_only=True, runner=self.fake_sops)
        self.assertEqual(results[0].reason, "ciphertext is up to date")

    def test_pull_writes_a_private_dotenv(self) -> None:
        encrypt_env(self.root, "lab", runner=self.fake_sops)

        path = pull_env(self.root, "lab", runner=self.fake_sops)

        self.assertEqual(path.read_text(encoding="utf-8"), "API_TOKEN=real\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_env_file_is_named(self) -> None:
        with self.assertRaises(SecretsError) as raised:
            encrypt_env(self.root, "production", runner=self.fake_sops)
        self.assertIn(".env.production", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
