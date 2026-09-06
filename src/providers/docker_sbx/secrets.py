"""Secrets on docker-sbx: sbx's own per-sandbox secret store does the swap."""

import shlex
from pathlib import Path

from host_secrets.catalog import Service
from host_secrets.placeholder import Placeholder
from providers.base import SecretInjectionCapability
from providers.exceptions import ProviderTransportError

from .api import SbxCLI
from .exceptions import DockerSbxProviderError

# sbx's own secret for these services takes our value and covers every
# client on its own. Every other service is a custom secret on its host.
_NATIVE_SERVICES = frozenset({"github"})


class SbxInjection(SecretInjectionCapability):
    """sbx holds the value in its own store, scoped to one sandbox, and swaps
    the placeholder at its proxy.

    sbx reads the value by running ``cat`` on a file, on the host, under
    sandboxd. The file lives under ``secrets_root``, beside the sandbox
    workspaces and never inside one, because a workspace is mounted into its
    box. The provider removes a sandbox's directory with the sandbox.
    """

    holds_value = True

    def __init__(self, api: SbxCLI, secrets_root: Path) -> None:
        self.api = api
        self.secrets_root = secrets_root

    async def put_secret(
        self,
        *,
        vm: str,
        service: Service,
        placeholder: Placeholder,
        value: str,
    ) -> dict[str, str]:
        path = self.value_path(vm, placeholder.service)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch(mode=0o600)
        path.chmod(0o600)
        path.write_text(value)
        command = f"cat {shlex.quote(str(path))}"
        try:
            if placeholder.service in _NATIVE_SERVICES:
                await self.api.set_secret(placeholder.service, sandbox=vm, command=command)
            else:
                await self.api.set_custom_secret(
                    sandbox=vm,
                    host=service["host"],
                    env=service["credential_var"],
                    placeholder=str(placeholder),
                    command=command,
                )
        except DockerSbxProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc
        return {service["credential_var"]: str(placeholder)}

    async def delete_secret(self, *, vm: str, placeholder: Placeholder) -> None:
        try:
            if placeholder.service in _NATIVE_SERVICES:
                await self.api.remove_secret(placeholder.service, sandbox=vm)
            else:
                await self.api.remove_custom_secret(sandbox=vm, placeholder=str(placeholder))
        except DockerSbxProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc
        self.value_path(vm, placeholder.service).unlink()

    def value_path(self, vm: str, service: str) -> Path:
        return self.secrets_root / vm / service
