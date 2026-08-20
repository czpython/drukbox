# Deploy and operate

For why the networking modes behave the way they do, read
[Networking](networking.md). For the trust model and the tradeoffs
behind these defaults, read [Security](security.md).

## Image and processes

One image serves everything — API, maintenance commands, migrations.
It's published to `ghcr.io/czpython/drukbox` on every release; build
`docker build -t ghcr.io/czpython/drukbox .` only to run a local change.

```bash
IMAGE=ghcr.io/czpython/drukbox:latest

# API (port 8780; /healthz for liveness probes)
docker run --rm -p 8780:8780 --env-file drukbox.env "$IMAGE"

# Migrations (one-off, before first start and on upgrades)
docker run --rm --env-file drukbox.env "$IMAGE" .venv/bin/alembic upgrade head

# Maintenance (cron, e.g. every 10-15 min)
docker run --rm --env-file drukbox.env "$IMAGE" .venv/bin/python -m hosts.janitor
docker run --rm --env-file drukbox.env "$IMAGE" .venv/bin/python -m hosts.pool
```

The janitor reaps expired and orphaned hosts. The pool maintainer
pre-provisions warm hosts per provider and only does anything when at
least one provider has a warm target (`POOL_SIZES` / `POOL_SIZE`).
Schedule both under your cron infrastructure (k8s `CronJob`, systemd
timer) from the same image and env file.

Use Postgres in production (`postgresql+psycopg://...`). SQLite
(`sqlite+aiosqlite:///./drukbox.db`) is for single-process demos and
local development; the pool maintainer is safe under SQLite only with
a single runner.

The API binds all interfaces by default. When only loopback callers
reach it (host-networked, co-located client), set `UVICORN_HOST=127.0.0.1`
to keep the credential-holding control plane off other interfaces.

## Choose a provider

| Provider | Sandboxes | Where |
| --- | --- | --- |
| `exe` | exe.dev VMs | Remote |
| `aws` | EC2 instances | Remote |
| `hetzner` | Hetzner Cloud VMs | Remote |
| `exoscale` | Exoscale VMs | Remote |
| `docker` | Containers ([Local sandboxes with Docker](#local-sandboxes-with-docker)) | Local, no external account |
| `docker-sbx` | microVMs ([Local microVMs with Docker Sandboxes](#local-microvms-with-docker-sandboxes)) | Local |

`DEFAULT_HOST_PROVIDER` selects the provider for `POST /hosts` (default
`exe`). Set the matching provider variables below. The image contains
all provider extras.

## Local sandboxes with Docker

The `docker` provider runs each sandbox as a local container with sshd,
so you can try drukbox with no cloud account or API token. The published
image includes the Docker CLI. Set `DEFAULT_HOST_PROVIDER=docker`,
`TAILSCALE_ENABLED=false`, and `UVICORN_HOST=127.0.0.1` in `drukbox.env`,
then run:

```bash
# Linux: match the host socket's group. macOS: use group 0 (see below).
docker run --rm --network host \
  --group-add "$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo 0)" \
  --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
  --env-file drukbox.env \
  ghcr.io/czpython/drukbox:latest
```

On macOS, use `--group-add 0` instead: Docker Desktop mounts the socket into
the container as `root:root` mode `0660`, so only group 0 grants access — the
host socket's own gid is irrelevant.

Host networking is required because Docker sandboxes publish SSH on the
host's `127.0.0.1`. The loopback Uvicorn binding makes the API reachable
only from that host. Do not combine this mode with the generic
`-p 8780:8780` invocation above. On macOS, if sandbox SSH is
unreachable, enable host networking in Docker Desktop's settings.

The sandbox image (`DOCKER_DEFAULT_IMAGE`, default
`ghcr.io/czpython/drukbox/sandbox:latest`) is pulled on first provision.
To customize it, build [images/local/](../images/local/) and point
`DOCKER_DEFAULT_IMAGE` at your tag.

Containers publish sshd on a random `127.0.0.1` port and are reachable
only from the host that runs drukbox; the per-host key is the auth
boundary. Tailscale is not supported — a local container has no path
onto the tailnet, so docker hosts stay local under a tailnet-mode
service: no join, no `internal_ssh_host`, the published port is the only
path. One drukbox can serve tailnet VMs and local containers side by
side.

This provider is for local development and demos, not production: it
talks to the host's Docker daemon, and granting drukbox access to that
socket is host-root-equivalent. Do not expose a docker-backed drukbox to
untrusted callers.

Janitor and pool one-off containers using the Docker provider need the
same socket mount and socket-GID supplemental group. `DOCKER_HOST` remains
available when the daemon is remote or rootless instead of exposed through
`/var/run/docker.sock`.

## Local microVMs with Docker Sandboxes

The `docker-sbx` provider runs each sandbox as a
[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) microVM. Each
microVM has its own kernel, its own filesystem, and its own Docker
daemon. The sandboxd network policy controls the egress. This provider
is local to the drukbox machine, the same as the `docker` provider. It
does not support Tailscale.

Prepare the host fully before drukbox starts. drukbox only connects to
the host:

1. Install Docker Engine and `docker-sbx`. Ubuntu 24.04+ with KVM is
   necessary: `/dev/kvm` must exist, and the service user must be in the
   `kvm` group.
2. Sign in one time with `sbx login`. Headless hosts use a device-code
   flow.
3. Start the daemon: `sbx daemon start -d --policy balanced`.

Docker documents `sbx` as a tool for the daemon owner's own user on the
host. Thus the simplest deployment runs drukbox directly on the host, as
the same user:

```bash
uv run uvicorn api.app:app --host 127.0.0.1 --port 8780
```

In this mode, no mounts and no extra variables are necessary. The CLI
finds the daemon socket automatically.

drukbox can also run as a container adjacent to the daemon. Docker does
not document this mode; drukbox uses the CLI's own daemon-endpoint
variable, `DOCKER_SANDBOXES_API`. Mount the daemon socket, the `sbx`
binary of the host (then the CLI version and the daemon version always
agree), the CLI auth store, and the workspace root:

```bash
docker run --rm --network host \
  --mount type=bind,src=$HOME/.local/state/sandboxes/sandboxes/sandboxd/sandboxd.sock,dst=/run/sandboxd.sock \
  --mount type=bind,src=$(command -v sbx),dst=/usr/local/bin/sbx,readonly \
  --mount type=bind,src=$HOME/.config/com.docker.sandboxes,dst=/root/.config/com.docker.sandboxes,readonly \
  --mount type=bind,src=$HOME/.drukbox/sbx-workspaces,dst=$HOME/.drukbox/sbx-workspaces \
  --env DOCKER_SANDBOXES_API=unix:///run/sandboxd.sock \
  --env DOCKER_SBX_WORKSPACE_ROOT=$HOME/.drukbox/sbx-workspaces \
  --env-file drukbox.env \
  ghcr.io/czpython/drukbox:latest
```

The daemon reads workspace paths on its own filesystem. Thus the
workspace mount must have the same path on the host and in the
container. The janitor and pool containers need the same mounts and
variables. Host networking is necessary: the daemon publishes sandbox
SSH ports on the host loopback interface only, and drukbox returns
`127.0.0.1` addresses, the same as the `docker` provider.

Only this machine can connect to the sandboxes. The key for each host
is the auth boundary. Sandboxes have no `SERVICE_LABEL` tag, because
`sbx create` has no label option.

The template image (`DOCKER_SBX_DEFAULT_IMAGE`, default
`ghcr.io/czpython/drukbox/sbx-sandbox:latest`) must start sshd without
environment variables. `sbx create` sends none. drukbox injects the key
for each host through the exec channel after the start. Build
[images/sbx/](../images/sbx/) to change the template. The
`images/local/` entrypoint needs boot-time environment variables and
cannot start as a sandbox template.

The daemon has its own image store and does not read local Docker
images. It pulls unknown template names from a registry. For a local
template, load the image into the daemon:

```bash
docker build -t drukbox/sbx-sandbox:latest images/sbx/
docker save drukbox/sbx-sandbox:latest -o /tmp/sbx-sandbox.tar
sbx template load /tmp/sbx-sandbox.tar
```

A sandbox creation takes approximately 20 seconds with a warm template
cache, and more than 30 seconds at the first pull. Thus a warm pool
(`POOL_SIZES`) is useful. Each sandbox gets the explicit
`DOCKER_SBX_CPUS` and `DOCKER_SBX_MEMORY` sizes. Without them,
the daemon gives one sandbox all host CPUs and half of the host memory.

## Choose a networking mode

`TAILSCALE_ENABLED=false` (default): callers reach sandboxes over the
provider's public path. On AWS this means per-VM keypairs and the
managed security group — see
[Networking](networking.md#tailscale-off-public-path-key-only-auth).

`TAILSCALE_ENABLED=true`: sandboxes join your tailnet at boot and
callers connect over the overlay. Requires a Tailscale OAuth client
with auth-key write scope, and tailnet ACLs that (a) own the tags in
`TAILSCALE_AUTH_TAGS` and (b) permit tailscaled-SSH to the tagged
nodes.

## AWS credentials and IAM

AWS credentials come from the SDK's default chain (instance profile,
`~/.aws`, or env) — drukbox never plumbs them through its own
settings. The policy needs `ec2:RunInstances`,
`ec2:TerminateInstances`, `ec2:DescribeInstances`, `ec2:CreateTags`,
`sts:GetCallerIdentity`, plus — with Tailscale off —
`ec2:ImportKeyPair`, `ec2:DeleteKeyPair`, `ec2:CreateSecurityGroup`,
`ec2:DescribeSecurityGroups`, `ec2:AuthorizeSecurityGroupIngress`,
`ec2:DescribeSubnets` (when `AWS_SUBNET_ID` is set), and
`ssm:GetParameter` when `AWS_DEFAULT_IMAGE` is an SSM path.
Drukbox tags everything it creates with `managed-by=<SERVICE_LABEL>`,
so write permissions can be tag-scoped.

## Verify

```bash
curl -fsS -H "Authorization: Bearer $TOKEN" http://localhost:8780/doctor
```

`/doctor` runs one read-only probe per dependency (database, active
provider, Tailscale when enabled) and reports per-check status,
latency, and a remediation hint on failures. It always returns 200 —
health is the `ok` field. `GET /healthz` is the unauthenticated
liveness probe.

For a full end-to-end check, run the black-box suite against the
deployment (it provisions and destroys a real host — disposable
infrastructure only):

```bash
SERVICE_URL=http://localhost:8780 SERVICE_TOKEN=... npm --prefix api-tests test
```

## Configuration reference

Core, required:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Async SQLAlchemy URL. |
| `SERVICE_TOKENS` | Comma-separated bearer tokens accepted from trusted callers. |

Core, optional:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEFAULT_HOST_PROVIDER` | `exe` | Provider used when callers don't specify one. |
| `SERVICE_LABEL` | `drukbox` | Label stamped onto provider resources (VM tags, SG tags). |
| `UVICORN_HOST` | `0.0.0.0` | API bind address. Set `127.0.0.1` to restrict to loopback. |
| `PROVISIONING_GRACE_SECONDS` | `600` | Safety TTL on in-flight hosts so the janitor reaps row + VM if the client disconnects mid-provision. Must exceed the worst-case provision duration. |
| `LEASE_DEFAULT_TTL` | `86400` | Lease TTL in seconds for hosts created without an explicit `expires_at`, and the extension applied by an empty `POST /hosts/{id}/renew`. An explicit `expires_at: null` at create time opts out of expiry entirely. |
| `IDEMPOTENCY_KEY_TTL_HOURS` | `24` | Retention period for successful `Idempotency-Key` mappings. |
| `POOL_SIZES` | `{}` | Warm hosts to keep ready per provider, as JSON (e.g. `{"exe": 2, "hetzner": 1}`). Overrides `POOL_SIZE` for the providers it names. |
| `POOL_SIZE` | `0` | Warm hosts to keep ready for the default provider. `0` disables its pool. |
| `POOL_HOST_MAX_AGE_HOURS` | `4` | Max age before the janitor reaps an unclaimed pool host. |
| `POOL_MAX_CREATES_PER_TICK` | `2` | Upper bound on pool provisions per tick, across all providers; caps over-provision blast radius when ticks overlap. |

Tailscale (required when `TAILSCALE_ENABLED=true`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `TAILSCALE_ENABLED` | `false` | Provision hosts onto a tailnet. |
| `TAILSCALE_TAILNET` | — | Tailnet DNS suffix for sandbox MagicDNS hostnames. |
| `TAILSCALE_AUTH_TAGS` | — | Comma-separated tags applied to minted auth keys. |
| `TAILSCALE_OAUTH_CLIENT_ID` | — | OAuth client ID. |
| `TAILSCALE_OAUTH_CLIENT_SECRET` | — | OAuth client secret. |
| `TAILSCALE_API_TIMEOUT` | `30.0` | Timeout for Tailscale API calls. |
| `DEVICE_DISCOVERY_TIMEOUT_SECONDS` | `180.0` | How long provisioning waits for a sandbox to appear in the tailnet. |

exe.dev provider:

| Variable | Default | Purpose |
| --- | --- | --- |
| `EXE_API_TOKEN` | — (required) | Bearer token for the exe.dev exec API. |
| `EXE_DEFAULT_IMAGE` | — (required) | Image used when the caller omits `image`. |
| `EXE_API_URL` | `https://exe.dev` | API base URL. |
| `EXE_API_TIMEOUT` | `30.0` | Timeout for exe.dev API calls. |
| `EXE_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | `30.0` | ssh-keyscan retry budget for a fresh exe.dev sandbox. |
| `EXE_SSH_USERNAME` | `exedev` | In-VM user callers SSH as. |

AWS provider:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AWS_REGION` | — (required) | Region for the EC2 client and launches. |
| `AWS_DEFAULT_IMAGE` | — (required) | AMI id or SSM parameter path used when the caller omits `image`. |
| `AWS_INSTANCE_TYPE` | `t3.medium` | EC2 instance type when the caller omits `instance_type`. |
| `AWS_ROOT_GB` | `100` | Root EBS volume size (gp3, encrypted) when the caller omits `disk_gb`. |
| `AWS_SUBNET_ID` | — | Optional subnet; default VPC's otherwise. |
| `AWS_SECURITY_GROUP_ID` | — | Pre-existing SG; unset → drukbox manages `drukbox-managed`. |
| `AWS_SSH_CIDRS` | — | SSH ingress CIDRs. Authoritative when set; unset → detected egress `/32`, falling back to `0.0.0.0/0`. |
| `AWS_INSTANCE_PROFILE` | — | Optional IAM instance profile attached to sandboxes. |
| `AWS_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | `120.0` | ssh-keyscan retry budget for a fresh EC2 instance. |
| `AWS_SSH_USERNAME` | `ubuntu` | In-VM user callers SSH as. |

Hetzner provider:

| Variable | Default | Purpose |
| --- | --- | --- |
| `HETZNER_API_TOKEN` | — (required) | Bearer token for the Hetzner Cloud API. |
| `HETZNER_LOCATION` | — (required) | Location for launches, e.g. `nbg1`, `fsn1`, `hel1`, `ash`. |
| `HETZNER_DEFAULT_IMAGE` | `ubuntu-24.04` | Image name/id used when the caller omits `image`. |
| `HETZNER_SERVER_TYPE` | `cx23` | Server type when the caller omits `instance_type`, e.g. `cx23`, `cx33`. Hetzner retires older generations (e.g. `cx22`); a deprecated type fails provisioning with a 422. |
| `HETZNER_API_TIMEOUT` | `30.0` | Timeout for Hetzner API calls. |
| `HETZNER_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | `120.0` | ssh-keyscan retry budget for a fresh server. |
| `HETZNER_SSH_USERNAME` | `root` | In-VM user callers SSH as. |

A fresh Hetzner server has no firewall — port 22 is open and SSH is
key-only. Drukbox mints a per-VM ed25519 key in both networking modes;
there is no security-group or ingress-CIDR configuration to manage.

Exoscale provider:

| Variable | Default | Purpose |
| --- | --- | --- |
| `EXOSCALE_API_KEY` | — (required) | Exoscale API key ID. |
| `EXOSCALE_API_SECRET` | — (required) | Exoscale API secret used to sign requests. |
| `EXOSCALE_ZONE` | — (required) | Zone for launches, e.g. `ch-gva-2`, `de-fra-1`. |
| `EXOSCALE_DEFAULT_IMAGE` | `Linux Ubuntu 24.04 LTS 64-bit` | Template used when the caller omits `image`. |
| `EXOSCALE_INSTANCE_TYPE` | `standard.medium` | Instance type when the caller omits `instance_type`. |
| `EXOSCALE_DISK_GB` | `50` | Root disk size in GB when the caller omits `disk_gb`. |
| `EXOSCALE_API_TIMEOUT` | `30.0` | Timeout for Exoscale API calls. |
| `EXOSCALE_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | `120.0` | ssh-keyscan retry budget for a fresh instance. |
| `EXOSCALE_SSH_USERNAME` | `ubuntu` | In-VM user callers SSH as. Exoscale Ubuntu templates default to `ubuntu`. |

The API key's IAM role must allow the compute operations `list-templates` and
`list-instance-types` in addition to the instance and SSH-key operations:
instance creation resolves the configured template and instance-type names to
IDs through those list calls.

Docker provider:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOCKER_DEFAULT_IMAGE` | `ghcr.io/czpython/drukbox/sandbox:latest` | Sandbox image with sshd; auto-pulled. Build `images/local/Dockerfile` to customize. |
| `DOCKER_SSH_USERNAME` | `root` | In-container user callers SSH as. |
| `DOCKER_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | `30.0` | ssh-keyscan retry budget for a fresh container. |

The published image includes the Docker CLI. Mount the local daemon socket
with its supplemental group on Linux, or use `DOCKER_HOST` for a remote or
rootless daemon. Drukbox mints a per-VM ed25519 key and publishes sshd on a
random `127.0.0.1` port. See
[Local sandboxes with Docker](#local-sandboxes-with-docker) for the
container command and the trust caveat.

Docker Sandboxes provider:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOCKER_SBX_DEFAULT_IMAGE` | `ghcr.io/czpython/drukbox/sbx-sandbox:latest` | Template image that contains sshd and starts without environment variables. Build `images/sbx/Dockerfile` to change it. |
| `DOCKER_SBX_SSH_USERNAME` | `root` | User in the sandbox for caller SSH access. |
| `DOCKER_SBX_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | `30.0` | Time limit for the ssh-keyscan tries on a new sandbox. |
| `DOCKER_SBX_CPUS` | `2` | Number of CPUs for each sandbox. |
| `DOCKER_SBX_MEMORY` | `2g` | Memory for each sandbox, in binary units. |
| `DOCKER_SBX_WORKSPACE_ROOT` | `~/.drukbox/sbx-workspaces` | Directory with one temporary workspace for each sandbox. The path must be the same for drukbox and for the daemon. |

The published image does not contain the `sbx` CLI. Mount the binary and
the auth store of the host, as
[Local microVMs with Docker Sandboxes](#local-microvms-with-docker-sandboxes)
shows. Set `DOCKER_SANDBOXES_API` to the mounted daemon socket.
