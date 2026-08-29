# Changelog

All notable changes to Platform Automation Core are documented here.

The project follows Semantic Versioning after the initial private migration
release.

## [Unreleased]

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
