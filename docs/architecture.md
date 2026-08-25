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

`providers.base.VMProvider` is the whole interface:

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

Not every provider supports every feature, and the host contract must
not grow provider-shaped warts. Optional features are capability
mix-ins: `HttpProxyCapability` declares the http-proxy surface, and
`TemplateCapability` declares the template create and delete surface.
`resolve_capability` narrows a specific provider instance
to a capability — the default provider for account-bound operations,
the host's own provider for host-bound ones — and raises the shared
`CapabilityUnsupportedError` when that provider does not implement it,
which the routes surface as a clear error. New provider-specific
features must follow this pattern rather than widening `VMProvider`
or the host schema.

The review question that guards the whole design: *does this change
leak a provider into the contract?*

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

A template is a persistent provider image keyed by provider, base image,
and setup-script hash. `POST /templates` creates a `building` record and
returns `202 Accepted`. Callers poll until the template becomes
`available` or `failed`. Templates outlive hosts. Each provider builds
and deletes its own templates behind `TemplateCapability`.

A host request can name an available template by its ID. The `POST
/templates` create is idempotent by content, so the caller can always
repost the same declaration to get the current ID back.
The template's handle becomes the host image. An explicit `image`
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

Three maintenance commands run as cron jobs from the same image:

- `hosts.janitor` reaps expired and orphaned hosts.
- `templates.janitor` marks abandoned builds `failed` and deletes failed
  or unused templates.
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
