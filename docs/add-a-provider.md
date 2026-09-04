# Add a VM provider

Mirror an existing package as you go: `src/providers/exe/` for an
HTTP-API provider, `src/providers/aws/` for an SDK-backed provider
with optional dependencies. Why the contract is shaped this way is
covered in [Architecture](architecture.md).

## 1. Create the package

```text
src/providers/<name>/
  __init__.py      # registers the factory
  settings.py      # provider-owned pydantic-settings
  api.py           # thin client for the provider's API/SDK
  provider.py      # VMProvider implementation
  exceptions.py    # provider-local exception types
  tests/
```

## 2. Settings

Define a `BaseSettings` subclass with `env_prefix="<NAME>_"`. Required
fields stay bare so a misconfigured deployment fails loudly at
construction. Standard knobs every provider carries:

- `default_image` — used when the caller omits `image`
- `bootstrap_ssh_timeout_seconds` — ssh-keyscan retry budget tuned to
  the provider's boot time
- `ssh_username` — the in-VM user callers SSH as

## 3. API client

Keep `api.py` a thin wrapper: endpoints, auth, transport errors, and
status-to-exception mapping. Raise the package's own exception types
(subclassed from a common base) — see `providers/aws/exceptions.py`.
No sync HTTP clients or blocking calls; everything in the request path
is async.

## 4. Provider

Subclass `providers.base.VMProvider` and implement:

- `name: ClassVar[str]` — the registry key and `DEFAULT_HOST_PROVIDER`
  value
- `diagnose_hint: ClassVar[str]` — remediation slug shown on a failed
  `/doctor` probe
- `default_image` / `bootstrap_ssh_timeout_seconds` properties reading
  from settings
- `create_vm(...) -> VMCreateResult` — return the SSH coordinates;
  populate `private_key` only if the provider mints per-VM key
  material (it is returned once and never persisted)
- `delete_vm(name)` — raise `ProviderNotFoundError` for unknown VMs
- `diagnose() -> str` — one cheap, read-only call that proves auth and
  reachability (whoami / caller-identity class). Return a short detail
  string; raise on failure and let the orchestrator classify it
- `aclose()`

Translate every exception at this boundary: catch the package's types
and re-raise `providers.exceptions.ProviderTransportError` /
`ProviderNotFoundError`. Nothing outside the package may import the
provider's exception types.

Tag created resources with `SERVICE_LABEL` (default `drukbox`) so
operators can answer "what does this deployment own?".

## 5. Register

In the package `__init__.py`:

```python
from providers.<name>.provider import <Name>Provider
from providers.registry import register_vm_provider

register_vm_provider(<Name>Provider)
```

Add an import line in `src/providers/__init__.py`. If the provider
needs optional dependencies, guard registration with
`contextlib.suppress(ImportError)` and add an optional extra in
`pyproject.toml` — mirror `providers/aws/__init__.py`. The Dockerfile
installs `--all-extras`, so the shipped image carries every provider.

## 6. Optional capabilities

If the provider supports secret injection, inherit
`SecretInjectionCapability` from `providers.capabilities` and implement
`put_secret`, `delete_secret`, and `list_secrets`. Do not add
provider-specific fields to the host schema. Add a capability instead.
The three methods are the full provider contract. The `name` argument is the
service identity. `auth_var` and `base_url_var` are environment variable names.

## 7. Tests

- Unit-test the provider with a mocked api object
  (`MagicMock`/`AsyncMock`), mirroring `providers/aws/tests/`.
- Wire-test the API client with `respx` if it speaks HTTP, mirroring
  `providers/exe/tests/test_api.py`.
- Add a `tests/test_diagnose.py` covering the happy path and that
  failures raise.
- Run the repo checks: `uv run ruff check`, `uv run ruff format
  --check`, `uv run pyright`, `uv run pytest`.

## 8. Conformance against a live deployment

Point the black-box suite at a deployment running with
`DEFAULT_HOST_PROVIDER=<name>`:

```bash
SERVICE_URL=http://localhost:8780 SERVICE_TOKEN=... npm --prefix api-tests test
```

It provisions a real host, waits for `active`, and tears it down. Use
disposable infrastructure.

## 9. Document

Add the provider's env vars to the configuration reference in
[Deploy](deploy.md).
