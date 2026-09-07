# Security model

This page states what drukbox protects, what it deliberately does not,
and the tradeoffs behind each default. For how sandboxes are reached
read [Networking](networking.md); for the configuration knobs named
here read [Deploy](deploy.md).

## Trust model: trusted callers, untrusted sandboxes

Drukbox has one trust tier. A valid service token is full control —
it can create, list, get, and delete any host. There is no per-token
scoping, per-tenant isolation, or
ownership check between token holders. Treat every token as an
operator-level credential.

The asymmetry that *is* part of the model: **callers are trusted, the
sandboxes they provision are not.** Drukbox hands back SSH coordinates
and stops — it never runs a sandbox's code and owns no runtime inside
the VM. The hardening below is about keeping an untrusted workload on a
sandbox from reaching back into drukbox's credentials or its cloud
account, not about isolating one token holder from another.

This is the right model for a single team standing up sandboxes behind
their own API. It is **not** a multi-tenant boundary: do not hand
drukbox tokens to mutually distrusting users.

## Authentication

Every endpoint except `GET /healthz` requires
`Authorization: Bearer <service-token>`. Tokens come from
`SERVICE_TOKENS` (comma-separated) and are compared in constant time,
so a wrong token leaks no timing signal. `/healthz` is unauthenticated
by design and returns only `{"status": "ok"}` — no version, config, or
dependency detail. `/doctor` is authenticated and is the only endpoint
that reports dependency state.

Rotate a token by adding the new value to `SERVICE_TOKENS`, moving
callers over, then dropping the old one. Multiple tokens are accepted
at once precisely so rotation needs no downtime.

## Control-plane network exposure

The API holds provider credentials and mints cloud resources, so the
process is a high-value target. It binds `0.0.0.0` by default for
container friendliness. When only a co-located or host-networked
caller reaches it, set `UVICORN_HOST=127.0.0.1` to keep the control
plane off other interfaces. When it must be remote, front it with TLS
termination and treat the token as the only thing standing between the
internet and your cloud account — drukbox does no TLS itself and adds
no rate limiting (see [Resource exhaustion](#resource-exhaustion)).

## Sandbox reachability and SSH auth

How a caller reaches a sandbox, and the tradeoffs of each path, are
covered in [Networking](networking.md). The security-relevant summary:

- **Per-VM keys.** On AWS (Tailscale off), Hetzner, docker, and
  docker-sbx, drukbox mints a fresh ed25519 keypair per VM, returns
  the private half **once** in the create response, and never persists
  it — a later `GET /hosts/{id}` returns `private_key: null`. The key
  is the auth boundary; password auth is never enabled.
- **AWS ingress fail-open.** The managed `drukbox-managed` security
  group opens SSH to the detected egress `/32`, or to whatever
  `AWS_SSH_CIDRS` specifies. If egress detection fails and no CIDRs are
  set, ingress falls back to `0.0.0.0/0` with a warning log. The
  per-VM key remains the boundary in that case, but set `AWS_SSH_CIDRS`
  explicitly in any environment where world-open port 22 is
  unacceptable.
- **Hetzner has no firewall.** A fresh server exposes port 22 to the
  internet; the per-VM key is the only boundary. There is no ingress
  configuration to manage.
- **First-keyscan MITM window.** With Tailscale off, the `known_hosts`
  material is scanned over the public network and carries the usual
  trust-on-first-use window. Enable Tailscale to run the scan over the
  authenticated overlay.

## Secrets and in-VM metadata

Provider tokens (`EXE_API_TOKEN`, `EXE_REGISTRY_PASSWORD`,
`HETZNER_API_TOKEN`, Tailscale OAuth) and AWS credentials are read from
the environment / the AWS SDK default chain and never written to the
database or returned by the API. Host secret recipes are encrypted in
the database with AES-256-GCM. `SECRETS_KEY` stays in the process
environment. Rotate it by prepending a new key, then remove an old key only
after no stored row needs it. A database dump or backup contains ciphertext,
but a process with the key can decrypt it.

Caller `env` stays plaintext by design. It is ordinary configuration. Each
provider writes it to `/etc/environment` on the VM, and PAM hands it to every
session at login. No response echoes it. The schema rejects the reserved key
`TAILSCALE_AUTHKEY`. It also rejects a value that PAM would change. PAM cuts a
value at `#`, treats a quote as the start of a quoted value, and joins the
next line after a trailing backslash. It also stops at a line of 8192 bytes
and loses every entry after it. So a value must be printable ASCII without
`#`, quotes, or backslashes, and without a space at either end, and the whole
`KEY=VALUE` line must stay under 8191 bytes.

`POST /hosts` never returns a secret. A validation response omits the rejected
input, so a bad value or a bad issuer header does not reach the caller. An
issuer URL must use HTTPS. It must not carry user credentials or a fragment.
Drukbox stores the URL path and query as a readable address. Put credentials
only in the issuer headers, which Drukbox encrypts. The value an issuer returns
is never stored. The exchange process keeps it in memory and fetches it again
whenever it needs to, on first use and after a restart.

A sandbox holds a placeholder, never the credential. The placeholder works
only at the secrets proxy, and only for the host and the service it names.
The entry stores a fingerprint of it, so a database read cannot replay it. The
exchange refuses with `403` on any mismatch. It never answers `401`, because
git answers a `401` with a retry through its own credential store. The
exchange decides the header and the credential, and the proxy swaps that one
header on the host the exchange approved. The proxy refuses a destination
that resolves to a loopback, private, link-local, or metadata address, so a
sandbox cannot reach the exchange or the API through it. Bind the exchange
process where only the proxy can reach it, because its answer is the real
credential. The sandbox trusts the proxy's CA for the hosts with a secret, so
whoever holds the CA key can impersonate those hosts to the sandbox. The key
lives in the proxy's volume.

Two pieces of material reach the VM through its provider's user-data /
setup-script mechanism, and that channel is the relevant exposure:

- **Tailscale auth key.** Minted per host, ephemeral, tag-scoped, and
  short-lived; it is not persisted in drukbox's database. It is
  delivered to the VM via user-data, so a process on the box can read
  it — acceptable given its single-use, ephemeral nature.
- **AWS IMDS.** Sandboxes run untrusted code, so launched EC2
  instances **require IMDSv2** (`HttpTokens: required`) with a
  put-response hop limit of 1. This stops an in-VM SSRF or stray
  process from reading instance metadata over the legacy unauthenticated
  IMDSv1 path — which would otherwise expose the user-data auth key
  and, if `AWS_INSTANCE_PROFILE` is set, live IAM role credentials. The
  hop limit keeps a containerized workload one network hop from the
  endpoint; if you run a sandbox payload in a container that genuinely
  needs IMDS, raise it deliberately. Residual: a local root on the box
  can still read its own user-data, so scope `AWS_INSTANCE_PROFILE`
  tightly (or leave it unset) and keep nothing in caller `env` that the
  sandbox workload should not see.

## Information disclosure

Provisioning failures are stored on the host as a concrete summary
(exception type and message), not a raw Python traceback. That summary
is what `HostOut.last_error` and the `POST /hosts` 502 detail return to
callers; the full traceback stays in the server log only. Keep log
sinks access-controlled — they hold the detail the API withholds.

## Resource exhaustion

Drukbox adds no quota or rate limit of its own: a valid token can
provision paid VMs without bound, so a leaked token is a cost-DoS as
well as a control-plane compromise. The controls that exist are
operational — `expires_at` plus the janitor reap idle hosts,
`PROVISIONING_GRACE_SECONDS` bounds strands, and
`POOL_MAX_CREATES_PER_TICK` caps pool over-provision. Put per-caller
quotas and rate limiting in the layer that issues and fronts tokens.

## Not vulnerabilities by design

- A service token can delete any host. There is no second factor for
  destructive calls — the token is the boundary.
- Drukbox never opens an SSH session, runs sandbox code, or creates
  Linux users. Everything past the returned SSH coordinates is the
  caller's responsibility.
- `private_key` appearing once in the create response is intentional;
  callers must capture it then, because it is never recoverable later.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md) for private reporting.
