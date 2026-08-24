# API

The OpenAPI schema is the canonical reference: a running service serves
interactive docs at `/docs` and the raw schema at `/openapi.json`. This
page is the orientation.

## Authentication

Every endpoint except `GET /healthz` requires
`Authorization: Bearer <service-token>`, where the token is one of
`SERVICE_TOKENS`. See [Security](security.md) for the trust model.

## Endpoints

- `POST /hosts` · `GET /hosts` · `GET /hosts/{id}` · `DELETE /hosts/{id}`
- `POST /templates` · `GET /templates` · `GET /templates/{id}` ·
  `DELETE /templates/{id}`
- `POST /http-proxies` · `DELETE /http-proxies/{name}` ·
  `POST|DELETE /http-proxies/{name}/hosts/{host_id}`
- `GET /doctor` — read-only dependency diagnostics
- `GET /healthz` — unauthenticated liveness probe
