# Vercel sandboxes

Send `{"provider": "vercel"}` to `POST /hosts`. Drukbox creates a named
Vercel sandbox, starts Tailscale, and returns its `internal_ssh_host`.
The API and callers connect with Tailscale SSH as root. There is no
public SSH endpoint or gateway.

## Configure

Build the supplied `images/vercel/Dockerfile` as a `linux/amd64` image
and publish it to the Vercel Container Registry (VCR) of your project.
For example, from `images/vercel` with the project linked in the Vercel CLI:

```bash
vercel vcr login docker
vercel vcr build docker . drukbox-sandbox:v1 --push
```

Wait for the image to show `Ready` in VCR. Then configure Drukbox:

```dotenv
VERCEL_TOKEN=YOUR-VERCEL-ACCESS-TOKEN
VERCEL_TEAM_ID=team_YOUR_TEAM
VERCEL_PROJECT_ID=prj_YOUR_PROJECT
VERCEL_DEFAULT_IMAGE=drukbox-sandbox:v1
TAILSCALE_ENABLED=true
```

Use an access token with sandbox access to this team and project. These
settings also accept files in `/run/secrets`, including `VERCEL_TOKEN`.
Configure the Tailscale OAuth credentials and tag policy as described in
[Networking](networking.md). Permit Tailscale SSH as root from the API
and callers to the sandbox tag. A separate Vercel project per Drukbox
deployment keeps ownership clear.

The image contains bash, Tailscale, jq, sudo, git, gh, and CA tools.
Bootstrap uses userspace Tailscale when systemd is absent. It needs no
TUN device. Custom images must contain these tools; a Vercel managed
image alone does not satisfy this contract. Vercel ignores image
`ENTRYPOINT` and `CMD`; Drukbox starts bootstrap through the command API.

Userspace Tailscale provides incoming SSH but does not add kernel routes
for application traffic. The secrets proxy address must be reachable
through the sandbox's normal outbound network. A tailnet-only proxy
address is not sufficient.

| Setting | Default | Purpose |
| --- | --- | --- |
| `VERCEL_DEFAULT_IMAGE` | Required | VCR image reference; use a digest for a fixed image |
| `VERCEL_VCPUS` | `2` | Default virtual CPU count |
| `VERCEL_SESSION_TIMEOUT_SECONDS` | `2700` | Fixed session duration, including bootstrap |
| `VERCEL_API_TIMEOUT` | `150` | HTTP request timeout, in seconds |
| `VERCEL_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | `120` | SSH readiness timeout |

An `image` in `POST /hosts` selects another prepared VCR image. An
`instance_type`, such as `"4"`, sets the vCPU count. Drukbox accepts 1–32;
Vercel enforces the account's plan limit. Per-host disk sizing and Drukbox
template builds are not supported. Bootstrap has a 120-second command
limit and a 130-second client deadline.

## Leases and cleanup

Vercel limits each uninterrupted session to 45 minutes on Hobby and
24 hours on Pro and Enterprise. Set `VERCEL_SESSION_TIMEOUT_SECONDS`
within the plan limit. Drukbox accepts 600–86400 seconds and defaults to
the Hobby limit. Each sandbox has persistence disabled. Drukbox does not
snapshot, resume, or replace a stopped sandbox.

Run `uv run alembic upgrade head` before using this version. The migration
adds a nullable `lease_deadline` to hosts. At creation, Drukbox stores the
creation time plus the configured session duration, less 60 seconds.
This conservative limit includes provisioning time and is fixed for that
host. A change to provider settings cannot extend an existing host.

Omitted leases and empty renewals are capped at `lease_deadline`. An
explicit permanent lease or a date beyond the limit returns `400` with
`HOST_LEASE`. Pool hosts use the same limit; a claim cannot extend it.
Renewal does not call Vercel or increase the session duration. Copy work
out before expiry.

Deletion removes the named sandbox and its orphan snapshots. A failed
bootstrap or an uncertain create response triggers a deletion attempt.
The generated name makes cleanup possible even if a create response is
lost. If cleanup fails, Drukbox retains the host row for the janitor.
Keep the janitor running to remove expired provider records and Tailscale
devices after a session stops.

`GET /doctor` checks read access to the project's sandbox list. It does
not create a sandbox, check the image, or test Tailscale SSH.

## Verify

```bash
uv run pytest src/providers/vercel/tests src/hosts/tests/test_lease_deadline.py
uv run ruff check
uv run ruff format --check
uv run pyright
```

The provider tests mock Vercel HTTP responses. Before production use,
verify a real create, Tailscale SSH connection, renewal, expiry, and
janitor deletion in the target project.

## References

- [Vercel sandbox images](https://vercel.com/docs/sandbox/concepts/images)
- [Vercel session duration and persistence](https://vercel.com/kb/guide/vercel-sandbox-duration-and-persistence)
- [Vercel SDK API client](https://github.com/vercel/sandbox/blob/main/packages/vercel-sandbox/src/api-client/api-client.ts)
- [Tailscale userspace networking](https://tailscale.com/kb/1112/userspace-networking)
