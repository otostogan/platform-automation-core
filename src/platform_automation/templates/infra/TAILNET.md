# Tailnet access

All access to platform hosts goes through the tailnet. The firewall denies
incoming traffic by default and opens only `80` and `443` publicly — port 22 is
not reachable from the internet at all.

## Tag scheme

| Tag | Worn by | Scope |
| --- | --- | --- |
| `tag:server-platform` | every platform host | shared, by role |
| `tag:ci-<project>` | the deploy runner of one application repository | per project |

Server tags are per role: a host is a host regardless of which applications
live on it. CI tags are per project: the tag answers "whose automation just
connected", and that differs per application repository.

Tags also decide ownership. An untagged device belongs to the personal account
that authenticated it and its node key expires; a tagged device belongs to the
tailnet and does not.

## What changes when

| Event | Edit the ACL? | What else |
| --- | --- | --- |
| another platform host | **no** | `platform new host`, an auth key with `tag:server-platform`, bootstrap, converge |
| a new application | **yes** — new CI tag | see below |
| a new operator | no | their public key into `users_ops_ssh_keys`, converge, tailnet admin |
| an operator leaves | no | remove their key line, converge, remove from the tailnet admins |

## Onboarding a new application

Four additions, with the project name replaced:

```json
"tagOwners": {
    "tag:ci-<project>": ["autogroup:owner", "autogroup:admin"],
}

"grants": [
    {
        "src": ["tag:ci-<project>"],
        "dst": ["tag:server-platform"],
        "ip":  ["tcp:22"],
    },
]

"ssh": [
    {
        "action": "accept",
        "src":    ["tag:ci-<project>"],
        "dst":    ["tag:server-platform"],
        "users":  ["deploy"],
    },
]
```

## Rules worth keeping

- CI logs in only as `deploy`; that user's sudoers allows exactly
  `platform deploy`, `status` and `rollback`.
- Operators log in only as `ops`, under `action: check`.
- CI reaches port 22 only.
- Auth keys: `ephemeral` off. Ephemeral nodes are removed from the tailnet
  when they disconnect — right for CI runners, wrong for a server.
