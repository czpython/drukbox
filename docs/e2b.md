# E2B sandboxes

Create an E2B host through the existing API:

```json
{"provider": "e2b"}
```

The `image` field selects an E2B template name or ID. The provider requires
Tailscale SSH. It does not return a public SSH address or a private key.
CPU, memory, and disk are set by the template and E2B account limits.
Per-host `instance_type` and `disk_gb` are not supported.

## Prepare the template

Install the optional provider and build its Ubuntu template:

```bash
uv sync --extra e2b
uv run images/e2b/build.py --name drukbox
```

Set `E2B_API_KEY` in the build process environment. The build creates a
template in that E2B account and uses E2B build resources.
It installs Tailscale, Bash, jq, Git, GitHub CLI, and CA tools.
It does not join the template to a tailnet.

Use the returned template name or ID as `E2B_DEFAULT_IMAGE`.
For a custom template, keep these tools and root command access. Never
snapshot a host that has joined Tailscale into a reusable template.

## Configure Drukbox

| Variable | Value |
| --- | --- |
| `E2B_API_KEY` | Required account API key; can use `/run/secrets/E2B_API_KEY` |
| `E2B_DEFAULT_IMAGE` | Required prepared template name or ID |
| `E2B_SESSION_TIMEOUT_SECONDS` | VM lifetime; default `3600`, range `600`–`86400` |
| `E2B_API_TIMEOUT` | API request timeout in seconds; default `150` |
| `E2B_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | SSH readiness budget; default `120` |
| `TAILSCALE_ENABLED` | Required: `true` |

Configure the Tailscale credentials and tailnet as described in
[Networking](networking.md). The service image includes the E2B extra.

E2B permits up to one continuous hour on Base and 24 hours on Pro.
Set the lifetime within the account limit. Drukbox stores a lease deadline
60 seconds before this lifetime ends. Default leases and pool leases stay
within that deadline. Permanent leases and explicit leases beyond it fail.
Renewal cannot move the stored deadline. Changing the setting affects new
hosts only.

Drukbox explicitly disables automatic pause and resume. E2B kills the VM
when its timeout ends. Drukbox deletes it earlier when its lease ends or a
caller deletes it. Keep the janitor running for DB and Tailscale cleanup.

Bootstrap writes the host environment, installs the secrets proxy CA, and
joins Tailscale. Without systemd, Tailscale uses userspace networking. This
permits inbound Tailscale SSH but adds no kernel routes for applications.
The secrets proxy must be reachable through the VM's normal network.
Public E2B ingress is disabled.

Deletion finds all running and paused VMs with the host name and
`SERVICE_LABEL` in their metadata. Do not change those metadata fields or
`SERVICE_LABEL` while hosts exist. A failed bootstrap triggers cleanup;
failed cleanup leaves the host record for the janitor or a deletion retry.
The adapter does not retry create requests after an uncertain response.

`/doctor` checks API authentication. It does not provision a VM or prove
Tailscale connectivity. Live provisioning and SSH require a separate test
with an E2B account and a configured tailnet.

## Sources

- [E2B lifecycle and runtime limits](https://docs.e2b.dev/sandbox)
- [E2B template images](https://docs.e2b.dev/template/base-image)
- [E2B SSH transport](https://docs.e2b.dev/sandbox/ssh-access)
- [E2B API reference](https://docs.e2b.dev/api-reference/sandboxes/create-sandbox-v2)
