# vps_readiness

Audits a platform host and reports on every check. Changes nothing.

## Phases

The role has two halves, selected by `vps_readiness_phase`.

| Phase | Checks |
| --- | --- |
| `pre` | The machine itself: distribution and version, architecture, memory, root filesystem size and headroom, the public interface, default routes, passwordless sudo, the connecting SSH identity. |
| `post` | Everything convergence built: required systemd units, Tailscale backend state and addresses, UFW policy, the IPv4 and IPv6 Docker forwarding chains, listening TCP sockets, published container ports, proxy health, and any host port exposed that should not be. |

`post` includes `pre`, so it is a superset rather than an alternative.

**The default is `pre`.** That suits a machine that has never converged. Run it
unchanged *after* convergence and the report says
`All automated VPS readiness checks passed` having skipped every check that
covers what convergence did — a green result that means much less than it
looks like.

Consumers are expected to pin the phase where the steady state belongs — the
inventory — and override for the one run that happens before there is anything
to audit:

```yaml
# group_vars/platform_hosts.yml
vps_readiness_phase: post
```

```bash
# the single pre-convergence run
ansible-playbook otostogan.platform.readiness \
    --inventory inventory/hosts.yml -e vps_readiness_phase=pre
```

That way a mistake fails loudly — there is nothing to audit yet — instead of
quietly reporting success.

## Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `vps_readiness_phase` | `pre` | `pre` or `post`, see above. |
| `vps_readiness_output` | `human` | `human` prints one line per check; `json` emits a machine-readable document for archiving acceptance evidence or diffing runs. |
| `vps_readiness_fail_on_error` | `true` | Whether failed checks abort the play. Set false to collect the full picture without stopping at the first problem, as when investigating an incident. |
| `vps_readiness_minimum_memory_mb` | `2048` | Rejected below this. |
| `vps_readiness_minimum_root_disk_bytes` | 20 GiB | Total root filesystem size. |
| `vps_readiness_minimum_root_available_bytes` | 5 GiB | Free space that must remain. |
| `vps_readiness_supported_architectures` | `x86_64`, `aarch64` | |
| `vps_readiness_allowed_public_tcp_ports` | `80`, `443` | Anything else reachable publicly is reported in `post`. |
| `vps_readiness_expected_ssh_user` | `ops` | Reported, not enforced. |
| `vps_readiness_tailscale_interface` | `tailscale0` | |
| `vps_readiness_required_services` | see defaults | Units that must be active in `post`. |

## Checks that stay manual

One item is always reported as `[MANUAL]`: the provider's own firewall. UFW on
the host cannot see it, so confirm in the provider console that temporary
public SSH is gone and only 80 and 443 remain reachable.

## Relationship to preflight

`otostogan.platform.preflight` asserts the same machine prerequisites as the
`pre` phase, but as a gate: it fails without a per-check report. Use preflight
to decide whether a host is usable at all, and readiness to see the state of
one you are already working with.
