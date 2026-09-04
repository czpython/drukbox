from secret_proxy.client import SecretProxyClient
from secret_proxy.exceptions import SecretProxyError

from .capabilities import SecretInjectionCapability
from .exceptions import ProviderTransportError


class ProxySecretInjectionCapability(SecretInjectionCapability):
    secret_proxy: SecretProxyClient

    async def put_secret(
        self,
        *,
        vm: str,
        name: str,
        host: str,
        auth_var: str,
        base_url_var: str,
        placeholder: str,
        value: str,
    ) -> dict[str, str]:
        try:
            await self.secret_proxy.put_secret(
                vm=vm,
                name=name,
                host=host,
                placeholder=placeholder,
                value=value,
            )
        except SecretProxyError as error:
            raise ProviderTransportError("secret proxy request failed") from error
        return {auth_var: placeholder}

    async def delete_secret(self, *, vm: str, name: str) -> None:
        try:
            await self.secret_proxy.delete_secret(vm=vm, name=name)
        except SecretProxyError as error:
            raise ProviderTransportError("secret proxy request failed") from error

    async def list_secrets(self, *, vm: str) -> list[str]:
        try:
            return await self.secret_proxy.list_secrets(vm=vm)
        except SecretProxyError as error:
            raise ProviderTransportError("secret proxy request failed") from error
