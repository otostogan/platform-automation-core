"""The public halves an infrastructure publishes for its applications.

``docs/RECIPIENTS.md`` in the infrastructure repository holds one line per
recipient in its first code block — ``host <age1…>`` and ``recovery
<age1…>``. Public keys, committed on purpose. Reading them here is what
lets ``new app`` fill both recipients without asking.
"""

import re
from pathlib import Path
from typing import Optional

RECIPIENTS_RELATIVE = Path("docs/RECIPIENTS.md")
LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_-]*)\s+(age1[0-9a-z]+)\s*$", re.M)


def read_recipients(infra_root: Path) -> dict:
    """Label → recipient from the first fenced block; empty when there is none."""
    path = infra_root / RECIPIENTS_RELATIVE
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    fence = re.search(r"```[^\n]*\n(.*?)```", text, re.S)
    if fence is None:
        return {}
    return {label: recipient for label, recipient in LINE.findall(fence.group(1))}


def host_recipient(recipients: dict, host_name: str) -> Optional[str]:
    """A per-host line wins; the generic ``host`` line covers a single-host infra."""
    return recipients.get(host_name) or recipients.get("host")


def recovery_recipient(recipients: dict) -> Optional[str]:
    return recipients.get("recovery")
