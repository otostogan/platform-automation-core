#!/usr/bin/env python3

import re
from dataclasses import dataclass
from pathlib import Path

from .verify_bundle import (
    BundleVerificationError,
    VerifiedBundle,
    verify_bundle,
)


RELEASE_TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
IMMUTABLE_IMAGE_PATTERN = re.compile(
    r"^(?P<repository>.+)@" r"(?P<digest>sha256:[0-9a-f]{64})$"
)


class DeploymentRequestError(ValueError):
    pass


@dataclass(frozen=True)
class DeploymentRequest:
    project: str
    environment: str
    release_tag: str
    image: str
    image_repository: str
    image_digest: str
    bundle: VerifiedBundle


def validate_release_tag(release_tag: str) -> None:
    if not RELEASE_TAG_PATTERN.fullmatch(release_tag):
        raise DeploymentRequestError(f"invalid release tag: {release_tag}")


def parse_immutable_image(
    image: str,
    expected_repository: str,
) -> tuple[str, str]:
    match = IMMUTABLE_IMAGE_PATTERN.fullmatch(image)

    if match is None:
        raise DeploymentRequestError("image must use an immutable sha256 digest")

    repository = match.group("repository")
    digest = match.group("digest")

    if repository != expected_repository:
        raise DeploymentRequestError(
            "image repository does not match application manifest"
        )

    return repository, digest


def load_deployment_request(
    bundle_path: Path,
    project: str,
    environment: str,
    image: str,
    release_tag: str,
    minimum_age_recipients: int = 1,
) -> DeploymentRequest:
    validate_release_tag(release_tag)

    try:
        bundle = verify_bundle(
            bundle_path,
            minimum_age_recipients=minimum_age_recipients,
        )
    except BundleVerificationError as error:
        raise DeploymentRequestError(
            f"deployment bundle verification failed: {error}"
        ) from error

    if project != bundle.metadata["project"]:
        raise DeploymentRequestError(
            "requested project does not match deployment bundle"
        )

    if environment != bundle.metadata["environment"]:
        raise DeploymentRequestError(
            "requested environment does not match deployment bundle"
        )

    repository, digest = parse_immutable_image(
        image,
        bundle.manifest["image"]["repository"],
    )

    return DeploymentRequest(
        project=project,
        environment=environment,
        release_tag=release_tag,
        image=image,
        image_repository=repository,
        image_digest=digest,
        bundle=bundle,
    )
