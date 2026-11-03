# Consumer contract

Core and consumer repositories remain deliberately separate. A company
infrastructure repository owns inventory, host variables, controller-local
secret paths, and thin execution workflows. An application repository owns
its image, Compose model, `platform/v1` manifest, and SOPS-encrypted values.

Consumers pin the `v0.36.0` release, verify `SHA256SUMS`, install the
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

## Exports

With `database.mode: docker` the manifest may declare `exports`: SQL
queries the host runs on a calendar and mails as CSV. `exports.smtp` names
the relay (`host`, `port`, `from`, optional `username`) and the secret
variable that holds the password (`password_env`, read from the release's
encrypted environment secrets); `exports.jobs` lists up to twenty jobs,
each with a `name` (letters, digits and underscores), a systemd calendar
`schedule`, recipients `to`, the `query`, and optionally `subject` and
`compress`. The query runs as the application's role in a read-only
transaction with a ten-minute limit; the CSV is held in memory and sent
over TLS only. A deploy writes one `platform-export@<project>-<env>-<name>`
timer per job, checks the schedule with `systemd-analyze calendar`, and
removes timers of jobs that left the manifest; `retire` removes them all.
`platform export` runs a job now, `platform exports` and `platform status`
show how each last ran, and the **Export failed** alert fires when the last
run did not send.

