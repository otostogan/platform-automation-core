"""Stable access to contracts packaged inside the runtime wheel."""

from importlib.resources import files
from typing import Any


def contract_path(filename: str) -> Any:
    """Return an importlib resource supporting the pathlib read API."""

    return files("platform_automation.contracts").joinpath(filename)
