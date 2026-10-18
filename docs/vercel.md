# Vercel sandboxes

`POST /hosts` with `{"provider": "vercel"}` starts a Vercel sandbox, joins it
to Tailscale, and returns `internal_ssh_host`. The sandbox has no public SSH
address, so this provider requires the Tailscale settings.

## Prepare the image

Vercel boots sandboxes from images in the project's Vercel Container Registry.
In `images/vercel`, with the project linked in the Vercel CLI:

```bash
vercel vcr login docker
vercel vcr build docker . drukbox-sandbox:v1 --push
```

A custom image needs the tools in `images/vercel/Dockerfile`. Do not install an
SSH server: Tailscale SSH serves the host, and image host keys would be the
same in every sandbox.

## Configure

Give drukbox an access token for the team and project, and the Tailscale
settings from [Networking](networking.md). The tailnet SSH policy must let the
API and callers reach the sandbox tag as `root`.

```dotenv
VERCEL_TOKEN=YOUR-ACCESS-TOKEN
VERCEL_TEAM_ID=team_YOUR_TEAM
VERCEL_PROJECT_ID=prj_YOUR_PROJECT
VERCEL_DEFAULT_IMAGE=drukbox-sandbox:v1
```

The token can also come from `/run/secrets/VERCEL_TOKEN`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `VERCEL_VCPUS` | `2` | Default vCPU count; each vCPU has 2 GB of memory |
| `VERCEL_MAX_LIFETIME_SECONDS` | `2700` | Time until Vercel stops a sandbox |
| `VERCEL_API_TIMEOUT` | `150` | API request timeout, in seconds |
| `VERCEL_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | `120` | SSH readiness timeout, in seconds |

## Limits

- Vercel stops a sandbox after `VERCEL_MAX_LIFETIME_SECONDS`: at most 45
  minutes on Hobby and 24 hours on Pro and Enterprise. No lease ends after
  that time: drukbox cuts a later or permanent `expires_at` to it, at create
  and at renewal.
- `instance_type` is a vCPU count, such as `"4"`, up to the plan's maximum.
  Template builds and `disk_gb` are not supported.
- Without `/dev/net/tun`, Tailscale runs in userspace, so the sandbox reaches
  `SECRETS_PROXY_URL` over its normal outbound network, not over the tailnet.
- A stopped sandbox loses its files. Keep the janitor running: it deletes the
  sandbox and its Tailscale device when the lease ends.
