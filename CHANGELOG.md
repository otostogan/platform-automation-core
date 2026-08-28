# Changelog

All notable changes to Platform Automation Core are documented here.

The project follows Semantic Versioning after the initial private migration
release.

## [Unreleased]

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
