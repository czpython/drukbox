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
returns `404` for an unknown name. The `admin` account exists from the
migration, has no token, and cannot be removed. Admin keys act as it.

## Endpoints

- `POST /service-accounts` · `DELETE /service-accounts/{name}` —
  admin keys only

- `POST /hosts` · `GET /hosts` · `GET /hosts/{id}` · `DELETE /hosts/{id}`
- `POST /templates` · `GET /templates` · `GET /templates/{id}` ·
  `DELETE /templates/{id}`
- `POST /http-proxies` · `DELETE /http-proxies/{name}` ·
  `POST|DELETE /http-proxies/{name}/hosts/{host_id}`
- `POST /hosts/{host_id}/secrets/{service}/refresh` — refresh one secret
- `GET /doctor` — read-only dependency diagnostics
- `GET /healthz` — unauthenticated liveness probe

Host responses carry `service_account`: the service account that created
or claimed the host, `admin` for an admin key, or `null` for an unclaimed
warm host. Callers cannot set it. An `Idempotency-Key` belongs to the
service account that first used it. Another one reusing it gets `409`.

## Refresh a host secret

`POST /hosts/{host_id}/secrets/{service}/refresh` makes the exchange drop
its value for that secret and fetch a new one. A provider that stores the
value receives it at once. The response is `204` with no body. The API
sends no `Authorization` header to the exchange.

- `404` with `NOT_FOUND`: The host or the secret does not exist.
- `409` with `SECRET_STATIC`: The secret has a static value.
- `503` with `SECRET_REFRESH` and `Retry-After`: The exchange did not
  answer, or the issuer or provider did not supply a value.

## The secrets exchange

The exchange is a second process, `python -m secrets_exchange`, on loopback
with no bearer token. The API and proxy share its network namespace.
Remote callers use the API refresh route. See [Architecture](architecture.md)
for the flow.

- `GET /upstreams` — the hosts the proxy terminates TLS for
- `GET /authorize` — the proxy's question: the header and the real credential
  for a placeholder
- `POST /refresh/{host_id}/{service}` — an issuer's order: forget the held
  value and fetch a new one now. `200` after the fetch, and after the push
  where the provider holds the value. `503` with `Retry-After` when the issuer
  gave nothing usable. `404` for an unknown host or service, `409` for a
  static entry. The order carries no body.
- `GET /healthz` — liveness probe
