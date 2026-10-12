import json
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
import respx

from providers.exceptions import ProviderCommandError, ProviderNotFoundError, ProviderTransportError
from providers.registry import get_provider_names
from providers.vercel.api import VercelAPI
from providers.vercel.provider import VercelProvider
from providers.vercel.settings import VercelSettings

BASE = "https://vercel.com/api/"
SANDBOX = {
    "sandbox": {"name": "sb-test", "persistent": False},
    "session": {"id": "session-1", "timeout": 2700000},
}


@pytest.fixture
async def provider() -> AsyncIterator[VercelProvider]:
    settings = VercelSettings(
        token="test-token", team_id="team-1", project_id="project-1", default_image="sandbox:v1"
    )
    provider = VercelProvider(VercelAPI(settings), settings, service_label="drukbox")
    yield provider
    await provider.aclose()


@respx.mock
async def test_create_bootstraps_named_image_and_returns_tailnet_host(provider: VercelProvider):
    creation = respx.post(BASE + "v3/sandboxes").respond(201, json=SANDBOX)
    command = respx.post(BASE + "v2/sandboxes/sessions/session-1/cmd").respond(
        200, json={"command": {"id": "command-1"}}
    )
    completion = respx.get(BASE + "v2/sandboxes/sessions/session-1/cmd/command-1").respond(
        200, json={"command": {"exitCode": 0}}
    )
    result = await provider.create_vm(
        name="sb-test",
        image="custom:v2",
        setup_script="tailscale up",
        env={"FOO": "hello world"},
        instance_type="4",
    )
    request = creation.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.url.params["teamId"] == "team-1"
    assert json.loads(request.content) == {
        "projectId": "project-1",
        "name": "sb-test",
        "image": "custom:v2",
        "timeout": 2700000,
        "resources": {"vcpus": 4},
        "persistent": False,
        "tags": {"managed-by": "drukbox"},
    }
    body = json.loads(command.calls[0].request.content)
    assert body["sudo"] is True
    assert body["timeout"] == 120000
    assert body["command"] == "bash"
    assert "export FOO='hello world'" in body["args"][-1]
    assert "tailscale up" in body["args"][-1]
    assert completion.calls[0].request.url.params["wait"] == "true"
    assert result.name == "sb-test"
    assert result.provider_id == "session-1"
    assert result.ssh_username == "root"
    assert not result.ssh_host
    assert provider.max_lifetime == timedelta(seconds=2640)


@respx.mock
async def test_requires_tailscale(provider: VercelProvider):
    with pytest.raises(ProviderCommandError, match="TAILSCALE_ENABLED"):
        await provider.create_vm(name="sb-test", image="sandbox:v1")
    assert not respx.calls


@respx.mock
@pytest.mark.parametrize("size", ["small", "0", "33", "2.5"])
async def test_invalid_size_makes_no_request(provider: VercelProvider, size: str):
    with pytest.raises(ProviderCommandError):
        await provider.create_vm(
            name="sb-test", image="sandbox:v1", setup_script="true", instance_type=size
        )
    assert not respx.calls


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
async def test_rejected_create_does_not_delete_existing_sandbox(
    provider: VercelProvider, status: int
):
    respx.post(BASE + "v3/sandboxes").respond(status, text="sensitive details")
    with pytest.raises(ProviderCommandError) as error:
        await provider.create_vm(name="sb-test", image="sandbox:v1", setup_script="true")
    assert "sensitive" not in str(error.value)
    assert len(respx.calls) == 1


@respx.mock
async def test_lost_create_response_deletes_by_stable_name(provider: VercelProvider):
    respx.post(BASE + "v3/sandboxes").mock(side_effect=httpx.ReadTimeout("lost"))
    deletion = respx.delete(BASE + "v2/sandboxes/sb-test").respond(200, json={"sandbox": {}})
    with pytest.raises(ProviderTransportError):
        await provider.create_vm(name="sb-test", image="sandbox:v1", setup_script="true")
    assert deletion.called
    assert deletion.calls[0].request.url.params["projectId"] == "project-1"
    assert deletion.calls[0].request.url.params["deleteOrphanSnapshots"] == "true"


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        {},
        {**SANDBOX, "session": {"id": "session-1", "timeout": 300000}},
        {**SANDBOX, "sandbox": {"name": "wrong", "persistent": False}},
    ],
)
async def test_invalid_create_contract_is_cleaned_up(provider: VercelProvider, response: dict):
    respx.post(BASE + "v3/sandboxes").respond(201, json=response)
    deletion = respx.delete(BASE + "v2/sandboxes/sb-test").respond(200, json={"sandbox": {}})
    with pytest.raises(ProviderTransportError):
        await provider.create_vm(name="sb-test", image="sandbox:v1", setup_script="true")
    assert deletion.called


@respx.mock
@pytest.mark.parametrize("exit_code", [1, 137])
async def test_failed_bootstrap_deletes_sandbox(provider: VercelProvider, exit_code: int):
    respx.post(BASE + "v3/sandboxes").respond(201, json=SANDBOX)
    respx.post(BASE + "v2/sandboxes/sessions/session-1/cmd").respond(
        200, json={"command": {"id": "command-1"}}
    )
    respx.get(BASE + "v2/sandboxes/sessions/session-1/cmd/command-1").respond(
        200, json={"command": {"exitCode": exit_code}}
    )
    deletion = respx.delete(BASE + "v2/sandboxes/sb-test").respond(200, json={"sandbox": {}})
    with pytest.raises(ProviderTransportError, match="bootstrap"):
        await provider.create_vm(name="sb-test", image="sandbox:v1", setup_script="true")
    assert deletion.called


@respx.mock
async def test_missing_sandbox_translates_not_found(provider: VercelProvider):
    respx.delete(BASE + "v2/sandboxes/sb-test").respond(404)
    with pytest.raises(ProviderNotFoundError):
        await provider.delete_vm("sb-test")


@respx.mock
async def test_diagnose_lists_only_one_sandbox(provider: VercelProvider):
    listing = respx.get(BASE + "v2/sandboxes").respond(200, json={"sandboxes": []})
    assert "authentication succeeded" in await provider.diagnose()
    assert listing.calls[0].request.url.params["project"] == "project-1"
    assert listing.calls[0].request.url.params["limit"] == "1"


def test_provider_is_registered():
    assert "vercel" in get_provider_names()
