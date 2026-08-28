# Changelog

All notable changes to Platform Automation Core are documented here.

The project follows Semantic Versioning after the initial private migration
release.

## [Unreleased]

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
