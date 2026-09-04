# Ansible Collection

The collection is published as `otostogan.platform`. Consumers install its
release artifact and call roles by fully qualified name, for example
`otostogan.platform.docker` and `otostogan.platform.firewall`.

Collection playbooks expose the stable host lifecycle:

- `otostogan.platform.preflight`
- `otostogan.platform.bootstrap`
- `otostogan.platform.converge`
- `otostogan.platform.readiness`

The reboot verification remains a controlled acceptance sequence: converge,
confirm a clean idempotent converge, reboot through the provider-approved
channel, reconnect through the private administration network, run readiness,
and confirm a final clean converge. It is intentionally not an unattended
collection playbook in `v0.15.0`.

## Readiness phases

`otostogan.platform.readiness` audits a host in one of two phases, selected by
`vps_readiness_phase`. `pre` covers the machine; `post` adds everything
convergence built — systemd units, firewall policy, Docker forwarding chains,
exposed ports, proxy health.

The default is `pre`, which suits a host that has never converged. Consumers
running it afterwards should pin `vps_readiness_phase: post` in their
inventory, because the default reports success having checked half of what
matters. See `roles/vps_readiness/README.md`.
