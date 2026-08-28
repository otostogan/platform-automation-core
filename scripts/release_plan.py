#!/usr/bin/env python3
"""Validate a release tag and export version-derived artifact metadata."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml


SEMVER_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\." r"(?:0|[1-9][0-9]*)\." r"(?:0|[1-9][0-9]*)"
)


@dataclass(frozen=True)
class ReleasePlan:
    version: str
    tag: str
    runtime_artifact: str
    collection_artifact: str
    release_notes: str

    def github_environment(self) -> Mapping[str, str]:
        return {"RELEASE_VERSION": self.version}


def build_release_plan(
    tag: str,
    runtime_version: str,
    collection_version: str,
    repository_root: Path,
) -> ReleasePlan:
    if SEMVER_PATTERN.fullmatch(runtime_version) is None:
        raise ValueError(f"runtime version is not semantic: {runtime_version}")

    expected_tag = f"v{runtime_version}"
    if tag != expected_tag:
        raise ValueError(f"tag {tag} does not match runtime {runtime_version}")

    if collection_version != runtime_version:
        raise ValueError(
            "collection version "
            f"{collection_version} does not match runtime {runtime_version}"
        )

    release_notes = f"docs/releases/{expected_tag}.md"
    if not (repository_root / release_notes).is_file():
        raise ValueError(f"release notes do not exist: {release_notes}")

    return ReleasePlan(
        version=runtime_version,
        tag=expected_tag,
        runtime_artifact=(
            f"platform_automation_runtime-{runtime_version}-py3-none-any.whl"
        ),
        collection_artifact=f"otostogan-platform-{runtime_version}.tar.gz",
        release_notes=release_notes,
    )


def write_github_environment(plan: ReleasePlan, destination: Path) -> None:
    with destination.open(mode="a", encoding="utf-8") as output:
        for name, value in plan.github_environment().items():
            output.write(f"{name}={value}\n")


def main() -> int:
    from platform_automation import __version__

    repository_root = Path.cwd()
    galaxy = yaml.safe_load(
        (repository_root / "galaxy.yml").read_text(encoding="utf-8")
    )
    plan = build_release_plan(
        tag=os.environ["RELEASE_TAG"],
        runtime_version=__version__,
        collection_version=str(galaxy["version"]),
        repository_root=repository_root,
    )
    write_github_environment(plan, Path(os.environ["GITHUB_ENV"]))
    print(
        f"release plan validated: {plan.tag}, "
        f"{plan.runtime_artifact}, {plan.collection_artifact}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
