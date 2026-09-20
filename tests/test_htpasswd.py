import tempfile
import unittest
from pathlib import Path

from platform_automation.htpasswd import (
    HtpasswdError,
    htpasswd_files_for_release,
    read_env_file,
    render_htpasswd_files,
    sha512_crypt,
)
from platform_automation.runtime_secrets import render_env_file

MANIFEST = {
    "service": {"web": "web"},
    "domains": [
        {"host": "app.example.invalid", "tls": True, "nginx": {}},
        {
            "host": "mail.app.example.invalid",
            "tls": True,
            "service": "mailpit",
            "auth": {"username": "team", "password_env": "MAILPIT_UI_PASSWORD"},
            "nginx": {},
        },
    ],
}


class Sha512CryptTest(unittest.TestCase):
    def test_matches_crypt3_for_a_known_vector(self) -> None:
        # openssl passwd -6 -salt saltsalt hunter2
        self.assertEqual(
            sha512_crypt("hunter2", "saltsalt"),
            "$6$saltsalt$8iYtNHxjWRl.NF6oNZ5tF.iKFlQREaXBLlSmZKP6dy9l5z3vsooWNW0/GZ6Nej73/TFug6pIPSqbJoCT6dfnj.",
        )

    def test_long_passwords_and_random_salts(self) -> None:
        # openssl passwd -6 -salt abcdefghijklmnop <96 x 'a'>
        self.assertEqual(
            sha512_crypt("a" * 96, "abcdefghijklmnop"),
            "$6$abcdefghijklmnop$1j58VLo/t4tafGbeuIuIZijLj/CP3IhVv.5Ijm3s027FbPwn5IjbrU1Y0JG1Ei3UUZFUo78JFRqnO7Sf3ICDT1",
        )


class RenderTest(unittest.TestCase):
    def test_env_file_written_by_the_runtime_reads_back(self) -> None:
        content = render_env_file({"MAILPIT_UI_PASSWORD": 'p"a\\ss\nw', "OTHER": 1})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.env"
            path.write_bytes(content)
            self.assertEqual(
                read_env_file(path), {"MAILPIT_UI_PASSWORD": 'p"a\\ss\nw', "OTHER": "1"}
            )

    def test_one_file_per_auth_domain_with_a_hash_only(self) -> None:
        files = render_htpasswd_files(MANIFEST, {"MAILPIT_UI_PASSWORD": "hunter2"})
        self.assertEqual(list(files), ["mail.app.example.invalid"])
        line = files["mail.app.example.invalid"]
        self.assertTrue(line.startswith("team:$6$"))
        self.assertNotIn("hunter2", line)

    def test_a_missing_or_empty_variable_is_named_without_its_value(self) -> None:
        with self.assertRaises(HtpasswdError) as caught:
            render_htpasswd_files(MANIFEST, {"OTHER": "s3cret-other"})
        self.assertIn("MAILPIT_UI_PASSWORD", str(caught.exception))
        self.assertNotIn("s3cret-other", str(caught.exception))
        with self.assertRaisesRegex(HtpasswdError, "is empty"):
            render_htpasswd_files(MANIFEST, {"MAILPIT_UI_PASSWORD": ""})

    def test_no_auth_means_no_files_and_no_secrets_needed(self) -> None:
        plain = {**MANIFEST, "domains": MANIFEST["domains"][:1]}
        self.assertEqual(htpasswd_files_for_release(plain, None), {})
        with self.assertRaisesRegex(HtpasswdError, "no environment secrets"):
            htpasswd_files_for_release(MANIFEST, None)
