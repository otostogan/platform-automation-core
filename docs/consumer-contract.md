# Consumer contract

Core and consumer repositories remain deliberately separate. A company
infrastructure repository owns inventory, host variables, controller-local
secret paths, and thin execution workflows. An application repository owns
its image, Compose model, `platform/v1` manifest, and SOPS-encrypted values.

Consumers pin the `v0.3.4` release, verify `SHA256SUMS`, install the
collection tarball, and invoke fully qualified playbooks. Application triggers
call the centrally owned reusable deployment workflow. No consumer imports a
checkout-relative Python path or receives the core Git history. The core is
public, so no credential is needed to read its releases.

See `examples/consumer` for sanitized repository fixtures.

Consumers must also adopt the recovery and access controls in
[`runbook.md`](runbook.md). In particular, application workflows and host
inventory both enforce two unique SOPS age recipients only after all encrypted
files have been re-keyed and independently recovery-tested.
