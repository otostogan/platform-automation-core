# External consumer fixture

This directory models repositories that consume an immutable core
release without importing its source tree.

- `company-infra` owns inventory and company-specific variables.
- `application` owns its `platform/v1` manifest, Compose model, image source,
  and a thin trigger workflow.

The sample values use documentation-only addresses and domains. The workflows
pin `v0.4.1`. The core repository is public, so reading its releases needs no
credential.

The fixture demonstrates the steady-state bus-factor policy: application
bundles require two unique SOPS age recipients, the host independently enforces
the same minimum, and Tailscale enrollment reads a controller-local auth-key
file. Follow `docs/runbook.md` before enabling those settings in a real company
repository.
