import unittest

from scripts.boundary_policy import SECRET_PATTERNS

# Every vector is assembled from fragments so this file does not trip the
# very check it exercises. boundary_policy.py splits the tailnet prefix for
# the same reason.
AWS_NAME = b"aws_secret_access_key"
AWS_VALUE = b"wJalrXUtnFEMI" + b"/K7MDENG/bPxRfiCYEXAMPLEKEY"
AGE_KEY = b"AGE-SECRET-" + b"KEY-1" + b"Q" * 25
GITHUB_TOKEN = b"ghp" + b"_" + b"abcdefghijklmnopqrstuvwxyz012345"
TAILSCALE_KEY = b"tskey" + b"-auth-" + b"abcdefghijklmnopqrstuvwxyz"
PRIVATE_KEY = b"BEGIN " + b"OPENSSH PRIVATE " + b"KEY"


def flagged(content: bytes) -> bool:
    return any(pattern.search(content) for pattern in SECRET_PATTERNS)


class SecretPatternTest(unittest.TestCase):
    def test_a_real_shaped_aws_secret_is_caught(self) -> None:
        for line in (
            AWS_NAME.upper() + b"=" + AWS_VALUE,
            AWS_NAME + b': "' + AWS_VALUE + b'"',
            AWS_NAME + b"= " + AWS_VALUE,
        ):
            with self.subTest(line=line):
                self.assertTrue(flagged(line))

    def test_naming_the_argument_is_not_a_secret(self) -> None:
        """boto3 takes it as a keyword, so source has to write the name."""
        for line in (
            AWS_NAME + b"=secret,",
            b'values.get("' + AWS_NAME.upper() + b'")',
            AWS_NAME.upper() + b"=readsecret",
            AWS_NAME + b"=credentials[1]",
        ):
            with self.subTest(line=line):
                self.assertFalse(flagged(line))

    def test_the_other_credential_shapes_still_catch(self) -> None:
        for line in (PRIVATE_KEY, AGE_KEY, GITHUB_TOKEN, TAILSCALE_KEY):
            with self.subTest(line=line):
                self.assertTrue(flagged(line))


if __name__ == "__main__":
    unittest.main()
