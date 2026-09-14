# API

The OpenAPI schema is the canonical reference: a running service serves
interactive docs at `/docs` and the raw schema at `/openapi.json`. This
page is the orientation.

## Authentication

Every route except `GET /healthz` and the OpenAPI pages takes
`Authorization: Bearer <token>`. The token is an admin key from
`SERVICE_TOKENS` or a service account token. Only admin keys manage
service accounts. A missing token returns `401`, a rejected one `403`.

## Service accounts

`POST /service-accounts` with `{"name": "ci"}` returns
`{"name": "ci", "token": "drkb_..."}` once. Drukbox stores only the name
and the SHA-256 fingerprint of the token. Names are 1–64 lowercase
letters, digits, or hyphens. A duplicate name returns `409`.
`DELETE /service-accounts/ci` revokes the token on the next request, or
returns `404` for an unknown name.

## Endpoints

- `POST /service-accounts` · `DELETE /service-accounts/{name}` —
  admin keys only

- `POST /hosts` · `GET /hosts` · `GET /hosts/{id}` · `DELETE /hosts/{id}`
- `POST /templates` · `GET /templates` · `GET /templates/{id}` ·
  `DELETE /templates/{id}`
- `POST /http-proxies` · `DELETE /http-proxies/{name}` ·
  `POST|DELETE /http-proxies/{name}/hosts/{host_id}`
- `GET /doctor` — read-only dependency diagnostics
- `GET /healthz` — unauthenticated liveness probe

## The secrets exchange

The exchange is a second process, `python -m secrets_exchange`, on a private
port with no bearer token. Only the proxy and an issuer inside the deployment
reach it. See [Architecture](architecture.md) for the flow.

- `GET /upstreams` — the hosts the proxy terminates TLS for
- `GET /authorize` — the proxy's question: the header and the real credential
  for a placeholder
- `POST /refresh/{host_id}/{service}` — an issuer's order: forget the held
  value and fetch a new one now. `200` after the fetch, and after the push
  where the provider holds the value. `503` with `Retry-After` when the issuer
  gave nothing usable. `404` for an unknown host or service, `409` for a
  static entry. The order carries no body.
- `GET /healthz` — liveness probe
