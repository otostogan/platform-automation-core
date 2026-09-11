# {{company}} platform infrastructure

Inventory and convergence for the {{company}} platform hosts.

This repository owns **what the hosts are**: inventory, host variables and
controller-local secret paths. The automation comes from the pinned
`otostogan.platform` collection (`requirements.yml`); applications live in
their own repositories.

What must never be committed here: private keys, age identities, auth keys,
decrypted values. Controller-local material is referenced **by path** in the
git-ignored `local-secrets.yml` files; the `.yml.example` next to each one
documents the shape.

Every procedure — bootstrap, converge, readiness, adding a host or an
operator, updating the core — is a guided flow in the core's
`docs/handbook.html`. `platform doctor` in this directory checks the
workstation before any of them.

- `inventory/bootstrap.yml` — once per host, over the provider's public SSH.
- `inventory/hosts.yml` — steady state, over the tailnet as `ops`.
- `docs/RECIPIENTS.md` — public SOPS recipients every application encrypts to.
- `docs/TAILNET.md` — tag scheme, and what to edit when something is added.
