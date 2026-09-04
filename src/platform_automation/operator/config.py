"""What the operator's machine knows that no repository can carry.

An application repository does not know where the company's infrastructure
lives on this machine, and the infrastructure does not know which
workstation holds which key directory. Both are facts about the operator, so
they live in the operator's config — as paths only, never as secrets — and
the console fills that config itself whenever it learns something.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml

CONFIG_RELATIVE_PATH = Path(".config/platform/config.yml")


@dataclass(frozen=True)
class Infra:
    path: Path
    keys: Optional[Path] = None

    @property
    def name(self) -> str:
        return self.path.name


def config_path(home: Optional[Path] = None) -> Path:
    return (Path.home() if home is None else home) / CONFIG_RELATIVE_PATH


def load_config(home: Optional[Path] = None) -> dict[str, Any]:
    try:
        with config_path(home).open(encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return document if isinstance(document, dict) else {}


def save_config(document: dict, home: Optional[Path] = None) -> Path:
    path = config_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return path


def _expand(value: Any) -> Optional[Path]:
    return Path(str(value)).expanduser() if isinstance(value, str) and value else None


def infras(home: Optional[Path] = None) -> list:
    """Every registered infrastructure; the old single ``infra:`` key still counts."""
    document = load_config(home)
    found = []
    for entry in document.get("infras") or []:
        if isinstance(entry, dict) and _expand(entry.get("path")):
            found.append(Infra(_expand(entry["path"]), _expand(entry.get("keys"))))
        elif isinstance(entry, str) and entry:
            found.append(Infra(_expand(entry)))
    legacy = _expand(document.get("infra"))
    if legacy and all(item.path != legacy for item in found):
        found.append(Infra(legacy, _expand(document.get("keys"))))
    return found


def infra_root(home: Optional[Path] = None) -> Optional[Path]:
    """The first registered infrastructure, for callers that need exactly one."""
    known = infras(home)
    return known[0].path if known else None


def register_infra(
    path: Path, keys: Optional[Path] = None, home: Optional[Path] = None
) -> bool:
    """Remember an infrastructure; True when it was not known before."""
    path = path.expanduser().resolve()
    document = load_config(home)
    entries = [e for e in (document.get("infras") or []) if isinstance(e, (dict, str))]
    for entry in entries:
        known = _expand(entry.get("path") if isinstance(entry, dict) else entry)
        if known is not None and known.resolve() == path:
            if keys is not None and isinstance(entry, dict) and not entry.get("keys"):
                entry["keys"] = str(keys)
                document["infras"] = entries
                save_config(document, home)
            return False
    entry = {"path": str(path)}
    if keys is not None:
        entry["keys"] = str(keys.expanduser())
    entries.append(entry)
    document["infras"] = entries
    document.pop("infra", None)
    document.pop("keys", None)
    save_config(document, home)
    return True


def forget_infra(path: Path, home: Optional[Path] = None) -> bool:
    path = path.expanduser().resolve()
    document = load_config(home)
    entries = document.get("infras") or []
    kept = [
        e
        for e in entries
        if (
            _expand(e.get("path") if isinstance(e, dict) else e) or Path("/nonexistent")
        ).resolve()
        != path
    ]
    if len(kept) == len(entries):
        return False
    document["infras"] = kept
    save_config(document, home)
    return True


def infra_for_host(host: str, home: Optional[Path] = None) -> Optional[Infra]:
    """The infrastructure whose inventory names this host — by name or address.

    An application never records its infrastructure; it records where it
    deploys. That is enough: the host appears in exactly one inventory.
    """
    from .context import read_hosts

    wanted = host.rstrip(".").lower()
    for infra in infras(home):
        for entry in read_hosts(infra.path):
            candidates = {entry.name.lower()}
            if entry.address:
                candidates.add(entry.address.rstrip(".").lower())
            if wanted in candidates:
                return infra
    return None
