# Daytona Linux VM sandboxes

Create a Daytona host through the existing API:

```json
{"provider": "daytona"}
```

The `image` field selects a prepared Daytona Linux VM snapshot name or ID.
The provider rejects container and Windows snapshots. CPU, memory, and disk
come from the snapshot. Per-host `instance_type` and `disk_gb` are not supported.

## Prepare the snapshot

Build and push the supplied image to a registry that Daytona can read:

```bash
docker build --platform linux/amd64 -t registry.example.com/team/drukbox-daytona:1 images/daytona
docker push registry.example.com/team/drukbox-daytona:1
```

Replace the example registry and image name. In Daytona, create a snapshot
from that image and select the **Linux VM** sandbox class. Wait for the
snapshot to become active in the target region. Use its name or ID as
`DAYTONA_DEFAULT_IMAGE`.

Daytona Linux VM snapshots require an existing registry image. Daytona's
Dockerfile builder does not build this snapshot class. The supplied image
includes Tailscale, Bash, jq, Git, GitHub CLI, sudo, and CA tools. For a custom
image, keep these tools and passwordless root access. Never snapshot a VM
that has joined Tailscale into a reusable image.

The account must have Linux VM capacity in the selected region. Daytona's
VPN guide requires Tier 3 or higher. Check those account requirements before
provisioning. Image builds, stored snapshots, and VMs use provider resources.

## Configure Drukbox

| Variable | Value |
| --- | --- |
| `DAYTONA_API_KEY` | Required API key; can use `/run/secrets/DAYTONA_API_KEY` |
| `DAYTONA_DEFAULT_IMAGE` | Required active Linux VM snapshot name or ID |
| `DAYTONA_TARGET` | Required target region with Linux VM capacity |
| `DAYTONA_API_TIMEOUT` | API request timeout in seconds; default `150` |
| `DAYTONA_LIFECYCLE_TIMEOUT_SECONDS` | Start or deletion polling budget; default `180` |
| `DAYTONA_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | SSH readiness budget; default `120` |
| `TAILSCALE_ENABLED` | Required: `true` |

Configure the tailnet and Tailscale credentials as described in
[Networking](networking.md). Hosts use Tailscale SSH as `root`; they have no
public SSH address. The adapter does not use Daytona's expiring SSH tokens.

Drukbox disables Daytona auto-stop, auto-pause, auto-delete, and wall-clock
TTL. It checks those settings before bootstrap. If an account policy forces
a TTL, provisioning fails and deletes the VM. Drukbox owns the lease; keep
the janitor running. Pause, resume, and template builds through Drukbox are
not supported by this adapter.

Bootstrap persists the environment and installs the secrets proxy CA. When
systemd is absent, Tailscale uses userspace networking. This permits inbound
SSH but adds no kernel routes for applications. The secrets proxy must be
reachable through the VM's normal network. HTTP previews stay private.

Drukbox tags the VM with `SERVICE_LABEL`, checks ownership before deletion,
and waits for destruction. If provider deletion fails, Drukbox keeps the
host record for a retry. Do not rename VMs, change their ownership label,
or change `SERVICE_LABEL` while hosts exist.

`/doctor` checks API authentication and the default snapshot class and state.
It does not provision a VM or prove Tailscale connectivity. Live provisioning
and SSH require a separate test with a Daytona account and configured tailnet.

## Sources

- [Daytona VM sandboxes](https://www.daytona.io/docs/en/sandboxes/)
- [Daytona snapshots](https://www.daytona.io/docs/en/snapshots/)
- [Daytona Tailscale support](https://www.daytona.io/docs/en/vpn-connections/)
- [Daytona REST API](https://www.daytona.io/docs/openapi.json)
- [Daytona command API](https://www.daytona.io/docs/toolbox-openapi.json)
