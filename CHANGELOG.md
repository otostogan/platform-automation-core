# Changelog

All notable changes to Platform Automation Core are documented here.

The project follows Semantic Versioning after the initial private migration
release.

## [Unreleased]

## [0.14.0] - 2026-09-02

- Add `docs/handbook.html`: an interactive operator handbook in Russian, in a
  single self-contained file with no build step and no network access. Guided
  flows for each routine task — new host, new application, deploy, rollback,
  backups, database work, domains, operators, core update, reboot acceptance,
  incidents — where every step that gives a command also states how to tell it
  worked and what the known failures look like. Alongside them, a reference for
  the manifest, the Compose contract, inventory variables, keys, the release
  ledger, host layout and the CLI, a glossary, and a register of the claims
  that have not been verified in practice yet.
- Rewrite `README.md` around what the platform does and the rules that enforce
  it, rather than around the contents of the repository.
- Fix `docs/qa-digest.md`, which named the server tag `tag:platform-server`.
  No such tag exists: the inventory advertises `tag:server-platform`. An
  operator following the digest would have issued an auth key carrying a tag
  that no access rule matches, and the host would have joined the tailnet
  without the access it needs. Real endpoints and bucket names in the same file
  were replaced with placeholders so it passes the repository boundary check.


## [0.13.3] - 2026-08-31

- Fix `platform rotate-database-password`: psql interpolates variables for
  input it reads and not for `--command`, so the placeholder reached the
  server as literal SQL and every rotation failed. The statement now arrives
  on stdin, which also keeps the new password out of `argv` and therefore out
  of `ps`.
- Check the generated password against a conservative alphabet before it
  reaches SQL.

## [0.13.2] - 2026-08-31

- Fix `platform backup` raising `ValueError: flush of closed file`: writing the
  envelope means feeding age's stdin directly, and `communicate()` flushes a
  stdin that is already closed. Scheduled backups have failed since `0.13.0`;
  skip `0.13.0` and `0.13.1`.
- Derive the metadata sidecar's name instead of substituting a substring, so a
  path not ending in the dump suffix cannot resolve to the dump itself.
- Test the dump pipeline with real processes; three defects in a row hid
  behind doubles that did not model process semantics.

## [0.13.1] - 2026-08-31

- Fix `pg_restore` receiving an empty stream. A buffered reader's `seek`
  restores the Python-level position but not the file descriptor a child
  inherits, so every restore and verification in `0.13.0` failed. The envelope
  is now an offset and the caller seeks a raw descriptor. Skip `0.13.0`.
- Test the handover with a real subprocess rather than a fake runner.

## [0.13.0] - 2026-08-31

- Carry the metadata card inside the encrypted dump as well as beside it, in a
  streamable envelope that opens with one readable line. `0.8.0` claimed this
  and did not do it.
- Read both dump formats on restore; the embedded card wins over the sidecar.
- Add `platform rotate-database-password`: database first, stored credential
  second, application restart third, all under the project lock.

## [0.12.0] - 2026-08-31

- Report the loss window in `platform status`: the age of the newest dump
  against the configured cadence, and a plain statement when the schedule has
  stopped producing dumps at all.
- Add `platform backups`, listing each dump with its release, size, offsite state
  and whether a restore from it has been proven.
- Reorder the runbook for the moment it is opened: emergencies first, setup
  last, and the two things worth knowing stated up front.

## [0.11.0] - 2026-08-31

- Upload encrypted dumps to object storage, reconciling rather than pushing:
  each run sends whatever is missing remotely, so enabling offsite carries
  existing dumps up and a failed upload is retried next run. A failed upload
  fails the command; the dump still stays on disk.
- Add `platform restore --from-offsite`, taking reader credentials on stdin.
- Report offsite state in `platform status`: current, behind, or not
  configured.
- Install `python3-boto3` from apt rather than adding a bundled CLI.
- Narrow the `aws_secret_access_key` boundary rule from the name to a
  realistic value; the name is a boto3 keyword argument.

## [0.10.2] - 2026-08-30

- Fix the interval drop-in resetting the timer list twice. An empty assignment
  resets every monotonic timer, not the option it names, so the second reset
  discarded the interval and left a timer that fired once and never computed a
  next elapse. Skip `0.10.0` and `0.10.1` for scheduled backups.
- Remove `Persistent=true`, which only affects calendar timers and was inert
  here while the documentation claimed otherwise.

## [0.10.1] - 2026-08-30

- Fix the scheduled backup unit: it split the systemd instance with shell
  parameter expansion, but systemd resolves `${...}` in `ExecStart` before a
  shell sees it, so every scheduled run reached the CLI with an empty
  environment and exited 2. `platform backup` now accepts `--instance` and the
  unit runs without a shell. Skip `0.10.0` for scheduled backups.
- Refuse shell parameter expansion in unit `Exec*` lines in tests.

## [0.10.0] - 2026-08-30

- Reconcile a systemd backup timer on every deploy from
  `database.backup.interval_minutes`, and remove it when an application stops
  asking for backups. Units come from convergence; a deploy supplies only the
  cadence. A timer that cannot be written warns rather than failing a release
  that already serves traffic.
- Take a dump immediately before any migration, regardless of schedule. Unlike
  retention and scheduling this failure does stop the deploy: it is the safety
  net for a destructive step, not housekeeping.

## [0.9.0] - 2026-08-29

- Add `platform verify-backup`: restore a dump into a networkless throwaway
  container, run the declared `restore_validation` query, tear it down either
  way, and record the outcome. `platform status` now reports when a restore
  was last proven, not just when a dump was last taken.
- Add `platform restore`: replaces the live database from a dump, requires
  `--confirm-destructive`, refuses while a deployment holds the lock, and
  names a revision gap without refusing it.

## [0.8.1] - 2026-08-29

- Fix the age install: `unarchive` does not accept `checksum`, so every
  convergence to `0.8.0` failed. Fetch with `get_url` and verify before
  extracting, run only when the pinned version is absent, and clean up in an
  `always` block so a converged host reports no change. Skip `0.8.0`.

## [0.8.0] - 2026-08-29

- Add `platform backup`: `pg_dump` piped straight through age, encrypted to
  the application's own recipients with escrow, described by a metadata card
  written beside the dump and inside it, retained by `database.backup.retain`.
- Install a pinned `age` binary beside SOPS; a dump is an opaque stream, not a
  structured document.
- Refuse a `postgres_major` change on an existing volume, and refuse a deploy
  whose volume exists but whose credential has gone missing. Both would
  otherwise report success while leaving the application broken or the data
  ignored.

## [0.7.0] - 2026-08-29

- **Breaking:** `reusable-deploy.yml` requires `application_commit`, and
  builds the bundle from that commit. It previously used the calling
  workflow's branch while the image came from the requested revision, so a
  deployment could pair a new image with an older manifest and still report
  success.

## [0.6.0] - 2026-08-29

- Run the database a `mode: docker` application declares: a generated
  per-project Compose model, an image pinned by digest until the major
  changes, an internal-only network, a volume that survives everything, and
  a credential encrypted to the application's own recipient set with
  `DATABASE_URL` injected at deploy time.
- Order deploys database-first: unhealthy database, no migrations, no swap,
  no ledger record.
- Restore the database's tmpfs material during boot recovery without Docker.

## [0.5.0] - 2026-08-29

- Widen `database.postgres_major` from exactly 17 to the supported set 16-18.
- Add the `database.backup` contract object: `interval_minutes` (15-1440) and
  `retain` (1-100), required exactly when `backup_enabled` is true. Nothing
  implements it yet; the shape lands first so later phases stay additive.

## [0.4.2] - 2026-08-29

- Fetch a container image only when the host does not already hold that
  digest. `platform rollback` pulled unconditionally, so the path meant for an
  unreachable registry could not run without registry credentials.

## [0.4.1] - 2026-08-29

- Install `release_retention.py` on the host. The `platform_cli` role copies a
  named list of modules and this one was missing, so converging to `0.4.0`
  installed a CLI that cannot start. Skip `0.4.0`.
- Stop installing `bundle_action.py` on the host. It is a CI entry point that
  imports a module the host does not have; nothing on the host imported it.
- Add a structural test that keeps the role's module list, the runtime package,
  and the contract list in agreement.

## [0.4.0] - 2026-08-29

- Deploying a release tag that already had a record skipped starting
  containers while reporting that it had started them, so redeploying an
  earlier revision to roll back silently left the newer release running. The
  shortcut now applies only to the release that is already current, and every
  other deploy of a known tag becomes a new record that actually starts.
- Reclaim superseded releases after a successful deploy or rollback. Retention
  counts distinct image digests, never removes a digest another ledger on the
  host still wants, keeps decrypted secrets only for the running release, and
  can only warn.
- Add the optional `deployment.retained_releases` manifest field, defaulting
  to 5.
- Record release timestamps with millisecond precision so two deployments in
  the same second have a defined order.

## [0.3.4] - 2026-08-28

- Stop reporting a change when a task only refreshes the APT package index.
  Three roles did, so any convergence past the cache lifetime looked
  non-idempotent — the one signal the acceptance sequence depends on.

## [0.3.3] - 2026-08-28

- Document the readiness role: every variable, both phases, and the fact that
  the default phase reports success having skipped every post-convergence
  check. The parameter was previously undocumented.
- Name the phase in the runbook's acceptance sequence.
- Assert in tests that documented defaults match the role.

## [0.3.2] - 2026-08-28

- Validate the reboot recovery entrypoint by its installed path rather than by
  the module invocation systemd uses, which is not a path and made the role
  fail on every convergence.

## [0.3.1] - 2026-08-28

- Make the core release token optional now that the repository is public, so a
  consuming application needs no credential to fetch the runtime.
- Stop failing repository and release boundary scans when no private marker
  list is supplied; the generic patterns run regardless, and fork pull requests
  are never given secrets.
- Exclude build metadata from the collection artifact.

## [0.3.0] - 2026-08-28

- Keep sanitized consumer workflow fixtures pinned to the current core release.
- Enforce a configurable minimum of unique SOPS age recipients across bundle
  build, `platform deploy`, `platform rollback`, and boot secret recovery, so
  key escrow cannot lapse silently.
- Reject caller arguments that would lower the host recipient policy and
  non-positive policy values.
- Enroll hosts in Tailscale non-interactively from a controller-local
  pre-authorized key that never reaches inventory, the process list, or disk
  outside a transient root-owned runtime file.
- Add an operations runbook covering deployment, rollback, stuck `deploying`
  releases, `access_guard` recovery, reboot acceptance, and the recovery
  material a second operator needs.

## [0.2.0] - 2026-08-28

- Replace source-stored customer marker lists with a generic repository and
  release boundary policy.
- Remove legacy migration provenance and sanitize test-only SOPS metadata.
- Keep encrypted consumer SOPS files trackable while ignoring controller-local
  identities and plaintext files.
- Reject non-allowlisted FQDNs and global IPv6 addresses in the repository and
  built release artifacts.
- Accept private forbidden-marker lists from a CI secret or an untracked local
  file without committing customer identifiers to the core.
- Fail CI and release scans when the private marker list is missing, reject the
  fixed Tailscale IPv6 range even though it is ULA, and keep Markdown filenames
  outside FQDN detection.
- Derive release artifact and notes paths from the validated tag version so
  future stable releases do not require workflow edits.

## [0.1.0] - 2026-08-27

- Establish the private proprietary automation core with clean Git history.
- Publish the `platform-automation-runtime` wheel and
  `otostogan.platform` Ansible Collection.
- Preserve the `platform/v1`, bundle v1, and release v1 contracts, server CLI,
  nginx activation transaction, rollback, reboot recovery, and access guards.
- Add sanitized external consumer fixtures, centralized deployment automation,
  artifact boundary scanning, and reproducible checksums.
