# Platform Automation Core

One server, several applications, several environments — deployed without a
human opening an SSH session, and recoverable by someone who did not build it.

This repository is the versioned automation core: reusable Ansible roles that
bring a host to a described state, a runtime that deploys applications through
the stable `platform/v1` contract, and the release machinery that ships both.
Company inventories, credentials, domains, and application source code live in
separate private repositories and never enter this one.

## What it gives you

- **Deploy a version.** One dispatch in GitHub. Nothing in the path asks a
  person to log in to the server.
- **Know what is running.** One command reports the current release, whether it
  is healthy, when the last dump was taken, and when a restore from that dump
  was last *proven* — not assumed.
- **Go back.** The same dispatch with an earlier version; or, when GitHub or the
  registry cannot be reached, one command on the server using what is already
  on its disk.
- **Give and take away access.** One line carrying a public key in the
  infrastructure repository. The host's authorized keys are rewritten whole, so
  a removed key stops working instead of lingering at the end of a file.

## Rules that do not bend

Each one prevents a specific failure, and each is enforced by code rather than
by remembering:

| Rule | What it prevents |
| --- | --- |
| A deployment resolves to an image digest; a moving tag is refused | "the same version" quietly becoming different code tomorrow |
| Decrypted secrets exist only in `tmpfs`, never on disk | a seized or stolen disk carrying plaintext |
| The minimum count of unique SOPS age recipients is checked on every path — build, deploy, rollback, reboot recovery | one lost key making every encrypted value unrecoverable |
| CI connects as `deploy` and may run exactly `deploy`, `status`, `rollback` | a compromised pipeline turning into host administration |
| Convergence refuses to run unless it arrives as `ops` over the tailnet | a host being configured from an unverified network path |
| Incoming traffic is denied by default; only TCP 80 and 443 are public | the port that password guessing starts on being reachable at all |
| Release records are appended and never rewritten | a rollback target that can no longer be reconstructed |
| Version pins move by hand, as a reviewed change | an unreviewed update reaching every host at once |

## Where to start

[`docs/handbook.html`](docs/handbook.html) is an interactive, step-by-step guide
to operating the platform, written in Russian for the operators who run it:
guided flows for each routine task, a reference for every manifest field,
inventory variable and CLI flag, and a register of the claims that have not yet
been verified in practice. It is a single self-contained file — open it in a
browser, no build step and no network.

[`docs/runbook.md`](docs/runbook.md) defines the operational handoff, the
recovery controls, and the bus-factor drill.

## Status

`v0.15.1` is the current installable release. Consumers pin the release, verify
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
