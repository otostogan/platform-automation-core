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

`v0.29.0` is the current installable release. Consumers pin the release, verify
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

## Workstation console

The same wheel, with the `operator` extra, is the console an operator or a
developer runs on their own machine. One binary, `platform`; what it offers
depends on the directory it is started in.

Install it once, pinned to the release the infrastructure pins:

```bash
python3 -m venv ~/.local/share/platform-console
~/.local/share/platform-console/bin/pip install \
  "platform-automation-runtime[operator] @ https://github.com/otostogan/platform-automation-core/releases/download/v0.29.0/platform_automation_runtime-0.29.0-py3-none-any.whl"
mkdir -p ~/.local/bin
ln -sf ~/.local/share/platform-console/bin/platform ~/.local/bin/platform
```

`~/.local/bin` must be on the `PATH` — on macOS it usually is not; add
`export PATH="$HOME/.local/bin:$PATH"` to the shell profile. `sops` and
`age` must be on it as well: the pre-commit hook of an application encrypts
`.env.<environment>` with them. `platform doctor` in any
directory prints the console's version first and ends with `all checks
passed` or the number of findings.

| Where it runs | What `platform` opens |
| --- | --- |
| Anywhere | `platform new company-infra` scaffolds an infrastructure repository; `platform infra` registers one on this machine |
| Infrastructure repository | hosts from `inventory/hosts.yml`, then: converge and readiness, bootstrap of a new host, backups and restore proofs, Grafana tunnel, Telegram alerts; `platform update` moves the core pin and converges |
| Application repository | environments from `deploy/platform.*.yml`, then: Deploy (through the application's own workflow), status and logs of the host, secrets push and pull, database tunnel, rollback; `platform new app` scaffolds the deploy files, `platform update` rewrites the platform-managed workflows and hooks |

The console holds no secrets — only paths to them — prints every command
before running it, and never commits: it writes files and names them. A
developer without access to the infrastructure repository still has the whole
build-and-deploy loop: validation, secrets push, Deploy. The actions that
reach the host over the tailnet (status, logs, backups, database, rollback)
need the `ops` key, which stays with the operator.

The handbook describes every action and its preconditions:
[`docs/handbook.html#/ref-console`](docs/handbook.html).

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
