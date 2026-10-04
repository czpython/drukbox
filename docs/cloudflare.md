# Cloudflare sandboxes

Send `{"provider": "cloudflare"}` to `POST /hosts`. Drukbox starts a
Cloudflare Linux VM, joins it to Tailscale, and returns `internal_ssh_host`.
The API and callers must reach the tailnet. There is no public SSH endpoint.

## Deploy

The provider uses an authenticated Worker in your Cloudflare account.
Each host name identifies one Durable Object and one VM. Drukbox does not
need a Cloudflare account API token at runtime.

1. Enable Cloudflare Containers for the account. Install Node.js 24 and
   Docker on the deployment machine.
2. In `deploy/cloudflare`, run `npm ci` and `npx wrangler login`.
3. Run `npx wrangler secret put DRUKBOX_TOKEN`. Supply a dedicated random
   token. Give Drukbox the same value in `CLOUDFLARE_WORKER_TOKEN`.
4. Run `npm run deploy`. Wrangler builds and uploads the supplied Ubuntu
   image, which includes Tailscale and the tools for environment setup.
5. Set these Drukbox variables and the Tailscale credentials described in
   [Networking](networking.md):

```dotenv
CLOUDFLARE_WORKER_URL=https://drukbox-sandboxes.YOUR-SUBDOMAIN.workers.dev
CLOUDFLARE_WORKER_TOKEN=YOUR-DEDICATED-TOKEN
TAILSCALE_ENABLED=true
```

The token can also come from `/run/secrets/CLOUDFLARE_WORKER_TOKEN`.
Use a separate Worker for each Drukbox deployment. Keep request bodies out
of logs: bootstrap contains Tailscale credentials and host env. The supplied
configuration disables Worker observability.

The image uses userspace Tailscale and Tailscale SSH. It needs neither
systemd nor `/dev/net/tun`. Configure the tailnet policy to allow SSH from
the API and callers to the sandbox tag, as root. Userspace Tailscale does
not add kernel routes for applications. `SECRETS_PROXY_URL` must therefore
be reachable through the VM's normal outbound network. A tailnet-only
address is not sufficient.

## Images, sizing, and lifetime

`image` selects an alias from `containers[].images` in `wrangler.jsonc`,
such as `base`. To add an image, add its Dockerfile to that map and deploy.
It must include bash, Tailscale, jq, sudo, git, gh, CA tools, and a root
account. Cloudflare pins aliases to image digests. Arbitrary image URLs
and Drukbox template builds are not supported.

| Setting | Default | Purpose |
| --- | --- | --- |
| `CLOUDFLARE_DEFAULT_IMAGE` | `base` | Deployed image alias |
| `CLOUDFLARE_INSTANCE_TYPE` | `standard-1` | Default VM size |
| `CLOUDFLARE_API_TIMEOUT` | `150` | Worker request timeout, in seconds |
| `CLOUDFLARE_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | `120` | SSH readiness timeout |

Requests can select `instance_type`: `lite`, `standard-1`, `standard-2`,
`standard-3`, or `standard-4`. Disk sizing is not supported. Bootstrap has
an independent 120-second limit in the Worker.

The Worker refreshes a five-minute inactivity timeout every minute with
an alarm. Alarms never restart a stopped VM. Drukbox leases and the janitor
own normal deletion. Keep the janitor running; VMs otherwise continue to
incur charges. An outage or missed alarms can stop a VM before its lease
ends. Files are lost when it stops. There is no automatic restore.

Creation is single-use per host name. A retry cannot run bootstrap twice.
Deletion is repeatable and leaves a tombstone, so a delayed create cannot
recreate a deleted host. Drukbox attempts deletion by name after an
uncertain response. If cleanup fails, the host row remains for the janitor.

`GET /doctor` checks Worker authentication and the local Tailscale setting.
It does not test capacity or tailnet SSH.

## Verify

```bash
uv run pytest src/providers/cloudflare/tests src/hosts/tests/test_userspace_bootstrap.py
cd deploy/cloudflare
npm ci
npm run check
npm test
npx wrangler deploy --dry-run
```

Before production use, create a host in the target account, connect over
Tailscale SSH, and verify deletion and janitor cleanup. Local tests mock
the API and Durable Object runtime.

## References

- [Durable Object Container API](https://developers.cloudflare.com/containers/api/durable-object-container/)
- [Wrangler container configuration](https://developers.cloudflare.com/workers/wrangler/configuration/#containers)
- [Tailscale userspace networking](https://tailscale.com/kb/1112/userspace-networking)
- [Tailscale SSH](https://tailscale.com/kb/1193/tailscale-ssh)
