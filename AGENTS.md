# AGENTS.md

Guide for AI agents working in `drukbox`.

## Project

`drukbox` is a FastAPI service that provisions sandbox hosts. It is
a service, not a library.

It owns:

- Host records and lifecycle state in Postgres
- Inline provisioning in `POST /hosts`
- Provider VM creation and deletion (exe.dev, AWS, Hetzner, Exoscale,
  local Docker, Docker Sandboxes)
- Tailscale auth key creation, device discovery, and cleanup
- SSH host key scanning and `known_hosts` material
- Account-bound exe.dev HTTP proxy resources

Periodic maintenance runs as cron jobs: `python -m hosts.janitor` reaps
expired hosts, `python -m hosts.pool` tops up the warm pool.

No backwards compatibility is required unless a caller contract is explicitly
documented in this repo.

## Before Editing

- Read this file and the relevant source before changing behavior.
- The checklist and craft gate every change is held to:
  `.druks/review/checklist.md`.
- `docs/` holds the conceptual and operational docs. Update them when behavior
  they describe changes.
- For Python code, tests, or tooling changes, follow the style and verification
  expectations in this file.
- Keep changes scoped to this service. Do not import assumptions from unrelated
  repos.
- Prefer clean current design over compatibility layers or support for stale
  pre-launch behavior.

## Project Map

```text
src/
  api/               # FastAPI app and global handlers
  core/              # Settings, database, exception base
  hosts/             # Host API, models, schemas, service, janitor, pool, auth
  http_proxies/      # HTTP proxy API, schemas, service, deps
  providers/         # VM provider ABC, capabilities, registry, adapters
  networking/        # Network provider framework and Tailscale adapter
  conftest.py        # Test env defaults and database reset fixture
alembic/             # Database migrations
api-tests/           # Playwright black-box API tests
docs/                # Architecture, networking, deploy, add-a-provider
Dockerfile           # Single image: API + cron commands + migrations
```

## Common Commands

```bash
uv sync
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

The unit test suite uses SQLite by default — `uv run pytest` works with no
external services. The test fixture writes to a gitignored `.drukbox-test.db`
under the repo root and resets all tables between tests.

To run the suite against Postgres instead (CI does this), set
`TEST_DATABASE_URL` before running pytest (the harness ignores an ambient
`DATABASE_URL` — it might be a real one — and derives it from this):

```bash
docker run --rm --name drukbox-postgres \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=drukbox_test \
  -p 55432:5432 \
  postgres:17

TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:55432/drukbox_test \
  uv run pytest
```

For migration checks, load `env/test.env` first:

```bash
set -a
source env/test.env
set +a
uv run alembic upgrade head
```

## Architecture Rules

### Boundaries

- `hosts.api` owns HTTP request and response concerns.
- `hosts.service.HostService` owns host lifecycle behavior.
- `http_proxies.service.HTTPProxyService` owns HTTP proxy behavior.
- VM provider implementations live in `providers/<name>/`.
- Network provider implementations live in `networking/<name>/`.
- Provider and networking packages register themselves through their registries
  at package import time.

Do not put provider-specific logic directly in route handlers. Do not put CLI or
HTTP response decisions into service methods.

Provider adapters should translate provider-specific failures at the boundary.
Do not import `Exe*Error` or `Tailscale*Error` outside their provider packages;
use neutral exceptions from `providers.exceptions` and the `NetworkError` types
in `networking.tailscale`.

### Host Lifecycle

Lifecycle states are defined in `hosts.models.HostStatus`:

- `provisioning`
- `creating_network`
- `creating_vm`
- `bootstrapping`
- `active`
- `error`

`POST /hosts` runs the full provision inline: ask the network provider for join
credentials, ask the VM provider to create the VM, poll the Tailscale API for
the new device by hostname, capture its device ID, ssh-keyscan the host,
mark it `active`, and return `201 Created`. If any step errors, the host row
is left in `error` state and the response is `502 Bad Gateway`.

The effective host image is captured on the host record at creation time. If
`POST /hosts` omits `image`, store `EXE_DEFAULT_IMAGE`; provisioning must use the
stored `host.image`, not the current runtime default.

Every host is a renewable lease. `POST /hosts` without `expires_at` stores
`now + LEASE_DEFAULT_TTL`; an explicit `expires_at: null` is the deliberate
opt-in to a permanent host. `POST /hosts/{id}/renew` bumps `expires_at` — to
the given future instant, or by `LEASE_DEFAULT_TTL` from now on an empty body.
Only `active` and `bootstrapping` hosts renew, and unclaimed pool members
refuse with `409` (pool maintenance owns them). The janitor reaps hosts whose
`expires_at` has passed.

### Delete Semantics

`DELETE /hosts/{id}` is service-token only.

Deletion blocks early provisioning states where deletion can race VM creation.
For VM-backed states, if `host.tailscale_device_id` is present, release that
Tailscale device and clear the column before deleting the VM and DB row.

If provider teardown fails, keep the DB row so deletion can be retried. Because
`tailscale_device_id` is cleared as soon as Tailscale release succeeds, retrying
skips the completed Tailscale step.

Do not fall back to deleting Tailscale machines by hostname.

### Auth And Data Exposure

- Service bearer tokens can create, list, get, and delete hosts.
- HTTP proxy endpoints are service-token only.
- Caller-supplied host `env` must not include keys from
  `hosts.schemas.RESERVED_HOST_ENV_KEYS`.
- Do not expose caller `env` in API responses.

### HTTP Proxies

HTTP proxies are account-bound exe.dev resources. They are not host-owned
lifecycle state in this service.

`POST /http-proxies` creates the provider resource. Attach and detach hosts with
`POST /http-proxies/{name}/hosts/{host_id}` and
`DELETE /http-proxies/{name}/hosts/{host_id}`. Deleting a host must not delete
account-bound HTTP proxies.

Keep the API provider-agnostic. Do not expose exe attachment specs or persist
proxy headers/secrets in Postgres.

## Code Style

- Python 3.11+
- Async through request, provider, and task boundaries
- Typed functions and explicit domain methods
- Network, subprocess, database, and provider calls should stay easy to spot
- Ruff formatting and import sorting
- No sync HTTP clients or blocking subprocess calls in async paths
- No hidden fallbacks for undocumented response or config shapes
- No backwards-compatibility shims for pre-launch contracts unless explicitly
  requested

## Testing

- API unit tests use `httpx.AsyncClient` with `ASGITransport`.
- Patch provider class methods for unit tests, for example
  `providers.exe.provider.ExeProvider.create_vm` and
  `networking.tailscale.Tailscale.release_device`.
- API-level wire tests use `respx` to mock `httpx` transport.
- Use a disposable Postgres container for service tests.
- Keep Playwright API tests black-box. They must not import FastAPI internals or
  seed the database directly.

When behavior changes, add or update focused tests and run the repo-standard
checks when feasible.
