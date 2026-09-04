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
        host: str,
        env_var: str,
        base_url_env: dict[str, str],
        headers: dict[str, str],
        placeholder: str,
        value: str,
    ) -> dict[str, str]:
        try:
            await self.secret_proxy.put_secret(
                vm=vm,
                host=host,
                env_var=env_var,
                headers=headers,
                placeholder=placeholder,
                value=value,
            )
        except SecretProxyError as error:
            raise ProviderTransportError("secret proxy request failed") from error
        return {env_var: placeholder, **base_url_env}

    async def delete_secret(self, *, vm: str, env_var: str) -> None:
        try:
            await self.secret_proxy.delete_secret(vm=vm, env_var=env_var)
        except SecretProxyError as error:
            raise ProviderTransportError("secret proxy request failed") from error

    async def list_secrets(self, *, vm: str) -> list[str]:
        try:
            return await self.secret_proxy.list_secrets(vm=vm)
        except SecretProxyError as error:
            raise ProviderTransportError("secret proxy request failed") from error
