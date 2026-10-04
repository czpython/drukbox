import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from providers.cloudflare.api import CloudflareAPI
from providers.cloudflare.provider import CloudflareProvider
from providers.cloudflare.settings import CloudflareSettings
from providers.exceptions import ProviderCommandError, ProviderTransportError
from providers.registry import get_provider_names


@pytest.fixture
async def provider() -> AsyncIterator[CloudflareProvider]:
    settings = CloudflareSettings(worker_url="https://worker.test", worker_token="test-token")
    provider = CloudflareProvider(CloudflareAPI(settings), settings, service_label="drukbox")
    yield provider
    await provider.aclose()


@respx.mock
async def test_create_uses_named_image_and_returns_tailnet_only_host(provider: CloudflareProvider):
    route = respx.put("https://worker.test/sandboxes/sb-test").respond(
        201, json={"status": "active"}
    )
    result = await provider.create_vm(
        name="sb-test",
        image="base",
        env={"GREETING": "hello world"},
        setup_script="tailscale up",
        instance_type="lite",
    )
    request = route.calls[0].request
    body = json.loads(request.content)
    assert request.headers["Authorization"] == "Bearer test-token"
    assert body["image"] == "base"
    assert body["instance"] == "lite"
    assert body["label"] == "drukbox"
    assert "export GREETING=" in body["script"]
    assert "tailscale up" in body["script"]
    assert result.name == "sb-test"
    assert result.ssh_username == "root"
    assert not result.ssh_host
    assert not result.private_key


@respx.mock
async def test_requires_tailscale_before_any_request(provider: CloudflareProvider):
    with pytest.raises(ProviderCommandError, match="TAILSCALE_ENABLED"):
        await provider.create_vm(name="sb-test", image="base")
    assert not respx.calls


@respx.mock
async def test_rejects_unknown_size(provider: CloudflareProvider):
    with pytest.raises(ProviderCommandError, match="instance_type"):
        await provider.create_vm(
            name="sb-test", image="base", setup_script="true", instance_type="huge"
        )
    assert not respx.calls


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 502])
async def test_uncertain_creation_is_deleted_by_name(provider: CloudflareProvider, status: int):
    respx.put("https://worker.test/sandboxes/sb-test").respond(status, text="sensitive details")
    deletion = respx.delete("https://worker.test/sandboxes/sb-test").respond(
        200, json={"status": "deleted"}
    )
    with pytest.raises(ProviderTransportError) as error:
        await provider.create_vm(name="sb-test", image="base", setup_script="true")
    assert "sensitive" not in str(error.value)
    assert deletion.called


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 409])
async def test_rejected_creation_does_not_delete_an_existing_host(
    provider: CloudflareProvider, status: int
):
    respx.put("https://worker.test/sandboxes/sb-test").respond(status)
    with pytest.raises(ProviderCommandError):
        await provider.create_vm(name="sb-test", image="base", setup_script="true")
    assert len(respx.calls) == 1


@respx.mock
async def test_lost_response_is_cleaned_up(provider: CloudflareProvider):
    respx.put("https://worker.test/sandboxes/sb-test").mock(side_effect=httpx.ReadTimeout("lost"))
    deletion = respx.delete("https://worker.test/sandboxes/sb-test").respond(
        200, json={"status": "deleted"}
    )
    with pytest.raises(ProviderTransportError):
        await provider.create_vm(name="sb-test", image="base", setup_script="true")
    assert deletion.called


@respx.mock
async def test_wrong_worker_response_fails(provider: CloudflareProvider):
    respx.put("https://worker.test/sandboxes/sb-test").respond(200, text="login page")
    respx.delete("https://worker.test/sandboxes/sb-test").respond(200, json={"status": "deleted"})
    with pytest.raises(ProviderTransportError, match="Invalid"):
        await provider.create_vm(name="sb-test", image="base", setup_script="true")


@respx.mock
async def test_delete_and_health(provider: CloudflareProvider):
    deletion = respx.delete("https://worker.test/sandboxes/sb-test").respond(
        200, json={"status": "deleted"}
    )
    respx.get("https://worker.test/health").respond(200, json={"status": "ok"})
    await provider.delete_vm("sb-test")
    assert deletion.called
    assert "authentication succeeded" in await provider.diagnose()


def test_registered_provider():
    assert "cloudflare" in get_provider_names()
