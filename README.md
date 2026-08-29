# Platform Automation Core

Versioned automation core for preparing platform hosts and deploying
containerized applications through a stable `platform/v1` contract.

The repository owns reusable Ansible roles, the platform runtime, deployment
contracts, proxy assets, tests, and release automation. Company inventories,
credentials, domains, and application source code belong in separate private
consumer repositories.

The operational handoff, recovery controls, and bus-factor drill are defined in
[docs/runbook.md](docs/runbook.md).

## Status

`v0.4.1` is the current installable release. Consumers pin the release, verify
`SHA256SUMS`, and keep inventory and credentials in their own private
repositories.

The source is published so that consumers can install releases without
credentials and audit what runs on their hosts. It is not open source — see
[LICENSE](LICENSE).

## Ownership

Copyright © 2026 Otostogan. All rights reserved. See [LICENSE](LICENSE).

## Runtime package

The `platform-automation-runtime` wheel exposes the `platform_automation`
package and keeps the server-facing command stable:

```text
platform deploy
platform status
platform rollback
```

The versioned `platform/v1`, bundle v1, and release v1 schemas are packaged in
the wheel and are resolved through Python package resources. Runtime state
continues to live under `/opt/platform`, `/var/lib/platform`, and
`/run/platform`.

## Ansible Collection

Reusable host automation is distributed as `otostogan.platform`. Consumers
call the `preflight`, `bootstrap`, `converge`, and `readiness` collection
playbooks and refer to roles through names such as
`otostogan.platform.docker`. Real inventories and customer values are never
stored in this repository.

## Consumer boundary

Sanitized company-infrastructure and application fixtures live under
`examples/consumer`. They demonstrate immutable release installation,
inventory ownership, SOPS-only application secrets, and thin workflows without
granting consumers access to another company's data.

## Repository boundary checks

CI rejects credentials, local paths, public IP addresses, non-allowlisted
FQDNs, committed age recipients, and private inventory paths. Known historical
or customer-specific values are supplied as newline-separated entries in the
private `CORE_FORBIDDEN_MARKERS` GitHub Actions secret; local checks may instead
point `CORE_FORBIDDEN_MARKERS_FILE` at the ignored
`.core-forbidden-markers` newline-separated file. CI and release jobs are
fail-closed: they reject an empty marker list and report only the number loaded.
