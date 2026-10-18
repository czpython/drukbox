# Daytona sandboxes

Create a Daytona host through the existing API:

```json
{"provider": "daytona"}
```

The `image` field selects a prepared Daytona snapshot name or ID. The
snapshot can be a Linux VM or a container; the provider rejects Windows
snapshots. CPU, memory, and disk come from the snapshot. Per-host
`instance_type` and `disk_gb` are not supported.

A Linux VM has its own kernel. A container shares the host kernel, and
Daytona cannot pause it. Linux VMs can be unavailable to an organization: a
VM create then fails with "Snapshot ... is not available in region ...". Use
a container snapshot, or ask Daytona to enable VMs.

The Daytona organization must be Tier 3 or higher. Tier 1 and Tier 2 limit
outbound traffic to an allowlist without Tailscale, and a sandbox setting
cannot change that.

## Prepare the snapshot

The snapshot needs the tools in `images/daytona/Dockerfile`: Tailscale with
iptables and iproute2, Bash, jq, Git, GitHub CLI, sudo, and CA tools. For a
custom image, keep these tools and passwordless root access. Do not install
an SSH server: Tailscale SSH serves the host, and an image with `/etc/ssh`
host keys gives every sandbox the same keys. Never snapshot a sandbox that
has joined Tailscale into a reusable image.

Daytona's Dockerfile builder can build a container snapshot from
`images/daytona/Dockerfile`. A Linux VM snapshot needs the image in a
registry that Daytona can read:

```bash
docker build --platform linux/amd64 -t registry.example.com/team/drukbox-daytona:1 images/daytona
docker push registry.example.com/team/drukbox-daytona:1
```

Replace the example registry and image name. Set the CPU, memory, and disk
on the snapshot: every host from it gets that size. If you do not set them,
Daytona applies its default sandbox size. For more than one size, create one
snapshot per size and select it with `image`. Wait for the snapshot to become
active in the target region. Use its name or ID as `DAYTONA_DEFAULT_IMAGE`.
Image builds, stored snapshots, and sandboxes use provider resources.

## Configure Drukbox

| Variable | Value |
| --- | --- |
| `DAYTONA_API_KEY` | Required API key; can use `/run/secrets/DAYTONA_API_KEY` |
| `DAYTONA_DEFAULT_IMAGE` | Required active snapshot name or ID |
| `DAYTONA_TARGET` | Required target region where the snapshot is available |
| `DAYTONA_API_TIMEOUT` | API request timeout in seconds; default `150` |
| `DAYTONA_LIFECYCLE_TIMEOUT_SECONDS` | Start or deletion polling budget; default `180` |
| `DAYTONA_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | SSH readiness budget; default `120` |

Configure the tailnet and Tailscale credentials as described in
[Networking](networking.md). Hosts use Tailscale SSH as `root`; they have no
public SSH address. The adapter does not use Daytona's expiring SSH tokens.

Drukbox disables Daytona auto-stop, auto-pause, auto-delete, and wall-clock
TTL. Deleting the host, or the janitor at lease expiry, removes the sandbox.
Drukbox owns the lease; keep the janitor running. Pause, resume, and template
builds through Drukbox are not supported by this adapter.

Bootstrap persists the environment and installs the secrets proxy CA. Without
systemd, as in a Daytona container, bootstrap starts `tailscaled` itself and
keeps its state in `/var/lib/tailscale`. If the sandbox has a TUN device,
programs in it get tailnet routes and MagicDNS. Without one, `tailscaled`
falls back to userspace networking: inbound SSH works, but programs in the
sandbox cannot reach tailnet addresses, so the secrets proxy must be
reachable off the tailnet. HTTP previews stay private.

A paused VM keeps `tailscaled` running. Nothing restarts `tailscaled` if it
exits. A stop clears the sandbox's processes, so the host leaves the
tailnet. Do not stop Drukbox sandboxes in Daytona; delete the host instead.

Drukbox tags the sandbox with `SERVICE_LABEL`, checks ownership before
deletion, and waits for destruction. If provider deletion fails, Drukbox
keeps the host record for a retry. Do not rename sandboxes, change their
ownership label, or change `SERVICE_LABEL` while hosts exist.

`/doctor` checks API authentication and the default snapshot class and
state. It does not check that the snapshot is available in `DAYTONA_TARGET`.
If it is not, a create fails with Daytona's reason in the host error.
`/doctor` does not provision a sandbox or prove Tailscale connectivity. Live
provisioning and SSH require a separate test with a Daytona account and
configured tailnet.

## Sources

- [Daytona sandboxes](https://www.daytona.io/docs/en/sandboxes/)
- [Daytona snapshots](https://www.daytona.io/docs/en/snapshots/)
- [Daytona Tailscale support](https://www.daytona.io/docs/en/vpn-connections/)
- [Daytona REST API](https://www.daytona.io/docs/openapi.json)
- [Daytona command API](https://www.daytona.io/docs/toolbox-openapi.json)
