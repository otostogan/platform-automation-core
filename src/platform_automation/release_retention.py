#!/usr/bin/env python3

"""Reclaim disk space held by superseded releases.

Retention is expressed as an offline rollback depth: the number of distinct
container image digests that must stay resolvable on the host without any
network access. Bundles are content addressed, so several release records can
share one staged directory, and several environments can share one image.
Nothing is removed until every ledger on the host has been consulted.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .release_ledger import (
    ReleaseLedgerError,
    find_latest_deployed_release,
    list_release_records,
    validate_ledger_identity,
)

DEFAULT_DOCKER_EXECUTABLE = Path("/usr/bin/docker")
DEFAULT_RETAINED_IMAGES = 5
MAX_RETAINED_IMAGES = 50

BUNDLE_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RELEASE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class ReleaseRetentionError(RuntimeError):
    pass


def resolve_retained_images(manifest: dict[str, Any]) -> int:
    """Read the retention depth an application asked for."""
    configured = manifest.get("deployment", {}).get("retained_releases")

    if configured is None:
        return DEFAULT_RETAINED_IMAGES

    if isinstance(configured, bool) or not isinstance(configured, int):
        raise ReleaseRetentionError("retained_releases must be an integer")

    if configured < 1 or configured > MAX_RETAINED_IMAGES:
        raise ReleaseRetentionError(
            f"retained_releases must be between 1 and {MAX_RETAINED_IMAGES}"
        )

    return configured


def release_order(record: dict[str, Any]) -> tuple[str, str, str]:
    return (
        record["updated_at"],
        record["created_at"],
        record["release_id"],
    )


def successful_releases(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return rollback candidates, newest first."""
    return sorted(
        (
            record
            for record in records
            if record["status"] == "deployed"
            and record["healthcheck"]["status"] == "succeeded"
        ),
        key=release_order,
        reverse=True,
    )


def retained_image_digests(
    records: list[dict[str, Any]],
    retained_images: int,
) -> list[str]:
    """Select the newest distinct image digests worth keeping on disk."""
    digests: list[str] = []

    for record in successful_releases(records):
        digest = record["image"]["digest"]

        if digest in digests:
            continue

        digests.append(digest)

        if len(digests) >= retained_images:
            break

    current = find_latest_deployed_release(records)

    if current is not None and current["image"]["digest"] not in digests:
        digests.append(current["image"]["digest"])

    return digests


def discover_ledger_scopes(projects_root: Path) -> list[tuple[str, str]]:
    """List project/environment ledgers, skipping anything unrecognised.

    Retention must never abort because an unrelated directory appeared under
    the projects root, so this discovery is deliberately tolerant.
    """
    if projects_root.is_symlink() or not projects_root.is_dir():
        return []

    scopes: list[tuple[str, str]] = []

    for project_path in sorted(projects_root.iterdir()):
        if project_path.is_symlink() or not project_path.is_dir():
            continue

        for environment_path in sorted(project_path.iterdir()):
            if environment_path.is_symlink() or not environment_path.is_dir():
                continue

            try:
                validate_ledger_identity(
                    project_path.name,
                    environment_path.name,
                )
            except ReleaseLedgerError:
                continue

            if not (environment_path / "ledger").is_dir():
                continue

            scopes.append(
                (
                    project_path.name,
                    environment_path.name,
                )
            )

    return scopes


def protected_image_digests(
    projects_root: Path,
    project: str,
    environment: str,
    retained_images: int,
) -> set[str]:
    """Collect digests every ledger on the host still depends on.

    Two environments of the same application routinely run the same image, so
    a digest may only be removed once no scope wants it.
    """
    protected: set[str] = set()

    for scope_project, scope_environment in discover_ledger_scopes(projects_root):
        # An unreadable ledger cannot be reasoned about, so a ledger error
        # here aborts retention instead of licensing a removal.
        records = list_release_records(
            projects_root,
            scope_project,
            scope_environment,
        )

        if not records:
            continue

        if (scope_project, scope_environment) == (project, environment):
            depth = retained_images
        else:
            depth = DEFAULT_RETAINED_IMAGES

        protected.update(retained_image_digests(records, depth))

    return protected


def plan_retention(
    records: list[dict[str, Any]],
    retained_images: int,
    protected_digests: set[str],
    current_release_id: str,
) -> dict[str, Any]:
    """Decide what may be reclaimed for one project/environment."""
    kept_digests = set(retained_image_digests(records, retained_images))

    retained_bundles = {
        record["bundle"]["digest"]
        for record in successful_releases(records)
        if record["image"]["digest"] in kept_digests
    }

    references: dict[str, str] = {}

    for record in records:
        references.setdefault(
            record["image"]["digest"],
            record["image"]["reference"],
        )

    removable_images = [
        reference
        for digest, reference in sorted(references.items())
        if digest not in protected_digests
    ]

    return {
        "retained_images": retained_images,
        "retained_image_digests": sorted(kept_digests),
        "retained_bundle_digests": retained_bundles,
        "retained_release_id": current_release_id,
        "removable_images": removable_images,
    }


def remove_directory_tree(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ReleaseRetentionError(f"refusing to remove unexpected path: {path}")

    shutil.rmtree(path)


def prune_staged_bundles(
    releases_root: Path,
    project: str,
    environment: str,
    retained_bundle_digests: set[str],
    warnings: list[str],
) -> list[str]:
    parent = releases_root / project / environment / "bundles"

    if parent.is_symlink() or not parent.is_dir():
        return []

    removed: list[str] = []

    for path in sorted(parent.iterdir()):
        if not BUNDLE_DIGEST_PATTERN.fullmatch(path.name):
            continue

        if path.name in retained_bundle_digests:
            continue

        try:
            remove_directory_tree(path)
        except (OSError, ReleaseRetentionError) as error:
            warnings.append(f"bundle {path.name} not removed: {error}")
            continue

        removed.append(path.name)

    return removed


def prune_runtime_secrets(
    runtime_secrets_root: Path,
    project: str,
    environment: str,
    current_release_id: str,
    warnings: list[str],
) -> list[str]:
    """Drop decrypted material for every release that is not running.

    Rollback and reboot recovery both re-materialise secrets from the staged
    bundle, so a decrypted copy is a liability rather than a dependency.
    """
    parent = runtime_secrets_root / project / environment

    if parent.is_symlink() or not parent.is_dir():
        return []

    removed: list[str] = []

    for path in sorted(parent.iterdir()):
        if not RELEASE_ID_PATTERN.fullmatch(path.name):
            continue

        if path.name == current_release_id:
            continue

        try:
            remove_directory_tree(path)
        except (OSError, ReleaseRetentionError) as error:
            warnings.append(f"runtime secrets {path.name} not removed: {error}")
            continue

        removed.append(path.name)

    return removed


def remove_image(
    image: str,
    docker_executable: Path = DEFAULT_DOCKER_EXECUTABLE,
    runner=subprocess.run,
) -> None:
    """Remove exactly one image reference.

    Never reach for `docker system prune`: the proxy stack and its ACME
    companion share this daemon.
    """
    try:
        result = runner(
            [str(docker_executable), "image", "rm", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseRetentionError(
            "Docker image removal could not be executed"
        ) from error

    if result.returncode != 0:
        raise ReleaseRetentionError("Docker image removal failed")


def prune_images(
    removable_images: list[str],
    docker_executable: Path,
    image_remover,
    warnings: list[str],
) -> list[str]:
    removed: list[str] = []

    for image in removable_images:
        try:
            image_remover(
                image=image,
                docker_executable=docker_executable,
            )
        except ReleaseRetentionError as error:
            # A running container still holding the image lands here.
            warnings.append(f"image {image} not removed: {error}")
            continue

        removed.append(image)

    return removed


def apply_retention(
    records: list[dict[str, Any]],
    project: str,
    environment: str,
    current_release_id: str,
    retained_images: int,
    projects_root: Path,
    releases_root: Path,
    runtime_secrets_root: Path,
    docker_executable: Path,
    image_remover=remove_image,
) -> dict[str, Any]:
    warnings: list[str] = []

    protected = protected_image_digests(
        projects_root,
        project,
        environment,
        retained_images,
    )
    plan = plan_retention(
        records,
        retained_images,
        protected,
        current_release_id,
    )

    removed_bundles = prune_staged_bundles(
        releases_root,
        project,
        environment,
        plan["retained_bundle_digests"],
        warnings,
    )
    removed_secrets = prune_runtime_secrets(
        runtime_secrets_root,
        project,
        environment,
        current_release_id,
        warnings,
    )
    removed_images = prune_images(
        plan["removable_images"],
        docker_executable,
        image_remover,
        warnings,
    )

    return {
        "retained_images": plan["retained_images"],
        "retained_image_digests": plan["retained_image_digests"],
        "removed_bundles": removed_bundles,
        "removed_runtime_secrets": removed_secrets,
        "removed_images": removed_images,
        "warnings": warnings,
    }
