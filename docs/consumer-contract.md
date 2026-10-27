# Consumer contract

Core and consumer repositories remain deliberately separate. A company
infrastructure repository owns inventory, host variables, controller-local
secret paths, and thin execution workflows. An application repository owns
its image, Compose model, `platform/v1` manifest, and SOPS-encrypted values.

Consumers pin the `v0.30.1` release, verify `SHA256SUMS`, install the
collection tarball, and invoke fully qualified playbooks. Application triggers
call the centrally owned reusable deployment workflow. No consumer imports a
checkout-relative Python path or receives the core Git history. The core is
public, so no credential is needed to read its releases.

See `examples/consumer` for sanitized repository fixtures.

Consumers must also adopt the recovery and access controls in
[`runbook.md`](runbook.md). In particular, application workflows and host
inventory both enforce two unique SOPS age recipients only after all encrypted
files have been re-keyed and independently recovery-tested.

## Database modes

The `platform/v1` manifest declares the application's database in one of
three modes. `docker`: the platform runs PostgreSQL, writes `DATABASE_URL`
into the runtime environment, takes a dump before every migration and on the
declared schedule, and can restore one. `external`: the application brings
its own `DATABASE_URL` in its encrypted secrets; the platform backs nothing
up. `none`: a static site or an application without a database. With
`mode: none` the `database` block carries only `mode` and
`backup_enabled: false`; `postgres_major`, `backup`, `resources` and the
top-level `restore_validation` are refused, `DATABASE_URL` is neither
required nor injected, no backup timer is reconciled, and `platform backup`,
`restore` and `verify-backup` refuse with the reason. A Compose file must
not reference `PLATFORM_DB_NETWORK` unless the mode is `docker`.
