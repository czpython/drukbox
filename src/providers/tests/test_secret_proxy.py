from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.docker.provider import DockerProvider
from providers.docker.settings import DockerSettings
from providers.exceptions import ProviderTransportError
from secret_proxy.client import SecretProxyClient
from secret_proxy.exceptions import SecretProxyUnavailableError


def _provider() -> tuple[DockerProvider, MagicMock]:
    secret_proxy = MagicMock(spec=SecretProxyClient)
    secret_proxy.put_secret = AsyncMock()
    secret_proxy.delete_secret = AsyncMock()
    secret_proxy.list_secrets = AsyncMock(return_value=["openai"])
    provider = DockerProvider(
        MagicMock(),
        DockerSettings(),
        secret_proxy=secret_proxy,
    )
    return provider, secret_proxy


@pytest.mark.asyncio
async def test_proxy_capability_registers_the_value_and_returns_box_environment() -> None:
    provider, proxy = _provider()

    environment = await provider.put_secret(
        vm="box-one",
        name="openai",
        host="api.example.com",
        auth_var="API_TOKEN",
        base_url_var="API_BASE_URL",
        placeholder="placeholder",
        value="real-secret",
    )

    assert environment == {"API_TOKEN": "placeholder"}
    proxy.put_secret.assert_awaited_once_with(
        vm="box-one",
        name="openai",
        host="api.example.com",
        placeholder="placeholder",
        value="real-secret",
    )


@pytest.mark.asyncio
async def test_proxy_capability_deletes_and_lists_box_secrets() -> None:
    provider, proxy = _provider()

    assert await provider.list_secrets(vm="box-one") == ["openai"]
    await provider.delete_secret(vm="box-one", name="openai")

    proxy.list_secrets.assert_awaited_once_with(vm="box-one")
    proxy.delete_secret.assert_awaited_once_with(vm="box-one", name="openai")


@pytest.mark.asyncio
async def test_proxy_capability_translates_control_errors_without_secret_values() -> None:
    provider, proxy = _provider()
    proxy.put_secret.side_effect = SecretProxyUnavailableError("control failed")

    with pytest.raises(ProviderTransportError, match="secret proxy request failed") as caught:
        await provider.put_secret(
            vm="box-one",
            name="openai",
            host="api.example.com",
            auth_var="API_TOKEN",
            base_url_var="API_BASE_URL",
            placeholder="placeholder",
            value="must-not-appear",
        )

    assert "must-not-appear" not in str(caught.value)
