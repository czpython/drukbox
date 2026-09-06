"""Secrets on docker-sbx: sbx's own per-sandbox secret store does the swap."""

import os
import shlex
import shutil
import tempfile
from pathlib import Path

from host_secrets.catalog import CATALOG, Service
from host_secrets.placeholder import Placeholder
from providers.base import SecretInjectionCapability
from providers.exceptions import ProviderCommandError, ProviderTransportError

from .api import SbxCLI
from .exceptions import DockerSbxProviderError

# sbx's own secret for these services takes our value and covers every
# client on its own, when the entry reaches the service itself. Every other
# service, and a custom entry that reaches a host of its own, is a custom
# secret on its hosts.
_NATIVE_SERVICES = {"github": CATALOG["github"]}


class SbxInjection(SecretInjectionCapability):
    """sbx holds the value in its own store, scoped to one sandbox, and swaps
    the placeholder at its proxy.

    sbx reads the value by running ``cat`` on a file, on the host, under
    sandboxd, at each use. So a pushed value is a rewritten file. The file
    lives under ``secrets_root``, beside the sandbox workspaces and never
    inside one, because a workspace is mounted into its box.
    ``delete_secrets`` removes the directory with the secrets.
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
        try:
            (self.secrets_root / vm).mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise ProviderCommandError(f"cannot make the secrets directory: {exc}") from exc
        path = self.write_value(vm, placeholder.service, value)
        command = f"cat {shlex.quote(str(path))}"
        try:
            if _NATIVE_SERVICES.get(placeholder.service) == service:
                await self.api.set_secret(placeholder.service, sandbox=vm, command=command)
            else:
                await self.api.set_custom_secret(
                    sandbox=vm,
                    hosts=[upstream.host for upstream in service.upstreams],
                    env=service.credential_var,
                    placeholder=str(placeholder),
                    command=command,
                )
        except DockerSbxProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc
        return {service.credential_var: str(placeholder)}

    async def push_secret(self, *, vm: str, name: str, value: str) -> None:
        """A push after teardown finds no directory and fails, so it brings nothing back."""
        self.write_value(vm, name, value)

    async def delete_secrets(self, *, vm: str) -> None:
        """Remove every secret in the sandbox's scope, and the value files.

        A custom secret is removed by the placeholder sbx lists for it. sbx
        keeps a sandbox's secrets after the sandbox is removed, and answers a
        missing one with success, so this can run again after a partial
        teardown.
        """
        try:
            for name in _NATIVE_SERVICES:
                await self.api.remove_secret(name, sandbox=vm)
            for placeholder in await self.api.custom_placeholders(sandbox=vm):
                await self.api.remove_custom_secret(sandbox=vm, placeholder=placeholder)
        except DockerSbxProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc
        shutil.rmtree(self.secrets_root / vm, ignore_errors=True)

    def write_value(self, vm: str, name: str, value: str) -> Path:
        """Replace the value file whole, so sbx never reads a half-written one."""
        path = self.value_path(vm, name)
        try:
            descriptor, staged = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(descriptor, "w") as file:
                    file.write(value)
                os.replace(staged, path)
            finally:
                Path(staged).unlink(missing_ok=True)
        except OSError as exc:
            raise ProviderCommandError(f"cannot write the value file: {exc}") from exc
        return path

    def value_path(self, vm: str, service: str) -> Path:
        return self.secrets_root / vm / service
