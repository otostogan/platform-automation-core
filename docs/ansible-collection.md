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
collection playbook in `v0.3.2`.
