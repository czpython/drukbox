# Cloudflare sandboxes

`POST /hosts` with `{"provider": "cloudflare"}` starts a Cloudflare container,
joins it to Tailscale, and returns `internal_ssh_host`. The container has no
public SSH address, so this provider requires the Tailscale settings.

Cloudflare starts containers only from a Durable Object in a Worker. Drukbox
calls the Worker in `deploy/cloudflare`, which you deploy to your account.

## Deploy

Enable Cloudflare Containers for the account. The deploy machine needs
Node.js and Docker. In `deploy/cloudflare`:

```bash
npx wrangler@4.147.0 login
npx wrangler@4.147.0 secret put DRUKBOX_TOKEN
npx wrangler@4.147.0 deploy
```

Give drukbox the Worker URL, the same token, and the Tailscale settings from
[Networking](networking.md). The tailnet SSH policy must let the API and
callers reach the sandbox tag as `root`.

```dotenv
CLOUDFLARE_WORKER_URL=https://drukbox-sandboxes.YOUR-SUBDOMAIN.workers.dev
CLOUDFLARE_WORKER_TOKEN=YOUR-DEDICATED-TOKEN
```

The token can also come from `/run/secrets/CLOUDFLARE_WORKER_TOKEN`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLOUDFLARE_DEFAULT_IMAGE` | `base` | Image alias from `wrangler.jsonc` |
| `CLOUDFLARE_INSTANCE_TYPE` | `standard-1` | Default size: `lite` or `standard-1` to `standard-4` |
| `CLOUDFLARE_API_TIMEOUT` | `150` | Worker request timeout, in seconds |
| `CLOUDFLARE_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | `120` | SSH readiness timeout, in seconds |

## Limits

- `image` is an alias from `containers[].images` in `wrangler.jsonc`. An added
  image needs the tools in `deploy/cloudflare/Dockerfile`. Template builds and
  `disk_gb` are not supported.
- Without `/dev/net/tun`, Tailscale runs in userspace, so the container
  reaches `SECRETS_PROXY_URL` over its normal outbound network, not over the
  tailnet.
- A container that stops loses its files and does not restart. A container
  runs, and costs money, until drukbox deletes it, so keep the janitor running.
