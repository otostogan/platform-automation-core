"""Choose a rollback target the way the ledger will judge it.

Only a release with a successful deployment and a passed healthcheck can be
returned to; the current one is not a target; and a release still
``deploying`` blocks everything, because the outcome of its migration is
unknown. The console applies the same rules before it asks the host, so the
list it shows contains only what the host would accept.
"""

import re
from dataclasses import dataclass
from typing import Optional

VERSION_IN_TAG = re.compile(r"(v\d+\.\d+\.\d+)$")


@dataclass(frozen=True)
class Target:
    release_tag: str
    updated_at: str
    migration: str  # of that release's own deployment record


def history(document: dict) -> list:
    entries = document.get("history")
    return (
        [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    )


def blocked_by_deploying(document: dict) -> bool:
    return any(entry.get("status") == "deploying" for entry in history(document))


def current_tag(document: dict) -> Optional[str]:
    current = document.get("current")
    return current.get("release_tag") if isinstance(current, dict) else None


def eligible_targets(document: dict) -> list:
    """Newest first, one per tag, the same acceptance rule as the host's."""
    seen = set()
    targets = []
    for entry in history(document):
        tag = entry.get("release_tag")
        if not tag or tag in seen or tag == current_tag(document):
            continue
        if entry.get("status") != "deployed" or entry.get("healthcheck") != "succeeded":
            continue
        seen.add(tag)
        targets.append(
            Target(
                tag,
                str(entry.get("updated_at") or ""),
                str(entry.get("migration") or ""),
            )
        )
    return targets


def migrations_between(document: dict, target_tag: str) -> list:
    """Release tags whose deployment ran a migration after the target's — what a rollback cannot undo."""
    entries = list(reversed(history(document)))  # oldest first
    index = next(
        (i for i, e in enumerate(entries) if e.get("release_tag") == target_tag), None
    )
    if index is None:
        return []
    return [
        str(e.get("release_tag"))
        for e in entries[index + 1 :]
        if e.get("migration") == "succeeded"
    ]


def version_of(release_tag: str) -> Optional[str]:
    """``lab-v0.1.7`` → ``v0.1.7``: what the Deploy workflow's ``ref`` takes."""
    match = VERSION_IN_TAG.search(release_tag)
    return match.group(1) if match else None
