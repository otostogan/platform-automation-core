"""Basic-auth files for the proxy, from the manifest and the environment secrets.

The manifest names a user and the *name* of the variable that holds the
password; the value comes from the decrypted environment file the release
already has, and only its hash reaches the disk. Nothing here ever puts the
value into a message: an error names the variable, never what it held.
"""

import base64
import hashlib
import re
import secrets
from pathlib import Path
from typing import Any

from .domains import web_domains  # noqa: F401  (documents the sibling module)

ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CRYPT_ALPHABET = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
SHA512_ROUNDS = 5000  # glibc/musl default; nginx accepts any


class HtpasswdError(ValueError):
    pass


# ---------------------------------------------------------------- sha512-crypt


def _b64_from_24bit(b2: int, b1: int, b0: int, size: int) -> str:
    value = (b2 << 16) | (b1 << 8) | b0
    out = []
    for _ in range(size):
        out.append(CRYPT_ALPHABET[value & 0x3F])
        value >>= 6
    return "".join(out)


def sha512_crypt(password: str, salt: str = None) -> str:
    """``$6$<salt>$<hash>`` exactly as crypt(3) computes it (Drepper's SHA-crypt)."""
    if salt is None:
        salt = "".join(secrets.choice(CRYPT_ALPHABET) for _ in range(16))
    if not re.fullmatch(r"[./0-9A-Za-z]{1,16}", salt):
        raise HtpasswdError("invalid crypt salt")
    key = password.encode("utf-8")
    salt_bytes = salt.encode("utf-8")

    digest_b = hashlib.sha512(key + salt_bytes + key).digest()
    a = hashlib.sha512()
    a.update(key + salt_bytes)
    length = len(key)
    while length > 64:
        a.update(digest_b)
        length -= 64
    a.update(digest_b[:length])
    bits = len(key)
    while bits > 0:
        a.update(digest_b if bits & 1 else key)
        bits >>= 1
    digest_a = a.digest()

    dp = hashlib.sha512(key * len(key)).digest()
    p = (dp * (len(key) // 64 + 1))[: len(key)]
    ds = hashlib.sha512(salt_bytes * (16 + digest_a[0])).digest()
    s = (ds * (len(salt_bytes) // 64 + 1))[: len(salt_bytes)]

    c = digest_a
    for i in range(SHA512_ROUNDS):
        h = hashlib.sha512()
        h.update(p if i & 1 else c)
        if i % 3:
            h.update(s)
        if i % 7:
            h.update(p)
        h.update(c if i & 1 else p)
        c = h.digest()

    order = [
        (0, 21, 42),
        (22, 43, 1),
        (44, 2, 23),
        (3, 24, 45),
        (25, 46, 4),
        (47, 5, 26),
        (6, 27, 48),
        (28, 49, 7),
        (50, 8, 29),
        (9, 30, 51),
        (31, 52, 10),
        (53, 11, 32),
        (12, 33, 54),
        (34, 55, 13),
        (56, 14, 35),
        (15, 36, 57),
        (37, 58, 16),
        (59, 17, 38),
        (18, 39, 60),
        (40, 61, 19),
        (62, 20, 41),
    ]
    encoded = "".join(_b64_from_24bit(c[x], c[y], c[z], 4) for x, y, z in order)
    encoded += _b64_from_24bit(0, 0, c[63], 2)
    return f"$6${salt}${encoded}"


# ----------------------------------------------------------- environment file


def read_env_file(path: Path) -> dict:
    """``KEY="value"`` lines as the runtime writes them; unquoted values are accepted too."""
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY_PATTERN.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = (
                value[1:-1]
                .replace("\\t", "\t")
                .replace("\\r", "\r")
                .replace("\\n", "\n")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
        values[key] = value
    return values


# ------------------------------------------------------------------ rendering


def auth_domains(manifest: dict[str, Any]) -> list:
    return [d for d in manifest["domains"] if isinstance(d.get("auth"), dict)]


def render_htpasswd_files(manifest: dict[str, Any], secrets: dict) -> dict:
    """host → ``user:$6$…`` for every domain with an ``auth`` block."""
    files = {}
    for domain in auth_domains(manifest):
        auth = domain["auth"]
        variable = auth["password_env"]
        if variable not in secrets:
            raise HtpasswdError(
                f"domain {domain['host']}: password variable {variable} is not in the"
                " environment secrets of this release"
            )
        password = secrets[variable]
        if not password:
            raise HtpasswdError(
                f"domain {domain['host']}: password variable {variable} is empty"
            )
        files[domain["host"]] = f"{auth['username']}:{sha512_crypt(password)}\n"
    return files


def htpasswd_files_for_release(
    manifest: dict[str, Any], runtime_secrets_path: Path
) -> dict:
    if not auth_domains(manifest):
        return {}
    if runtime_secrets_path is None:
        raise HtpasswdError(
            "domains declare auth but no environment secrets are available"
        )
    return render_htpasswd_files(manifest, read_env_file(Path(runtime_secrets_path)))


def content_fingerprint(content: str) -> str:
    """What ownership metadata may record: never the line itself."""
    return (
        base64.b16encode(hashlib.sha256(content.encode("utf-8")).digest()[:8])
        .decode()
        .lower()
    )
