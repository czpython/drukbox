import json
import traceback
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from providers.exceptions import ProviderAuthError, ProviderCommandError, ProviderNotFoundError
from providers.registry import get_provider_names
from providers.vercel.api import VercelAPI
from providers.vercel.provider import VercelProvider
from providers.vercel.settings import VercelSettings

BASE = "https://vercel.com/api/"
COMMANDS = BASE + "v2/sandboxes/sessions/session-1/cmd"
CREATED = {"sandbox": {"name": "sb-test"}, "session": {"id": "session-1"}}


@pytest.fixture
async def provider() -> AsyncIterator[VercelProvider]:
    settings = VercelSettings(
        token="test-token", team_id="team-1", project_id="project-1", default_image="sandbox:v1"
    )
    provider = VercelProvider(VercelAPI(settings), settings, service_label="drukbox")
    yield provider
    await provider.aclose()


@respx.mock
@pytest.mark.parametrize(("instance_type", "vcpus"), [(None, 2), ("4", 4)])
async def test_create_bootstraps_sandbox_on_the_tailnet(
    provider: VercelProvider, instance_type: str | None, vcpus: int
):
    creation = respx.post(BASE + "v3/sandboxes").respond(201, json=CREATED)
    command = respx.post(COMMANDS).respond(200, json={"command": {"id": "command-1"}})
    completion = respx.get(COMMANDS + "/command-1").respond(200, json={"command": {"exitCode": 0}})
    result = await provider.create_vm(
        name="sb-test",
        image="custom:v2",
        setup_script="true",
        env={"FOO": "hello world"},
        instance_type=instance_type,
    )
    request = creation.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-token"
    assert request.url.params["teamId"] == "team-1"
    assert json.loads(request.content) == {
        "projectId": "project-1",
        "name": "sb-test",
        "image": "custom:v2",
        "resources": {"vcpus": vcpus},
        "timeout": 2_700_000,
        "persistent": False,
        "tags": {"managed-by": "drukbox"},
    }
    body = json.loads(command.calls[0].request.content)
    assert body["command"] == "bash"
    assert body["sudo"] is True
    assert "/etc/environment" in body["args"][-1]
    assert completion.calls[0].request.url.params["wait"] == "true"
    assert result.provider_id == "session-1"
    assert result.name == "sb-test"
    assert result.ssh_username == "root"
    assert not result.ssh_host


@respx.mock
async def test_requires_tailscale_before_api_calls(provider: VercelProvider):
    with pytest.raises(ProviderCommandError, match="TAILSCALE_ENABLED"):
        await provider.create_vm(name="sb-test", image="sandbox:v1")
    assert not respx.calls


@respx.mock
async def test_rejects_instance_type_that_is_not_a_vcpu_count(provider: VercelProvider):
    with pytest.raises(ProviderCommandError, match="vCPU count"):
        await provider.create_vm(
            name="sb-test", image="sandbox:v1", setup_script="true", instance_type="large"
        )
    assert not respx.calls


@respx.mock
async def test_rejected_create_names_vercel_reason(provider: VercelProvider):
    respx.post(BASE + "v3/sandboxes").respond(
        400, json={"error": {"code": "bad_request", "message": "Image not found"}}
    )
    with pytest.raises(ProviderCommandError, match="Image not found"):
        await provider.create_vm(name="sb-test", image="missing:v1", setup_script="true")


@respx.mock
@pytest.mark.parametrize(
    "command_response",
    [
        httpx.Response(200, json={"command": {"id": "command-1"}}),
        httpx.Response(400, json={"error": {"message": "secret output"}}),
    ],
)
async def test_failed_bootstrap_hides_output(
    provider: VercelProvider, command_response: httpx.Response
):
    respx.post(BASE + "v3/sandboxes").respond(201, json=CREATED)
    respx.post(COMMANDS).mock(return_value=command_response)
    respx.get(COMMANDS + "/command-1").respond(200, json={"command": {"exitCode": 1}})
    with pytest.raises(ProviderCommandError) as error:
        await provider.create_vm(name="sb-test", image="sandbox:v1", setup_script="true")
    assert "secret output" not in "".join(traceback.format_exception(error.value))


@respx.mock
async def test_delete_removes_sandbox_and_its_snapshots(provider: VercelProvider):
    deletion = respx.delete(BASE + "v2/sandboxes/sb-test").respond(200, json=CREATED)
    await provider.delete_vm("sb-test")
    params = deletion.calls[0].request.url.params
    assert params["projectId"] == "project-1"
    assert params["deleteOrphanSnapshots"] == "true"
    deletion.respond(404)
    with pytest.raises(ProviderNotFoundError):
        await provider.delete_vm("sb-test")


@respx.mock
async def test_diagnose_checks_authentication(provider: VercelProvider):
    listing = respx.get(BASE + "v2/sandboxes").respond(200, json={"sandboxes": []})
    assert "authentication succeeded" in await provider.diagnose()
    listing.respond(401)
    with pytest.raises(ProviderAuthError):
        await provider.diagnose()


def test_provider_is_registered():
    assert "vercel" in get_provider_names()
