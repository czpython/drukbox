import shlex
from pathlib import Path

from host_secrets.catalog import Service
from host_secrets.placeholder import Placeholder
from providers.capabilities import SecretInjectionCapability
from providers.exceptions import ProviderTransportError

from .api import SbxCLI
from .exceptions import DockerSbxProviderError

# sbx's own secret for these covers git and gh. Every other service is a
# custom secret on its host.
_NATIVE_SERVICES = frozenset({"github"})


class SbxInjection(SecretInjectionCapability):
    """sbx holds the value, per sandbox, and reads it from a file under
    ``secrets_root``. A workspace is mounted into its box, so the file is
    never in one."""

    needs_value = True

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
        # Closed before the value goes in. chmod covers a file from an earlier put.
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
                    env=service["auth_variable"],
                    placeholder=str(placeholder),
                    command=command,
                )
        except DockerSbxProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc
        return {service["auth_variable"]: str(placeholder)}

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
