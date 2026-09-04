"""Operator preferences that no repository can carry.

An application repository cannot know where the company's infrastructure
repository lives on this machine; that is a fact about the operator, so it
lives in the operator's config. Absent config means the checks that need it
are skipped and say so — never guessed from neighbouring directories.
"""

from pathlib import Path
from typing import Any, Optional

import yaml

CONFIG_RELATIVE_PATH = Path(".config/platform/config.yml")


def config_path(home: Optional[Path] = None) -> Path:
    return (Path.home() if home is None else home) / CONFIG_RELATIVE_PATH


def load_config(home: Optional[Path] = None) -> dict[str, Any]:
    path = config_path(home)

    try:
        with path.open(encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}

    return document if isinstance(document, dict) else {}


def infra_root(home: Optional[Path] = None) -> Optional[Path]:
    value = load_config(home).get("infra")
    return Path(str(value)).expanduser() if isinstance(value, str) and value else None
