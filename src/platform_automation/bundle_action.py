#!/usr/bin/env python3

import argparse
import sys
from pathlib import Path

from .build_bundle import BundleError, create_bundle
from .validate_manifest import load_yaml


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a validated platform deployment bundle.",
    )
    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Application platform/v1 manifest path.",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Destination deployment bundle path.",
    )
    parser.add_argument(
        "--github-output",
        required=True,
        type=Path,
        help="GitHub Actions output file.",
    )
    parser.add_argument(
        "--minimum-age-recipients",
        type=int,
        default=1,
        help="Minimum number of unique SOPS age recipients.",
    )

    return parser.parse_args()


def require_safe_output(name: str, value: str) -> str:
    if not value:
        raise ValueError(f"{name} cannot be empty")

    if "\n" in value or "\r" in value:
        raise ValueError(f"{name} cannot contain line breaks")

    return value


def write_github_outputs(
    output_file: Path,
    values: dict[str, str],
) -> None:
    with output_file.open(
        "a",
        encoding="utf-8",
    ) as output:
        for name, value in values.items():
            output.write(f"{name}={require_safe_output(name, value)}\n")


def prepare_bundle(
    manifest_path: Path,
    bundle_path: Path,
    github_output: Path,
    minimum_age_recipients: int = 1,
) -> dict[str, str]:
    bundle_digest = create_bundle(
        manifest_path=manifest_path,
        output_path=bundle_path,
        minimum_age_recipients=minimum_age_recipients,
    )
    manifest = load_yaml(manifest_path)

    values = {
        "bundle_path": str(bundle_path.resolve()),
        "bundle_digest": bundle_digest,
        "project": str(manifest["project"]),
        "environment": str(manifest["environment"]),
        "healthcheck_host": str(manifest["domains"][0]["host"]),
        "healthcheck_path": str(manifest["service"]["healthcheck"]["path"]),
    }

    write_github_outputs(
        github_output,
        values,
    )
    return values


def main() -> int:
    arguments = parse_arguments()

    try:
        values = prepare_bundle(
            manifest_path=arguments.manifest,
            bundle_path=arguments.output,
            github_output=arguments.github_output,
            minimum_age_recipients=arguments.minimum_age_recipients,
        )
    except (BundleError, OSError, ValueError) as error:
        print(
            f"prepare bundle error: {error}",
            file=sys.stderr,
        )
        return 1

    if arguments.minimum_age_recipients < 2:
        print(
            "BUS FACTOR WARNING: only one SOPS age recipient is required; "
            "set --minimum-age-recipients 2 after adding a recovery recipient.",
            file=sys.stderr,
        )

    print(
        "Prepared deployment bundle: "
        f"{values['project']}/{values['environment']} "
        f"sha256:{values['bundle_digest']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
