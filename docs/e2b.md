# E2B sandboxes

`POST /hosts` with `{"provider": "e2b"}` starts an E2B sandbox, joins it to
Tailscale, and returns `internal_ssh_host`. The sandbox has no public SSH
address, so this provider requires the Tailscale settings.

## Prepare the template

E2B boots sandboxes from templates in your E2B account. Build the drukbox
template:

```bash
E2B_API_KEY=YOUR-API-KEY uv run --extra e2b images/e2b/build.py --name drukbox
```

A custom template needs the steps in `images/e2b/build.py`.

## Configure

Give drukbox an API key and the Tailscale settings from
[Networking](networking.md). The tailnet SSH policy must let the API and
callers reach the sandbox tag as `root`.

```dotenv
E2B_API_KEY=YOUR-API-KEY
E2B_DEFAULT_IMAGE=drukbox
```

The key can also come from `/run/secrets/E2B_API_KEY`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `E2B_MAX_LIFETIME_SECONDS` | `3600` | Time until E2B kills a sandbox |
| `E2B_API_TIMEOUT` | `150` | API request timeout, in seconds |
| `E2B_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | `120` | SSH readiness timeout, in seconds |

## Limits

- E2B kills a sandbox after `E2B_MAX_LIFETIME_SECONDS`: at most 1 hour or 24
  hours, by E2B plan. No lease ends after that time: drukbox cuts a later or
  permanent `expires_at` to it, at create and at renewal.
- The template sets CPU, memory, and disk. `instance_type`, `disk_gb`, and
  template builds are not supported.
- A killed sandbox loses its files. Keep the janitor running: it deletes the
  sandbox and its Tailscale device when the lease ends.
