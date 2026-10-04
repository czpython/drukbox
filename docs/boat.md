# Boat sandboxes

`POST /hosts` with `{"provider": "boat"}` creates a Boat sandbox and returns
direct SSH coordinates. The response contains a per-host private key once.
The provider uses the public Boat HTTP API. It needs no Boat CLI or SDK.

Boat supplies an Ubuntu VM with passwordless sudo. The provider uses this
external SSH path even when `TAILSCALE_ENABLED` is true. The provider does not
join the tailnet. The secrets proxy must accept connections from the sandbox.

## Configuration

Set `BOAT_API_TOKEN` to an API key with sandbox read, create, update, command,
SSH-key, and delete permissions. The provider also reads deployment secrets
from files through the standard settings loader.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BOAT_API_TOKEN` | Required | Boat bearer token |
| `BOAT_API_URL` | `https://boat.dev/api/v1` | API base URL |
| `BOAT_DEFAULT_IMAGE` | `default` | Native Boat image, or a named Boat snapshot |
| `BOAT_INSTANCE_TYPE` | `default` | `small`, `default`, or `large` |
| `BOAT_API_TIMEOUT` | `30` | HTTP timeout in seconds |
| `BOAT_PROVISION_TIMEOUT` | `300` | Readiness timeout in seconds |
| `BOAT_BOOTSTRAP_SSH_TIMEOUT_SECONDS` | `120` | SSH keyscan timeout in seconds |

`instance_type` in a host request overrides `BOAT_INSTANCE_TYPE`.
`image` selects a named Boat snapshot. The reserved value `default` selects
the native image. OCI images, custom disk sizes, and template builds are
unsupported. A named snapshot must retain Bash, sudo, SSH, and the standard
Ubuntu certificate tools.

The provider sets `noEnv: true`. Boat account credentials, repositories, and
secret files do not enter the sandbox. Drukbox writes its environment through
the command API and installs the proxy CA before it returns the host.

## Lifecycle

The provider sets `ttlSeconds: null` and disables snapshots. Drukbox leases
and the janitor own host expiry. Provisioning fails if Boat imposes an archival
deadline, for example on a restricted account.

The Boat display name is `SERVICE_LABEL:host-name`. Drukbox uses this exact
name for teardown and follows every page of the sandbox list. Keep
`SERVICE_LABEL` stable while hosts exist.

Create requests use a stable idempotency key. The adapter retries transport
failures with the same body and key. If all attempts fail before Boat returns
an ID, reconcile the request in Boat before another allocation. Boat retains
idempotency keys for 24 hours. A failed provision attempts immediate deletion.
The error retains the resource ID when Boat returned one.

Deletion submits the permanent-delete operation with the exact sandbox ID
in the confirmation header. Boat owns the accepted background purge. Archive
is not used, and no snapshot is retained for a new sandbox. A source named
snapshot remains independent.

`GET /doctor` uses one read-only sandbox list request.

## API evidence

- [Boat API and idempotency](https://docs.boat.dev/api/v1)
- [Create sandbox](https://docs.boat.dev/api/reference/sandboxes/create-sandbox)
- [SSH key and coordinates](https://docs.boat.dev/api/reference/agent/configure-sandbox-ssh-key)
- [Permanent deletion](https://docs.boat.dev/api/reference/sandboxes/permanently-delete-sandbox-data)
- [Machine capabilities](https://docs.boat.dev/machines)

Provider and HTTP tests use mocked responses. They do not prove live account
permissions, SSH connectivity, proxy access, or provider capacity.
