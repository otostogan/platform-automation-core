import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from platform_automation.runtime_secrets import (  # noqa: E402
    RuntimeSecretsError,
    decrypt_sops_document,
    materialize_env_secrets,
    render_env_file,
)


class RuntimeSecretsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.encrypted_file = self.base / "secrets.sops.yaml"
        self.age_key_file = self.base / "age.key"
        self.runtime_root = self.base / "runtime"

        self.encrypted_file.write_text(
            "encrypted fixture",
            encoding="utf-8",
        )
        self.age_key_file.write_text(
            "AGE-SECRET-KEY-TEST",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def successful_result(
        self,
        document: dict,
    ) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(document).encode("utf-8"),
            stderr=b"",
        )

    @patch("platform_automation.runtime_secrets.subprocess.run")
    def test_materializes_private_env_file(
        self,
        run: unittest.mock.Mock,
    ) -> None:
        run.return_value = self.successful_result(
            {
                "APP_SECRET": "line1\nline2",
                "QUOTE": 'say "hello"',
                "ENABLED": True,
                "PORT": 3000,
            }
        )

        destination = materialize_env_secrets(
            encrypted_file=self.encrypted_file,
            project="example",
            environment="lab",
            release_id="a" * 32,
            runtime_root=self.runtime_root,
            age_key_file=self.age_key_file,
            sops_executable=Path("/fake/sops"),
        )

        self.assertEqual(
            destination.read_text(encoding="utf-8"),
            'APP_SECRET="line1\\nline2"\n'
            'ENABLED="true"\n'
            'PORT="3000"\n'
            'QUOTE="say \\"hello\\""\n',
        )
        self.assertEqual(
            stat.S_IMODE(destination.stat().st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(destination.parent.stat().st_mode),
            0o700,
        )

        command = run.call_args.args[0]
        options = run.call_args.kwargs

        self.assertNotIn("line1", " ".join(command))
        self.assertEqual(
            options["env"]["SOPS_AGE_KEY_FILE"],
            str(self.age_key_file),
        )

    def test_rejects_nested_env_value(self) -> None:
        with self.assertRaisesRegex(
            RuntimeSecretsError,
            "must be scalar",
        ):
            render_env_file(
                {
                    "NESTED": {
                        "VALUE": "secret",
                    }
                }
            )

    def test_rejects_invalid_env_key(self) -> None:
        with self.assertRaisesRegex(
            RuntimeSecretsError,
            "invalid environment variable name",
        ):
            render_env_file(
                {
                    "INVALID-KEY": "secret",
                }
            )

    @patch("platform_automation.runtime_secrets.subprocess.run")
    def test_hides_sops_error_output(
        self,
        run: unittest.mock.Mock,
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout=b"",
            stderr=b"plaintext must never reach platform logs",
        )

        with self.assertRaisesRegex(
            RuntimeSecretsError,
            "^SOPS decryption failed$",
        ) as context:
            decrypt_sops_document(
                self.encrypted_file,
                age_key_file=self.age_key_file,
                sops_executable=Path("/fake/sops"),
            )

        self.assertNotIn(
            "plaintext",
            str(context.exception),
        )

    @patch("platform_automation.runtime_secrets.subprocess.run")
    def test_rejects_symlink_runtime_directory(
        self,
        run: unittest.mock.Mock,
    ) -> None:
        run.return_value = self.successful_result(
            {
                "APP_SECRET": "secret",
            }
        )

        outside = self.base / "outside"
        outside.mkdir()
        self.runtime_root.mkdir()
        (self.runtime_root / "example").symlink_to(
            outside,
            target_is_directory=True,
        )

        with self.assertRaisesRegex(
            RuntimeSecretsError,
            "runtime secrets path is unsafe",
        ):
            materialize_env_secrets(
                encrypted_file=self.encrypted_file,
                project="example",
                environment="lab",
                release_id="b" * 32,
                runtime_root=self.runtime_root,
                age_key_file=self.age_key_file,
                sops_executable=Path("/fake/sops"),
            )


if __name__ == "__main__":
    unittest.main()
