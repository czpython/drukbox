# Deploy and operate

For why the networking modes behave the way they do, read
[Networking](networking.md). For the trust model and the tradeoffs
behind these defaults, read [Security](security.md).

## Image and processes

One image serves everything — API, maintenance commands, migrations.
It's published to `ghcr.io/czpython/drukbox` on every release; build
`docker build -t ghcr.io/czpython/drukbox .` only to run a local change. The
secrets proxy image, `ghcr.io/czpython/drukbox/proxy`, is published beside it
with the same tags.

```bash
IMAGE=ghcr.io/czpython/drukbox:latest

# API (port 8780; /healthz for liveness probes)
docker run --rm --name drukbox -p 8780:8780 --env-file drukbox.env "$IMAGE"

# Secrets exchange (loopback, in the API network namespace)
docker run --rm --network container:drukbox --env-file drukbox.env "$IMAGE" .venv/bin/python -m secrets_exchange

# Migrations (one-off, before first start and on upgrades)
docker run --rm --env-file drukbox.env "$IMAGE" .venv/bin/alembic upgrade head

# Maintenance (cron, e.g. every 10-15 min)
docker run --rm --env-file drukbox.env "$IMAGE" .venv/bin/python -m janitor
docker run --rm --env-file drukbox.env "$IMAGE" .venv/bin/python -m hosts.pool
```

The janitor reaps expired and orphaned hosts, marks abandoned template
builds failed, keeps failed builds for diagnosis, and deletes failed or
unused templates. The pool maintainer
pre-provisions warm hosts per provider and only does anything when at
least one provider has a warm target (`POOL_SIZES` / `POOL_SIZE`).
Schedule both under your cron infrastructure (k8s `CronJob`,
systemd timer) from the same image and env file.

Use Postgres in production (`postgresql+psycopg://...`). SQLite
(`sqlite+aiosqlite:///./drukbox.db`) is for single-process demos and
local development; the pool maintainer is safe under SQLite only with
a single runner.

The API binds all interfaces by default. When only loopback callers
reach it (host-networked, co-located client), set `UVICORN_HOST=127.0.0.1`
to keep the credential-holding control plane off other interfaces.

## Secret files

A process reads a setting from a file in `/run/secrets` when that directory
exists. The file name is the variable name, for example
`/run/secrets/DATABASE_URL`. This applies to the core, exe.dev, Hetzner,
Exoscale, and Tailscale settings. Put each secret in a file and keep the other
settings in `drukbox.env`. Then `docker inspect` does not show the secrets, and
subprocesses do not inherit them.

An environment variable or a `.env` entry wins over a file. If a required
setting has no variable and no file, the process stops at startup. The error
names the setting and shows no value.

Compose mounts each file secret at `/run/secrets/<name>`. Give the same secrets
to every drukbox process: the API, the exchange, the SSH gateway, the
migrations, and the cron jobs.

```yaml
services:
  api:
    image: ghcr.io/czpython/drukbox:latest
    env_file: drukbox.env
    secrets: [DATABASE_URL, SERVICE_TOKENS, SECRETS_KEY, EXE_API_TOKEN]

secrets:
  DATABASE_URL:
    file: /srv/drukbox/secrets/DATABASE_URL
  SERVICE_TOKENS:
    file: /srv/drukbox/secrets/SERVICE_TOKENS
  SECRETS_KEY:
    file: /srv/drukbox/secrets/SECRETS_KEY
  EXE_API_TOKEN:
    file: /srv/drukbox/secrets/EXE_API_TOKEN
```

Compose bind-mounts a file secret, so the file keeps its host owner and mode.
The image runs as UID `1001`. Give each file to UID `1001` with mode `0400`.
With `docker run`, mount the directory: `-v /srv/drukbox/secrets:/run/secrets:ro`.

## Admin keys and service accounts

`SERVICE_TOKENS` holds one or more comma-separated admin keys, read at
startup. Service accounts live in the `service_accounts` table, so run
migrations before the API starts. Without the table every service
account token returns `503`. See [API](api.md#service-accounts).

## Choose a provider

| Provider | Sandboxes | Where |
| --- | --- | --- |
| `exe` | exe.dev VMs | Remote |
| `aws` | EC2 instances | Remote |
| `hetzner` | Hetzner Cloud VMs | Remote |
| `exoscale` | Exoscale VMs | Remote |
| `vercel` | [Vercel sandboxes](vercel.md) | Remote, Tailscale required |
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

Host networking lets the API reach the published sandbox ports on the
daemon host. The loopback Uvicorn binding keeps the API reachable only
from that host. Do not combine this mode with the generic
`-p 8780:8780` invocation above. On macOS, if sandbox SSH is
unreachable, enable host networking in Docker Desktop's settings.

The sandbox image (`DOCKER_DEFAULT_IMAGE`, default
`ghcr.io/czpython/drukbox/sandbox:latest`) is pulled on first provision.
To customize it, build [images/local/](../images/local/) and point
`DOCKER_DEFAULT_IMAGE` at your tag.

Docker publishes each sandbox's sshd on a random port at `DOCKER_SSH_HOST`,
and host responses return that address as `external_ssh_host`. The default
is `127.0.0.1`, so only the daemon host can connect. For remote callers,
set it to an address of the daemon host that the callers and the API can
reach: `DOCKER_SSH_HOST=100.64.0.10`. Bind the API where those callers
reach it with `UVICORN_HOST`. Docker Desktop publishes only on loopback,
so remote callers need a Linux daemon. Open the published ports in the
network policy of the daemon host. A remote caller reaches every sandbox
port the daemon host exposes, and each sandbox's SSH key is the only
boundary.

Containers do not join a tailnet and have no `internal_ssh_host`. One
drukbox can serve tailnet VMs and Docker containers together.

This provider is for local development and demos, not production: it
talks to the host's Docker daemon, and granting drukbox access to that
socket is host-root-equivalent. Do not expose a docker-backed drukbox to
untrusted callers.

Janitor and pool one-off containers using the Docker provider need the
same socket mount and socket-GID supplemental group. `DOCKER_HOST` remains available when the daemon is remote or
rootless instead of exposed through `/var/run/docker.sock`.

## Local microVMs with Docker Sandboxes

The `docker-sbx` provider runs each sandbox as a
[Docker Sandboxes](https://docs.docker.com/ai/sandboxes/) microVM. Each
microVM has its own kernel, its own filesystem, and its own Docker
daemon. The sandboxd network policy controls the egress. This provider
runs on the drukbox machine. It does not support Tailscale.

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
not document this mode. Run the container with the uid of the daemon
owner. Mount the `sbx` binary of the host, so that the CLI version and
the daemon version always agree. Mount the sbx directories and the
drukbox directory at the same paths as on the host:

```bash
docker run --rm --network host \
  --user "$(id -u):$(id -g)" \
  --mount type=bind,src=$(command -v sbx),dst=/usr/local/bin/sbx,readonly \
  --mount type=bind,src=$HOME/.local/state/sandboxes/sandboxes/sandboxd,dst=$HOME/.local/state/sandboxes/sandboxes/sandboxd \
  --mount type=bind,src=$HOME/.cache/sandboxes,dst=$HOME/.cache/sandboxes \
  --mount type=bind,src=$HOME/.config/com.docker.sandboxes,dst=$HOME/.config/com.docker.sandboxes \
  --mount type=bind,src=$HOME/.config/sandboxes,dst=$HOME/.config/sandboxes \
  --mount type=bind,src=$HOME/.drukbox,dst=$HOME/.drukbox \
  --env XDG_CONFIG_HOME=$HOME/.config \
  --env XDG_CACHE_HOME=$HOME/.cache \
  --env XDG_STATE_HOME=$HOME/.local/state \
  --env DOCKER_SANDBOXES_API=unix://$HOME/.local/state/sandboxes/sandboxes/sandboxd/sandboxd.sock \
  --env DOCKER_SBX_WORKSPACE_ROOT=$HOME/.drukbox/sbx-workspaces \
  --env-file drukbox.env \
  ghcr.io/czpython/drukbox:latest
```

The container has no home directory for the daemon owner. Thus the
`XDG_*` variables point the CLI to the mounted directories. The mounts
have these reasons:

- The daemon reads workspace paths on its own filesystem. Thus the
  workspace root must have the same path on the host and in the
  container.
- The mount holds the directory of the daemon socket, not the socket
  file. A daemon restart makes a new socket, and a file mount keeps the
  old one.
- `sbx ssh proxy` finds the socket through `XDG_STATE_HOME` only, and
  it ignores `DOCKER_SANDBOXES_API`. The other commands use
  `DOCKER_SANDBOXES_API`.
- The CLI reads its feature flags from the cache. Without the cache,
  it sees the SSH endpoint of the daemon as off, and the gateway tunnel
  fails. `/doctor` reports this failure.
- The auth store and the settings store must be writable. The CLI takes
  a lock file in the auth store also for reads, and it writes the
  settings store on first use.

The janitor, pool, and gateway containers need the same mounts and
variables.

Callers reach the sandboxes through
[the SSH gateway](#the-ssh-gateway); the provider requires it. The key
for each host is the auth boundary. Sandboxes have no `SERVICE_LABEL`
tag, because `sbx create` has no label option.

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

drukbox does these steps for each template that `POST /templates`
builds, and then removes the Docker image.

A sandbox creation takes approximately 20 seconds with a warm template
cache, and more than 30 seconds at the first pull. Thus a warm pool
(`POOL_SIZES`) is useful. Each sandbox gets the explicit
`DOCKER_SBX_CPUS` and `DOCKER_SBX_MEMORY` sizes. Without them,
the daemon gives one sandbox all host CPUs and half of the host memory.

## The SSH gateway

The gateway gives remote callers SSH access to the hosts of gateway
providers such as `docker-sbx`, whose sandboxes have no dialable sshd
of their own. Callers connect with normal SSH:

```bash
ssh -p 2222 -i <private-key> <host-name>@<gateway-address>
```

The gateway authenticates the key against the host's stored public key,
and the username must name the same host. It then opens a session
through the provider — `sbx exec` for `docker-sbx`. The daemon stops an
idle sandbox; a connection through the gateway wakes it (approximately
6 seconds) and keeps it awake while connected. The first data can
therefore come after a short delay.

The gateway serves an interactive shell, command execution, SFTP, and
local port forwarding to the sandbox loopback. It refuses scp (the
legacy protocol) and remote port forwarding. SFTP runs the
sandbox's own SFTP server over one persistent session for each SSH
connection, thus repeated file operations on one connection start no new
session. The session closes after a short idle period, so an inactive
sandbox still sleeps.

Forward a local port to a service that listens on the sandbox loopback:

```bash
ssh -N -p 2222 -i <private-key> -L 43123:127.0.0.1:43123 <host-name>@<gateway-address>
```

The destination must be `127.0.0.1` or `localhost`. The gateway refuses
all other destinations, also the IPv6 loopback. For `docker-sbx`, the
gateway opens one tunnel through `sbx ssh proxy` for each caller
connection. All forwarding channels of that caller connection use it,
and it closes when the caller disconnects. sandboxd accepts a channel to
a closed port and then closes it immediately. Thus the caller gets an
immediate end of data, not a channel-open error.

Every session runs with HOME set to the per-host home directory
`/home/<host-name>`. The gateway makes this directory and moves into it
first. Thus a caller writes files there — for example a `.gitconfig` or
credential files — and a later command finds them by `$HOME`. SFTP
relative paths also resolve against this home.

The gateway is a requirement for gateway providers: `POST /hosts` for
`docker-sbx` fails without `GATEWAY_SSH_HOST`. Set it to the address
callers use. The response then carries the gateway coordinates:
`external_ssh_host` is the gateway, `external_ssh_port` is the gateway
port, and `ssh_username` is the host name. `known_hosts` carries the
gateway's host key. Hosts of the other providers are not affected.

Run the gateway on the machine that runs sandboxd, as the same user, and
run the migrations first:

```bash
uv run python -m gateway.server
```

A systemd unit follows the same pattern as the API service. The gateway
reads the same `drukbox.env` and connects to the same database.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GATEWAY_SSH_HOST` | — (required) | Address of the gateway. Gateway-provider hosts advertise it; creation fails without it. |
| `GATEWAY_SSH_PORT` | `2222` | Port the gateway listens on and advertises. |
| `GATEWAY_BIND_HOST` | `0.0.0.0` | Interface the gateway server binds. |
| `GATEWAY_HOST_KEY_PATH` | `~/.drukbox/gateway_host_key` | Private host key. The server makes one at start when the file does not exist. |

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

## Private image registry

`REGISTRY_HOST`, `REGISTRY_USERNAME`, and `REGISTRY_PASSWORD` give drukbox
access to private images on one registry host. Set the three together.
drukbox sends the credentials only for an image on that host:

- `exe` passes them to exe.dev with the host image.
- `docker` uses them when the Docker engine pulls a host image that it
  does not have.

`TEMPLATE_REPOSITORY` is the repository path on that host where drukbox
publishes template images. The credential needs push permission there.
Registry access does not require a template repository.

```dotenv
REGISTRY_HOST=ghcr.io
REGISTRY_USERNAME=builder
REGISTRY_PASSWORD=<registry-token>
TEMPLATE_REPOSITORY=acme/sandbox-templates
```

`exe` boots hosts from a registry, so its templates require
`TEMPLATE_REPOSITORY`. `docker` and `docker-sbx` publish each template when
it is set, and keep the image local when it is not. The `docker-sbx` daemon
has its own registry login. drukbox loads each template into that daemon
and does not give it these credentials.

AWS, Hetzner, and Exoscale boot from machine images. They have no
templates and do not use these settings.

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

## The secrets exchange and the secrets proxy

A sandbox never holds a real third-party credential. It holds a placeholder,
and it sends its HTTPS through the secrets proxy. The proxy swaps the
placeholder for the real credential on the way out. Two pieces run this:

- **The proxy** is `ghcr.io/czpython/drukbox/proxy`: the official
  `mitmproxy/mitmproxy` image with the addon `deploy/proxy/swap.py` built in.
  It listens on 8880 at `SECRETS_PROXY_BIND_HOST`, `0.0.0.0` by default, and
  reads the exchange address from `SECRETS_EXCHANGE_URL`. A deployment on the
  host network sets the bind host to the one address its sandboxes dial. On a
  public host an open listener is an open proxy. A checkout can mount the
  addon into the official image instead. It terminates TLS only for the hosts
  that have a registered secret and tunnels every other host blind. It
  refuses a destination that resolves to a loopback, private, link-local, or
  metadata address. It makes its CA on first start and keeps it in a volume.
- **The exchange process** runs as `python -m secrets_exchange` from this
  image. The proxy asks it which hosts to terminate, and, for a request with
  a placeholder, for the header the upstream reads and the real credential.
  Its answer contains a credential. Keep its listener on loopback. The API
  carries remote refresh requests to it and checks its health.

The API, exchange, and proxy must share a network namespace. This Compose
example uses the API's namespace for the other two processes. Only the API
and proxy ports are published:

```yaml
services:
  api:
    image: ghcr.io/czpython/drukbox:latest
    env_file: drukbox.env
    environment:
      SECRETS_PROXY_URL: http://proxy.example:8880
      SECRETS_PROXY_CA_FILE: /secrets-proxy-ca/mitmproxy-ca-cert.pem
    ports:
      - "8780:8780"
      - "8880:8880"
    volumes:
      - secrets-proxy-ca:/secrets-proxy-ca:ro

  exchange:
    image: ghcr.io/czpython/drukbox:latest
    command: [".venv/bin/python", "-m", "secrets_exchange"]
    network_mode: "service:api"
    env_file: drukbox.env
    environment:
      SECRETS_EXCHANGE_BIND_HOST: 127.0.0.1

  proxy:
    image: ghcr.io/czpython/drukbox/proxy:latest
    network_mode: "service:api"
    environment:
      SECRETS_EXCHANGE_URL: http://127.0.0.1:8781
    volumes:
      - secrets-proxy-ca:/home/mitmproxy/.mitmproxy

volumes:
  secrets-proxy-ca:
```

A recreated `api` container gets a new network namespace and the other two
stay in the old one. After a change to `api`, recreate all three:
`docker compose up -d --force-recreate api exchange proxy`.

Use Postgres for the shared database. Set `SECRETS_PROXY_URL` to the proxy
address that sandboxes can contact. Apply the deployment's API and proxy
access rules to the published ports. Do not publish port 8781 or bind the
exchange to a public, bridge, or tailnet address.

On a host-network deployment, all three processes use the host namespace.
Keep `SECRETS_EXCHANGE_BIND_HOST=127.0.0.1`. The API reads
`SECRETS_EXCHANGE_BIND_HOST` and `SECRETS_EXCHANGE_PORT` from the same env
file as the exchange. The proxy reads `SECRETS_EXCHANGE_URL`. If you change
the exchange port, set it in both places.

Remote callers refresh a secret with
`POST /hosts/{host_id}/secrets/{service}/refresh` on the API. They never
connect to the exchange. The API reads the public CA certificate from the
shared volume and gives it to each sandbox with secrets.

A sandbox with secrets gets the certificate in `SECRETS_PROXY_CA`, base64,
and installs it at boot with `update-ca-certificates`. `SSL_CERT_FILE`,
`REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, and `NODE_EXTRA_CA_CERTS` point curl,
Python, and Node at it. A sandbox with a `github` secret gets the placeholder
in `GH_TOKEN`, git pointed at gh, and SSH remotes rewritten to HTTPS. That
sandbox needs `git` and `gh`. The docker sandbox image has both. Keep
`flow_detail` at `1` or below. A
higher level prints request headers, and after the swap those carry the
real credential. Real credentials exist in three places only. They are
encrypted in Postgres, they pass through the exchange process for one
request, and they pass through the proxy for one request.

The proxy is opt in. A deployment with only docker-sbx starts none, since
sbx does the swap itself. The exchange process runs there too. It fetches a
fresh issuer value before the old one expires and writes it into the value
file, so it needs the api's environment and its workspace root mount.

`SECRETS_PROXY_URL` is the proxy a sandbox sends its HTTPS through. Every
provider but docker-sbx sets `HTTPS_PROXY` in the sandbox to it. For local
containers the proxy listens on the Docker bridge, for example
`http://172.17.0.1:8880`. A tailnet or a private network uses its own address.

The proxy is the first flow that runs from a sandbox to drukbox. Every other
flow runs the other way. So the network between them needs one rule for it,
one address and one port, for every sandbox. On a tailnet that is a grant
from the sandbox tag to the proxy host:

```json
"hosts":  { "secrets-proxy": "100.64.0.10" },
"grants": [
    { "src": ["tag:sandbox"], "dst": ["secrets-proxy"], "ip": ["tcp:8880"] }
]
```

A sandbox then reaches that port on that host and nothing else there. A
Docker Sandboxes sandbox dials nothing. Drukbox puts each value in sbx's own
secret store for that sandbox, and sbx's proxy swaps the placeholder. The
`github` service is sbx's own `github` secret, which covers git and gh. Any
other service, and a custom entry that names a host of its own, is a custom
secret on its hosts. The value files that sbx reads live in a `secrets`
directory under `DOCKER_SBX_WORKSPACE_ROOT`, beside the workspaces and never
inside one. sbx keeps a sandbox's secrets after the sandbox is removed, so
host deletion removes every secret in the sandbox's scope and the files. The
janitor deletes an expired host the same way. Remove a sandbox through
drukbox, never with `sbx rm`, or its secrets stay in sbx's store until its
row expires. Do not set a global sbx secret for a destination drukbox
manages. sbx applies the global one first, and drukbox's value never reaches
the sandbox.

Give secrets to `POST /hosts`. Provisioning delivers the placeholders in the
sandbox's boot environment, on every provider, the same way as `env`. A
refreshable secret, one given with `issuer`, is fetched by the exchange
process on first use and kept in memory until shortly before it expires. The
exchange process must reach the issuer URL, over plain HTTP inside the
deployment or HTTPS outside it. On docker-sbx the API process
fetches it once at provisioning, since sbx holds the value. The exchange
process pushes a fresh one before it expires, and logs each push. A pool host
takes no secrets: a request with secrets always provisions a new sandbox.

## Verify

```bash
curl -fsS -H "Authorization: Bearer $TOKEN" http://localhost:8780/doctor
```

`/doctor` runs one read-only probe per dependency: database, active
provider, secrets exchange, and Tailscale when enabled. It reports per-check
status, latency, and a remediation hint on failures. It always returns 200 —
health is the `ok` field. `GET /healthz` is the unauthenticated
liveness probe.

For a full end-to-end check, run the black-box suite against the
deployment (it provisions and destroys a real host — disposable
infrastructure only):

```bash
SERVICE_URL=http://localhost:8780 SERVICE_TOKEN=... npm --prefix api-tests test
```

## Configuration reference

A core, exe.dev, Hetzner, Exoscale, or Tailscale variable can also come from a
file. See [Secret files](#secret-files).

Core, required:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Async SQLAlchemy URL. |
| `SECRETS_KEY` | Comma-separated base64 32-byte keys for encrypted host secret recipes. The first key encrypts and every key decrypts. |
| `SERVICE_TOKENS` | Comma-separated admin keys. These keys use every protected route and manage service accounts. |

Core, optional:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEFAULT_HOST_PROVIDER` | `exe` | Provider used when callers don't specify one. |
| `SERVICE_LABEL` | `drukbox` | Label stamped onto provider resources (VM tags, SG tags). |
| `UVICORN_HOST` | `0.0.0.0` | API bind address. Set `127.0.0.1` to restrict to loopback. |
| `PROVISIONING_GRACE_SECONDS` | `600` | Safety TTL on in-flight hosts so the janitor reaps row + VM if the client disconnects mid-provision. Must exceed the worst-case provision duration. |
| `REGISTRY_HOST` | — | Registry host for private images, such as `ghcr.io` or `docker.io`, with no scheme or path. See [Private image registry](#private-image-registry). |
| `REGISTRY_USERNAME` | — | Registry user for private image pulls and template pushes. |
| `REGISTRY_PASSWORD` | — | Registry password or token. |
| `TEMPLATE_REPOSITORY` | — | Repository path on `REGISTRY_HOST` for template images, with no tag or digest. |
| `TEMPLATE_BUILD_TIMEOUT` | `3600` | Max age in seconds of an unfinished template build before the janitor marks it failed. |
| `TEMPLATE_FAILED_RETENTION` | `86400` | Seconds that failed template records and diagnostics remain before the janitor deletes them. |
| `TEMPLATE_UNUSED_TTL` | `1209600` | Seconds that an available template remains after its last use, or creation when never used. |
| `LEASE_DEFAULT_TTL` | `86400` | Lease TTL in seconds for hosts created without an explicit `expires_at`, and the extension applied by an empty `POST /hosts/{id}/renew`. An explicit `expires_at: null` at create time opts out where the provider permits it. Defaults are capped by the host's `lease_deadline`. |
| `IDEMPOTENCY_KEY_TTL_HOURS` | `24` | Retention period for successful `Idempotency-Key` mappings. |
| `POOL_SIZES` | `{}` | Warm hosts to keep ready per provider, as JSON (e.g. `{"exe": 2, "hetzner": 1}`). Overrides `POOL_SIZE` for the providers it names. |
| `POOL_SIZE` | `0` | Warm hosts to keep ready for the default provider. `0` disables its pool. |
| `POOL_HOST_MAX_AGE_HOURS` | `4` | Max age before the janitor reaps an unclaimed pool host. |
| `POOL_MAX_CREATES_PER_TICK` | `2` | Upper bound on pool provisions per tick, across all providers; caps over-provision blast radius when ticks overlap. |

Secrets exchange:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SECRETS_PROXY_URL` | — | Proxy a sandbox sends its HTTPS through. Required to create a host with secrets on every provider but docker-sbx. |
| `SECRETS_PROXY_CA_FILE` | — | Path of the proxy's public CA certificate, from the proxy's volume. Required with `SECRETS_PROXY_URL`. |
| `SECRETS_EXCHANGE_BIND_HOST` | `127.0.0.1` | Loopback listener for the exchange. The API reads it to reach the exchange. |
| `SECRETS_EXCHANGE_PORT` | `8781` | Port the exchange process listens on. The API reads it to reach the exchange. |

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

exe.dev provider. exe puts `--env` where only a login shell reads it, so
the setup script also writes the exports to `~/.bashrc`, which every bash
session on the box reads.

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
| `DOCKER_DEFAULT_IMAGE` | `ghcr.io/czpython/drukbox/sandbox:latest` | Sandbox image with sshd, git, and gh; auto-pulled. Build `images/local/Dockerfile` to customize. |
| `DOCKER_SSH_HOST` | `127.0.0.1` | Daemon host address where Docker publishes sshd and callers dial it. |
| `DOCKER_SSH_USERNAME` | `root` | In-container user callers SSH as. The entrypoint seeds its `authorized_keys`; a derived image adds the user. |
| `DOCKER_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | `30.0` | ssh-keyscan retry budget for a fresh container. |

The published image includes the Docker CLI. Mount the local daemon socket
with its supplemental group on Linux, or use `DOCKER_HOST` for a remote or
rootless daemon. Drukbox mints a per-VM ed25519 key and publishes sshd on a
random port at `DOCKER_SSH_HOST`. See
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
| `DOCKER_SBX_WORKSPACE_ROOT` | `~/.drukbox/sbx-workspaces` | Directory with one temporary workspace for each sandbox, and a `secrets` directory with the value files sbx reads. The path must be the same for drukbox and for the daemon. |

The published image does not contain the `sbx` CLI. Mount the binary and
the sbx directories of the host, as
[Local microVMs with Docker Sandboxes](#local-microvms-with-docker-sandboxes)
shows.
