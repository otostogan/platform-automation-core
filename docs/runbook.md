# Platform operations runbook

This runbook is the minimum handoff contract for operating a company platform
without depending on one individual. Company repositories may add
environment-specific details, but must not weaken these controls.

This document is ordered for the moment it is opened. Emergencies come first;
setup, which is read once and never in a hurry, comes last.

- **[When something is wrong](#when-something-is-wrong)** — restore, a stuck
  release, rollback, blocked convergence.
- **[Routine operations](#routine-operations)** — deploying, taking and
  proving a backup, accepting a reboot.
- **[How the machinery behaves](#how-the-machinery-behaves)** — what the
  platform does on its own, and why.
- **[Setting things up](#setting-things-up)** — a new host, a new recipient,
  the material an operator must hold.

Two things worth knowing before anything else:

**A rollback does not roll back data.** It moves the application backwards;
the database keeps whatever is in it. Restoring data is a separate, explicit
act — see *Restoring under pressure*.

**`platform status` answers the question you actually have.** Not only when a
dump was last taken, but when a restore was last *proven*, whether offsite
storage is current, and how much data an outage right now would cost.

# When something is wrong

## Rotating the database password

```sh
sudo -n platform rotate-database-password \
  --project <project> \
  --environment <environment> \
  --confirm-disruptive \
  --json
```

The database is changed first, then the stored credential, then the
application is restarted so it picks up the new URL. That order is deliberate:
a stored credential that no longer opens the database is a worse failure than
a database whose new password is not yet stored, because the second is visible
immediately and the first is not.

`--confirm-disruptive` is required because the application restarts. If the
restart fails after the password has changed, the command says so — deploy the
current release to recover.

Losing the password is not a data loss and does not need this command. The
image trusts local socket connections, so an operator with root can
`docker exec` into the container, `psql` with no password, set one, and
re-encrypt the credential file.

## Restoring under pressure

Find the dump first. `platform backups` lists what this host holds, newest
first, with the release each was taken on, whether it reached object storage,
and whether a restore from it has been proven:

```sh
sudo -n platform backups --project <project> --environment <environment>
```

Then restore it:

```sh
sudo -n platform restore \
  --project <project> \
  --environment <environment> \
  --from <stamp> \
  --confirm-destructive \
  --json
```

Omit `--from` to take the newest dump. `--confirm-destructive` is required and
deliberately unpleasant to type: this replaces the contents of the live
database.

The command refuses while a deployment holds the project lock, so a restore
cannot land underneath a migration in flight. If a deploy is stuck, resolve
that first — see the section on releases stuck in `deploying`.

When the dump was taken on a different release than the one deployed, the
command says so and continues. It warns rather than refuses because restoring
across a revision is sometimes exactly what an operator means to do — but the
schema lives inside the dump, so an older dump under a newer application will
break on the first query for a column the dump does not have. Read the note
before walking away.

## Restoring from object storage

Reading the history back is an operator action with an operator key. Supply it
on standard input, the same way a registry token reaches a deployment; it
never lands on the host.

```sh
cat ~/.config/platform-keys/reader-s3.env |
  sudo -n platform restore \
    --project <project> \
    --environment <environment> \
    --from <stamp> \
    --from-offsite \
    --confirm-destructive \
    --json
```

`--from-offsite` needs an explicit `--from`: there is no "newest" to infer
when the local copy is what went missing.

## A release is stuck in `deploying`

Treat this as an application data incident, not a retryable CI failure.

1. Stop automatic deploy retries for that project and environment.
2. Capture `platform status --json`, the failed workflow URL, application logs,
   proxy logs, and `journalctl` output for the incident window.
3. Determine from the release record and application database whether the
   migration did not start, failed, or completed. The application repository
   must document how its migrations are verified and reversed or how the
   database is restored.
4. Restore a known-compatible database/application pair using that application
   procedure. Do not claim success from container health alone.
5. Escalate for explicit operator resolution of the unfinished ledger entry.
   The core deliberately has no generic command that guesses a migration
   outcome. A dedicated audited resolution interface must be added before this
   step is delegated to on-call staff.

The last step is a known controlled limitation, not an invitation to modify JSON
under `/var/lib/platform/projects` by hand.

## Rollback

Prefer the application's own deploy workflow pointed at the older revision.
That path rebuilds the bundle for the revision, pulls the image by digest, and
records the result as a new deployment, so the ledger keeps a truthful forward
history of what ran and when. Re-running the tag of the release that is already
current is a no-op and reports `containers_started: false`.

Use the host-side `platform rollback` when CI, the registry, or the network is
unavailable. It restores a release from artefacts already on disk, which is why
retention is expressed as an offline rollback depth (see below).

Select a previously successful release tag from `platform status --json`, then:

```sh
sudo -n platform rollback \
  --project <project> \
  --environment <environment> \
  --to <previous-release-tag> \
  --registry-username <registry-user> \
  --registry-token-stdin \
  --json
```

Pass the registry token on standard input. The CLI intentionally refuses a
rollback while any release is still `deploying`, because the database migration
outcome may be unknown. Do not bypass that guard or edit the release ledger.

## `access_guard` blocks convergence

`access_guard` proves convergence is running as the operations user through the
host Tailscale address. If it fails:

1. keep or open the provider console;
2. verify `tailscaled` is running and enroll the host using the bootstrap
   procedure;
3. open a new tailnet SSH session as the operations user;
4. run the access-guard tagged preflight, then full convergence.

Do not disable the guard from an unverified public SSH session. A temporary
`access_guard_enabled=false` is allowed only from a verified provider console
as a documented break-glass action, and must be reverted and followed by a full
converge from the tailnet.

# Routine operations

## Normal deployment

Application repositories call the immutable, version-pinned reusable deploy
workflow. An operator should verify its bundle-build, Tailscale access,
deployment, status, and healthcheck steps all pass.

On the host, read-only status is available as:

```sh
sudo -n platform status \
  --project <project> \
  --environment <environment> \
  --json
```

Never deploy a mutable image tag. Never copy plaintext application secrets to
the host or CI workspace outside the platform's tmpfs materialization path.

## Listing what you have

```sh
sudo -n platform backups --project <project> --environment <environment>
```

One row per dump, newest first: when it was taken, why, how large, which
release it came from, whether it reached object storage, and whether a restore
from it has been proven. `--json` for a machine.

## Database backups

`platform backup` writes one encrypted dump of a platform-owned database:

```sh
sudo -n platform backup \
  --project <project> \
  --environment <environment> \
  --json
```

`--reason` records why it ran — `operator`, `schedule`, or `pre-migration`.
Only `operator` is in use today; the other two arrive with scheduling.

What the design guarantees, and why:

- `pg_dump` is piped straight through age into its destination. The plaintext
  exists only in that pipe, never on disk. A failed dump or a failed
  encryption leaves nothing behind at all.
- The dump is encrypted to the **same age recipients as the application's own
  secrets**, escrow included. A backup therefore inherits the bus factor of
  everything else rather than growing its own — whoever can read the secrets
  can read the backup, and nobody else.
- The filename carries only a timestamp and the reason. Everything else lives
  in a metadata card **both beside the dump and inside the encrypted stream**,
  so a dump found alone describes itself once decrypted. The application
  version deliberately stays out of the filename: object keys are readable by
  anyone who can list a bucket.
- The stream opens with one readable line — `PLATFORM-BACKUP/1 <length>` —
  followed by the card and then the untouched `pg_dump` bytes. Someone holding
  nothing but `age` can read the card directly:

  ```sh
  age --decrypt -i /etc/platform/keys/age.key <dump> | head -c 800
  ```

  Dumps written before this envelope existed begin with `pg_dump`'s own magic
  instead. `platform restore` reads both; a backup that stopped restoring
  because the format improved would not be an improvement.
- The card keys on `release_id`, not on the release tag. A tag is a human
  label and may be anything; `release_id` is always present and unique.
- Retention comes from `database.backup.retain`. It drops the oldest dumps
  beyond that count, never the newest, and only ever warns.

`platform status` reports the loss window this implies: how old the newest
dump is, and therefore how much data an outage right now would cost. When the
newest dump is more than twice the configured interval old, it says the
schedule has stopped rather than quoting a window that is no longer bounded by
anything.

## Proving a backup

A backup nobody has restored is not a backup. `verify-backup` turns that from
a slogan into a dated fact:

```sh
sudo -n platform verify-backup \
  --project <project> \
  --environment <environment> \
  --json
```

It decrypts the newest dump, restores it into a **throwaway container with no
network at all**, runs the query the application declared in
`restore_validation`, and removes the container whether or not any of that
worked. The live database is never touched. The container briefly holds a full
plaintext copy of the data, which is why it gets no network and why nothing it
writes outlives it.

Every attempt is recorded, success or failure. `platform status` reports when
a restore was last **proven**, which is a different question from when a dump
was last taken — and the only one that matters at three in the morning.

## Reboot acceptance

1. Confirm converge is idempotent and readiness is green in the `post`
   phase. The role defaults to `pre`, which skips every check covering what
   convergence built and still reports success.
2. Reboot through the provider console or a verified operations session.
3. Verify Tailscale operations access returns.
4. Confirm Docker, Tailscale, firewall, proxy, reconciliation timer, and secrets
   recovery units are enabled and healthy.
5. Run readiness in the `post` phase and converge again; converge must report
   no unexpected change.
6. Run an application healthcheck and `platform status --json`.

Reboot recovery refuses ambiguous ledgers, including unfinished deployments.
That refusal is a safety signal requiring the `deploying` incident procedure.

# How the machinery behaves

## Platform-owned databases

An application that declares `database.mode: docker` gets a PostgreSQL
instance the platform runs for it. The application states only the major
version; the platform decides everything else — the image (resolved to a
digest once and pinned until the major changes), the volume, the network,
and the credential.

What an operator needs to know:

- The database is the one thing releases do not recreate. The volume is
  keyed to project and environment and survives deploys, rollbacks, and
  release retention. **A rollback moves the application backwards; it does
  not move the data backwards.**
- The database joins its own internal network, unreachable from the edge.
  The application reaches it through `DATABASE_URL`, which the platform
  injects beside the application's own secrets. An application that ships
  its own `DATABASE_URL` is refused.
- The credential lives SOPS-encrypted in
  `/var/lib/platform/databases/{project}/{environment}/`, encrypted to the
  same age recipients as the application's secrets, escrow included. Its
  envelope follows the application's recipient set on each deploy; the
  password itself does not rotate.
- The container restarts after a reboot on its own. Boot recovery only
  re-materialises the tmpfs files; it needs the age key, not Docker.
- A database that will not come up fails the deploy before anything is
  swapped, and before the ledger records a release.
- **An existing volume is sacred.** Two mismatches around one stop the deploy
  rather than being improvised past. Changing `postgres_major` while a volume
  exists is refused: data does not move between majors on its own, and
  PostgreSQL 18 changed where the cluster lives inside the volume, so the old
  one would simply be ignored. A volume whose credential file has gone missing
  is refused too: a freshly generated password never reaches a database that
  already exists, so the application would fail to authenticate against a
  deploy that reported success.
- Losing the credential is recoverable and is not a data loss. The image
  trusts local socket connections, so an operator with root on the host can
  `docker exec` into the container, `psql` without a password, set a new one,
  and re-encrypt the credential file. Only the volume is irreplaceable — which
  is what backups are for.

## Scheduled backups

A deployment reconciles the host timer with what the release declares. An
application with `backup_enabled: true` gets
`platform-backup@{project}-{environment}.timer` running at its declared
`interval_minutes`; one that turns backups off, or moves to an external
database, has its timer disabled and its override removed. Host state that
outlives the release which created it needs a removal path, and this is it.

```sh
systemctl list-timers 'platform-backup@*'
journalctl --unit 'platform-backup@*' --since today
```

The units themselves are installed by convergence, not written by a deploy:
what runs and as whom is fixed by the operator, and a deploy only supplies the
cadence through a drop-in. Timers carry a randomized delay so several projects
on one host do not all dump at the same instant.

These are monotonic timers: the first run is scheduled relative to boot, and
every run after that relative to the previous one. A host that was switched off
does not replay the windows it missed — it simply resumes. `platform status`
reports the newest dump, which is where a long gap becomes visible.

A timer that cannot be written produces a warning, not a failed deployment: a
release already serving traffic is not a failed release. The warning appears in
the deploy output under `schedule`.

## Backups before migrations

A migration is the most common way to lose data, so a deploy that runs one
takes a dump first, automatically, regardless of the schedule. This narrows the
loss window at the riskiest moment to seconds.

This is not gated on `backup_enabled`. That flag governs the *schedule*;
turning it off must not silently remove the safety net from a destructive
step. Any platform-owned database gets a dump before its migration, and the
dump lands under the same retention as any other.

**This one is not housekeeping, and it does fail the deploy.** Retention and
scheduling only ever warn, because they are tidying up around a release that
already works. A pre-migration dump is the safety net for a destructive step:
if the net cannot be strung, the step does not happen. The release is recorded
as `failed` with the migration never attempted.

## Getting backups off the host

A backup on the same disk as its database does not survive losing the disk.
When `platform_cli_offsite_enabled` is set for a host, every backup run carries dumps to
object storage. A host without that configuration keeps its backups local,
which stays a supported answer rather than an error.

The upload **reconciles rather than pushes**: each run sends whatever local
dump is missing remotely, not merely the one just taken. Three consequences
worth knowing:

- Enabling offsite storage carries the existing dumps up on the first run.
- An upload that failed yesterday is retried today instead of being lost.
- The work is bounded by local retention, which already capped how many dumps
  exist.

Nothing readable leaves the host: dumps are encrypted to the application's own
age recipients before this step ever sees them. Object keys carry no
application version — they are readable by anyone who can list the bucket, and
the version lives in the metadata card.

**The host holds a write-only credential.** It can add backups, and can
neither read the history back nor delete any of it. Remote retention is a
bucket lifecycle rule, not something the platform does — a backup an attacker
can erase only protects against a failed disk.

**A failed upload fails the command.** This is the one place the
warnings-only rule bends: a host configured for offsite backups that has
quietly stopped uploading is exactly the failure discovered too late. The dump
itself still succeeded and stays on disk for the next run to carry up.
`platform status` reports whether offsite storage is current, behind, or not
configured.

## Release retention

Every successful deployment reclaims artefacts left by superseded releases.
Retention keeps the newest distinct **image digests**, not the newest
deployments, so repeatedly redeploying one version never evicts the versions
behind it. The depth defaults to five and an application may override it with
`deployment.retained_releases` in its `platform/v1` manifest.

Within the retained depth a release stays rollback-able with no network. Older
releases keep their ledger records — the audit trail is never pruned — but lose
their staged bundle and cached image, so restoring one means deploying its
revision again through CI.

Two rules keep this safe:

- An image digest is removed only when no ledger on the host still wants it.
  Environments of the same application routinely run the same digest.
- Decrypted runtime secrets are kept only for the release that is running.
  Rollback and reboot recovery both re-materialize them from the staged bundle.

Retention runs after the ledger is committed and can only produce warnings; a
successful deployment is never reported as failed because housekeeping was not.
Read `retention.warnings` in the deploy output. Never run `docker system prune`
on a platform host: the proxy and its ACME companion share that daemon.

# Setting things up

## Required ownership and recovery material

Before production use, verify all of the following:

- two current operators can access the company infrastructure repository;
- the tailnet has at least two company-controlled owners or admins;
- the hosting-provider account, billing, MFA recovery, and support access are
  company-controlled;
- `users_ops_ssh_keys` contains keys for at least two current operators;
- every deployable SOPS file has at least two unique age recipients;
- one age private key is available to normal automation and a different
  recovery private key is stored in the company vault with audited access;
- the recovery key has been tested from a clean workstation without copying it
  into a repository, CI log, shell history, ticket, or chat.

Removing a person requires rotating their SSH key, tailnet access, provider
access, tokens, and any age recipient they controlled. Removing an age
recipient requires `sops updatekeys` on every affected encrypted file.

## Bootstrap a new host

Keep the hosting-provider console or public bootstrap SSH session open until a
second tailnet session has been verified.

1. Create a short-lived, preferably one-off and pre-authorized Tailscale auth
   key for the company server tag. Save it in a local `0600` file outside Git.
2. Set `tailscale_auth_key_source` to that controller-local path and set
   `tailscale_advertise_tags` in ignored company inventory.
3. Run the collection bootstrap playbook as the bootstrap user.
4. From a separate terminal, connect as the operations user over Tailscale and
   verify `id`, `hostname`, `sudo -n true`, and `tailscale ip`.
5. Run preflight, converge, and readiness through the Tailscale address.
6. Only after those checks pass, remove public SSH access according to the
   company firewall policy and revoke any reusable enrollment credential.

If no auth-key source is configured, the role prints an interactive
`tailscale up` command. That path is break-glass onboarding, not the normal
company procedure.

## Roll out the second SOPS recipient

Do not raise enforcement before the files are re-encrypted.

1. Generate a dedicated company recovery age identity on a trusted machine and
   place its private key in the company vault.
2. Add its public recipient to the company `.sops.yaml` creation rules.
3. Run `sops updatekeys <encrypted-file>` for every environment secrets file.
4. On a clean workstation, retrieve only the recovery identity and prove that
   `sops decrypt <encrypted-file>` succeeds.
5. Set `minimum_age_recipients: 2` in every application call to the reusable
   deploy workflow.
6. Set `platform_cli_minimum_age_recipients: 2` in company host inventory and
   converge every host.

The builder then rejects weak bundles before transfer. The root-owned host
wrapper independently rejects new deployments and rollbacks whose saved bundle
does not satisfy the same policy. Duplicate recipient entries count only once.

## Quarterly bus-factor drill

A different operator, using a clean workstation and documented company access,
must demonstrate all of the following without contacting the primary author:

- decrypt one non-production SOPS file with the recovery identity;
- bootstrap a disposable host onto the company tailnet;
- run preflight, converge, readiness, and reboot acceptance;
- deploy a non-production release and roll it back;
- explain the `deploying` and `access_guard` stop conditions.

Record only evidence and outcomes. Never attach private keys, auth keys, tokens,
or decrypted secrets to the drill report.
