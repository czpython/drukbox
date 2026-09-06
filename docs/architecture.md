# Architecture

This page explains why drukbox is shaped the way it is. For the steps to
integrate a provider, read [Add a provider](add-a-provider.md). For
running the service, read [Deploy](deploy.md).

## Drukbox is a broker, not infrastructure

Drukbox runs no compute of its own. A provider creates and destroys
the actual VMs; drukbox owns the lifecycle records, the networking
glue, and a uniform API in front of them. Hosted sandbox platforms sit
a layer below — each maps to a `providers/` directory.

The design goal is that switching or adding a provider touches
configuration and one adapter package, nothing else. Two rules keep
that true:

- **The contract stays thin.** The API hands back SSH coordinates and
  `known_hosts` material, then stops. Drukbox does not speak SSH, does
  not own a runtime inside the VM, and does not create Linux users.
  Everything past the SSH handshake is the caller's job.
- **Provider knowledge stays in the provider package.** Each package
  carries the fixes for that provider's sharp edges (EC2's
  split-horizon DNS, exe.dev's command escaping, Tailscale's
  ephemeral-device races), with tests pinning them.

## Boundaries

```text
hosts.api          HTTP request/response concerns only
hosts.service      host lifecycle behavior (HostService)
host_secrets.api   host secret registration concerns only
host_secrets       built-in catalog, placeholders, delivery at provisioning
secrets_exchange   the secrets exchange process behind the secrets proxy
deploy/proxy       the secrets proxy addon, run by the official mitmproxy image
templates.api      template request/response concerns only
templates.service  template build and delete behavior (TemplateService)
providers/<name>   one package per VM provider
networking/        network provider framework + Tailscale adapter
core/              settings, database, exception base
diagnostics/       /doctor orchestration
```

Provider-specific logic never lives in route handlers; HTTP decisions
never live in service methods. Provider exceptions (`Exe*Error`,
`Aws*Error`, `Hetzner*Error`, `Tailscale*Error`) are translated at the
package boundary into neutral exceptions from `providers.exceptions` and
`networking.tailscale` — nothing outside a provider package imports
its exception types.

## The provider contract

`providers.base.VMProvider` is the base interface:

- `name` / `diagnose_hint` class vars
- `supports_instance_type` / `supports_disk_gb` class vars — which
  per-request sizing fields `create_vm` honors; the host service
  refuses unsupported sizing with a 400 before any row or VM exists
- `default_image` / `bootstrap_ssh_timeout_seconds` properties
- `create_vm(...) -> VMCreateResult`
- `delete_vm(name)`
- `diagnose() -> str` — one cheap read-only probe
- `aclose()`

Providers register a factory in `providers.registry` at package import
time; instances are lazy singletons. `DEFAULT_HOST_PROVIDER` selects
which one serves requests. Providers with optional dependencies
register conditionally (the aws package no-ops when `aioboto3` isn't
installed).

Provider configuration is provider-owned: each package has its own
pydantic-settings class with an env prefix (`EXE_*`, `AWS_*`), so a
knob like the bootstrap SSH timeout can differ per provider without
the core settings knowing any provider exists.

## Capabilities, the pressure valve

Not every provider supports every feature. The host contract must not grow
fields that only one provider uses. Optional features are capability mix-ins.
`TemplateCapability` declares the template create and delete surface.

`SecretInjectionCapability` is how a secret reaches a provider's boxes. It is
not a mix-in. Each provider declares its implementation in `secret_injection`,
and the host service calls `put_secret` for each secret when a host provisions.
`ProxyInjection` serves every provider but docker-sbx: the box gets the
placeholder and the address of our proxy, and the real value stays in the
exchange. `SbxInjection` serves docker-sbx: drukbox puts the value in sbx's own
secret store for that sandbox, and sbx does the swap. `holds_value` says which
of the two an implementation is, so provisioning fetches an issuer's value only
for an implementation that keeps it.

`resolve_capability` narrows a provider instance to a capability. It raises
`CapabilityUnsupportedError` when the provider does not implement that
capability, and the routes return a clear error. A new provider-specific feature
must use this pattern. It must not widen `VMProvider` or the host schema.

The review question that guards the whole design: *does this change leak
a provider into the contract?*

## Lifecycle

`POST /hosts` provisions inline on the event loop and returns
`201 Created` with the active host, or `502 Bad Gateway` with the row
left in `error` state. States live in `hosts.models.HostStatus`:
`provisioning → creating_network → creating_vm → bootstrapping →
active`, with `error` as the terminal failure.

Retry safety is the caller's `Idempotency-Key` header — a repeated
successful key returns the original host instead of a duplicate.
Caller `env` is stored for provisioning and never returned by the API;
keys in `hosts.schemas.RESERVED_HOST_ENV_KEYS` are rejected.

`POST /hosts` takes `secrets`, keyed by service handle. A built-in handle
resolves through the catalog, which names the variable a client reads and
the hosts the service reaches, each with the shape of its credential on the
wire. `github` reaches two: `api.github.com` takes a bearer, and `github.com`
takes Basic with `x-access-token` as the user, since git's smart HTTP refuses
a bearer. A custom entry names its own `host` and `credential_var`. It can
also set `credential_header` and `credential_prefix`. The default is a bearer
token in `Authorization`. Drukbox does not consult the catalog for a custom
entry.

A static entry stores `value`. A refreshable entry stores `issuer`: the URL,
the request headers, and the refresh interval. Drukbox never stores a fetched
token. The exchange fetches it on demand, keeps it in memory, and fetches again
when less than a minute of its life remains. The issuer answers
`{"value": "…", "expires_at": "…"}`. `expires_at` is optional, and without it
the refresh interval sets the expiry. Any other answer is a failure. While a
issuer fails, the exchange serves the last value it holds, as long as that
value is still valid. With nothing valid in memory it answers `503`.

A provider that holds the value, as docker-sbx does, never asks the exchange.
For such a provider the exchange runs a timer. It fetches a fresh value when
less than a minute of the pushed one remains, and hands it to the seam's
`push_secret`. A push that fails waits like a fetch that fails, and the same
value goes again after the wait. The first push happens when the exchange
first sees the host, since the boot value came from the API process. A host
that is gone is forgotten on the next pass, so no fetch runs for a dead box.
Nothing is written back to the database.

Provisioning mints a placeholder per secret. The placeholder names the host
and the service, `drk.<host id>.<service>.<random>`. The entry keeps only a
fingerprint of the random part. The sandbox receives `<credential_var>` with
the placeholder in its boot environment, next to the caller's `env`. On every
provider but docker-sbx it also receives `HTTPS_PROXY`, `https_proxy`, and
`NO_PROXY`, so it sends its HTTPS through the proxy at `SECRETS_PROXY_URL`,
and the proxy's public CA certificate in `SECRETS_PROXY_CA`, which it
installs at boot, with `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`,
`CURL_CA_BUNDLE`, and `NODE_EXTRA_CA_CERTS` set for it. A box with a
`github` secret also points git at gh for its credential, the lines
`gh auth setup-git` writes, so git sends the placeholder as a Basic password
and the proxy swaps it. An SSH remote would go around the proxy, so the box
rewrites `git@github.com:` and `ssh://git@github.com/` to HTTPS.
The proxy is the official mitmproxy image with the addon in `deploy/proxy`.
It terminates TLS only for the hosts the exchange lists at `/upstreams`, the
hosts with a registered secret, and tunnels every other host blind. For a
request with a placeholder it asks the exchange at `/authorize`, with the
placeholder and the destination host, for the header the upstream reads and
the real credential. It swaps that one header and streams the request on.
A destination that resolves to a loopback, private, link-local, or metadata
address is refused, so a sandbox cannot reach the exchange or the API through
the proxy. Every connection goes to the address the proxy checked, and the
upstream certificate is checked against the CONNECT host. A request whose
`Host` differs from the CONNECT host is refused. On docker-sbx the sandbox gets only the placeholder. Drukbox puts
the value in sbx's own secret store for that sandbox, and sbx's proxy swaps
the placeholder on the way out. sbx reads the value file at each use, so a
pushed value is a rewritten file. Host deletion calls `delete_secrets` for the
box before the VM goes, so nothing the seam put anywhere outlives the box. It
never reads the row's secrets, so a lost key cannot block a teardown. The
janitor deletes an expired host, and an abandoned provision, through the same
path.

A template is a persistent provider image keyed by provider, base image,
and setup-script hash. `POST /templates` creates a `building` record and
returns `202 Accepted`. Callers poll until the template becomes
`available` or `failed`. Templates outlive hosts. Each provider builds
and deletes its own templates behind `TemplateCapability`.

A host request can name an available template by its ID — the ID that
the create returned. The template's image becomes the host image. An
explicit `image`
wins over the template, and the template wins over the provider default.
Host creation never builds a missing or unavailable template. It returns
a client error, and the caller decides when to build.

Every host is a renewable lease. A create without `expires_at` gets
`now + LEASE_DEFAULT_TTL`, so a host whose owner disappears lapses and
self-reaps instead of leaking VM cost; an explicit `expires_at: null`
is the deliberate opt-in to a permanent host. `POST /hosts/{id}/renew`
is the keepalive: it bumps `expires_at` to the requested instant, or by
`LEASE_DEFAULT_TTL` from now when the body is empty. Only caller-owned
hosts renew — unclaimed warm-pool members belong to pool maintenance
and refuse with `409`.

Two maintenance commands run as cron jobs from the same image:

- `janitor` reaps expired and orphaned hosts, marks abandoned template
  builds `failed`, and deletes failed or unused templates.
- `hosts.pool` keeps a warm pool of pre-provisioned hosts per provider
  (`POOL_SIZES`, with `POOL_SIZE` as the default provider's target) to
  hide provider cold starts.

When you edit a template setup script, the hash changes. The old
template ages out after its last lease. Pool members
are warmed with the provider's default image and size, so a request that
customizes its host — `image`, `env`, `template`, `instance_type`, or
`disk_gb` — always provisions fresh instead of claiming a warm host.

## Diagnostics

`GET /doctor` runs one cheap, non-mutating probe per dependency —
database, active provider, Tailscale when enabled — in parallel with a
per-probe timeout. Providers own their probe (`diagnose()`) and their
remediation slug (`diagnose_hint`); the endpoint stays a thin
orchestrator. It always returns 200; health is the `ok` field in the
body.
