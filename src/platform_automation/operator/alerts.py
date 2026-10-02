"""Set up Telegram delivery for a host's alerts.

The bot token is the one secret here. It is typed once, written to a 0600
file beside the host's other keys and never printed, logged or kept by the
console; the inventory gets the path to that file and the chat's number.
Telegram puts the token in the request path, so every error raised from
here is built without the URL.
"""

import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

API = "https://api.telegram.org"
TOKEN_PATTERN = re.compile(r"^[0-9]{5,}:[A-Za-z0-9_-]{30,}$")
CHAT_PATTERN = re.compile(r"^-?[0-9]{1,20}$")
TOKEN_FILE = "telegram-alerts.token"
TOKEN_KEY = "observability_alerts_telegram_token_source"
CHAT_KEY = "observability_alerts_telegram_chat_id"
COMMENT = (
    "# Telegram delivery of this host's alerts: the bot token is a controller-local\n"
    "# file, the chat is the numeric id the bot writes to.\n"
)


class AlertsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Chat:
    id: str
    title: str
    kind: str


def valid_token(value: str) -> bool:
    return bool(TOKEN_PATTERN.match(value.strip()))


def valid_chat(value: str) -> bool:
    return bool(CHAT_PATTERN.match(value.strip()))


def masked(method: str) -> str:
    """The request as it may be shown: the token never is."""
    return f"{API}/bot<token>/{method}"


def call(token: str, method: str, parameters: Optional[dict] = None, opener=None):
    """One Bot API call; returns ``result``. Errors never carry the URL."""
    opener = opener or urllib.request.urlopen
    data = urllib.parse.urlencode(parameters).encode() if parameters else None
    request = urllib.request.Request(f"{API}/bot{token}/{method}", data=data)
    try:
        with opener(request, timeout=20) as response:
            document = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            description = json.loads(error.read().decode("utf-8")).get("description")
        except (ValueError, OSError):
            description = None
        raise AlertsError(
            f"Telegram refused {method}: {description or f'HTTP {error.code}'}"
        ) from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        reason = getattr(error, "reason", None) or type(error).__name__
        raise AlertsError(f"Telegram is unreachable for {method}: {reason}") from None
    if not isinstance(document, dict) or not document.get("ok"):
        description = document.get("description") if isinstance(document, dict) else ""
        raise AlertsError(f"Telegram refused {method}: {description or 'no reason'}")
    return document.get("result")


def bot_name(token: str, opener=None) -> str:
    result = call(token, "getMe", opener=opener)
    return "@" + str((result or {}).get("username") or "unknown")


def recent_chats(token: str, opener=None) -> list:
    """Chats that wrote to the bot lately, newest first, each once."""
    updates = call(token, "getUpdates", opener=opener) or []
    found: dict = {}
    for update in reversed(updates):
        if not isinstance(update, dict):
            continue
        for key in ("message", "channel_post", "my_chat_member", "edited_message"):
            chat = (update.get(key) or {}).get("chat")
            if not isinstance(chat, dict) or "id" not in chat:
                continue
            identifier = str(chat["id"])
            title = (
                chat.get("title")
                or " ".join(
                    part
                    for part in (chat.get("first_name"), chat.get("last_name"))
                    if part
                )
                or chat.get("username")
                or identifier
            )
            found.setdefault(
                identifier, Chat(identifier, str(title), str(chat.get("type", "")))
            )
    return list(found.values())


def send_test(token: str, chat: str, host: str, opener=None) -> None:
    call(
        token,
        "sendMessage",
        {
            "chat_id": chat,
            "text": f"🟢 platform: alerts of {host} will arrive in this chat.",
        },
        opener=opener,
    )


def write_token(path: Path, token: str) -> None:
    """Atomically, mode 0600, in a 0700 directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".token.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(token.strip() + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_token(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token if valid_token(token) else None


def set_values(text: str, token_path: str, chat: str) -> str:
    """``local-secrets.yml`` with the two keys set; every other line untouched."""
    wanted = {TOKEN_KEY: token_path, CHAT_KEY: f'"{chat}"'}
    lines = text.split("\n") if text else []
    seen = set()
    for index, line in enumerate(lines):
        for key, value in wanted.items():
            if re.match(rf"^{key}\s*:", line):
                lines[index] = f"{key}: {value}"
                seen.add(key)
    missing = [key for key in wanted if key not in seen]
    result = "\n".join(lines)
    if missing:
        if result and not result.endswith("\n"):
            result += "\n"
        if result.strip():
            result += "\n"
        if not seen:
            result += COMMENT
        result += "".join(f"{key}: {wanted[key]}\n" for key in missing)
    try:
        document = yaml.safe_load(result)
    except yaml.YAMLError as error:
        raise AlertsError(f"the edit would break local-secrets.yml: {error}") from error
    if (
        not isinstance(document, dict)
        or document.get(TOKEN_KEY) != token_path
        or document.get(CHAT_KEY) != chat
    ):
        raise AlertsError("the edit did not set both keys in local-secrets.yml")
    return result


def token_location(document) -> Optional[str]:
    """Where the token file goes, written the way the inventory writes paths.

    The path already configured wins; otherwise the file sits beside the
    host's age key. ``None`` when the host's secrets name neither.
    """
    if not isinstance(document, dict):
        return None
    current = document.get(TOKEN_KEY)
    if isinstance(current, str) and current:
        return current
    key = document.get("secrets_age_key_source")
    if isinstance(key, str) and "/" in key:
        return key.rsplit("/", 1)[0] + "/" + TOKEN_FILE
    return None
