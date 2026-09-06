import json
import re

import httpx
import pytest
import respx

from providers.exe.api import ExeAPI, _encode_setup_script
from providers.exe.exceptions import (
    ExeAuthError,
    ExeCommandError,
    ExeIntegrationAlreadyExistsError,
    ExeIntegrationNotFoundError,
    ExeResponseError,
    ExeVMNotFoundError,
)


def _api() -> ExeAPI:
    return ExeAPI(base_url="https://exe.dev", token="token")


def test_parse_json_response_accepts_single_object():
    assert _api()._parse_json_response('{"vm_name": "vm-1"}') == {"vm_name": "vm-1"}


def test_parse_json_response_merges_json_lines():
    assert _api()._parse_json_response('{"vm_name": "vm-1"}\n{"ssh_port": 22}\n') == {
        "vm_name": "vm-1",
        "ssh_port": 22,
    }


def test_parse_json_response_rejects_empty_output():
    with pytest.raises(ExeResponseError, match="empty output"):
        _api()._parse_json_response("")


def test_encode_setup_script_serializes_newlines_quotes_and_backslashes():
    encoded = _encode_setup_script('#!/bin/bash\necho "hi"; echo \\done')
    assert encoded == '"#!/bin/bash\\necho \\"hi\\"; echo \\\\done"'


def test_encode_setup_script_preserves_dollar_signs_and_shell_metas():
    # exe.dev's parser only unescapes \n, \", and \\ inside double quotes;
    # everything else (including $, `, !) passes through unchanged.
    encoded = _encode_setup_script('echo "$HOME" `date`')
    assert encoded == '"echo \\"$HOME\\" `date`"'


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_create_vm_includes_setup_script_in_command(respx_mock):
    route = respx_mock.post("/exec").mock(
        return_value=httpx.Response(200, content=b'{"vm_name": "sb-1", "ssh_port": 22}'),
    )

    await _api().create_vm(
        name="sb-1",
        image="ubuntu:22.04",
        setup_script='#!/bin/bash\necho "ready"',
    )

    body = route.calls.last.request.content.decode()
    assert body.startswith("new --json --name=sb-1 --image=ubuntu:22.04 ")
    assert '--setup-script="#!/bin/bash\\necho \\"ready\\""' in body


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_create_vm_exports_env_after_setup_script_shebang(respx_mock):
    route = respx_mock.post("/exec").mock(
        return_value=httpx.Response(200, content=b'{"vm_name": "sb-1", "ssh_port": 22}'),
    )

    await _api().create_vm(
        name="sb-1",
        image="ubuntu:22.04",
        env={"FOO": "bar baz", "EMPTY": ""},
        setup_script="#!/bin/bash\necho ready",
    )

    body = route.calls.last.request.content.decode()
    assert "--env 'FOO=bar baz' --env EMPTY=" in body
    assert _setup_script(body) == [
        "#!/bin/bash",
        "export FOO='bar baz'",
        "export EMPTY=''",
        "cat > ~/.bashrc.new <<'DRUKBOX_ENV'",
        "export FOO='bar baz'",
        "export EMPTY=''",
        "DRUKBOX_ENV",
        "cat ~/.bashrc >> ~/.bashrc.new && mv ~/.bashrc.new ~/.bashrc",
        "echo ready",
    ]


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_create_vm_sends_a_setup_script_for_env_alone(respx_mock):
    # exe puts --env where only a login shell reads it. The setup script
    # writes the exports to ~/.bashrc, which every bash session reads.
    route = respx_mock.post("/exec").mock(
        return_value=httpx.Response(200, content=b'{"vm_name": "sb-1", "ssh_port": 22}'),
    )

    await _api().create_vm(name="sb-1", image="ubuntu:22.04", env={"FOO": "bar"})

    body = route.calls.last.request.content.decode()
    assert "--env FOO=bar" in body
    assert _setup_script(body) == [
        "#!/bin/bash",
        "export FOO=bar",
        "cat > ~/.bashrc.new <<'DRUKBOX_ENV'",
        "export FOO=bar",
        "DRUKBOX_ENV",
        "cat ~/.bashrc >> ~/.bashrc.new && mv ~/.bashrc.new ~/.bashrc",
    ]


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_create_vm_installs_the_proxy_ca_with_sudo(respx_mock):
    # The setup script runs as a user with passwordless sudo, not as root.
    route = respx_mock.post("/exec").mock(
        return_value=httpx.Response(200, content=b'{"vm_name": "sb-1", "ssh_port": 22}'),
    )

    await _api().create_vm(name="sb-1", image="ubuntu:22.04", env={"SECRETS_PROXY_CA": "Q0VSVA=="})

    assert _setup_script(route.calls.last.request.content.decode())[-2:] == [
        "printf '%s' \"$SECRETS_PROXY_CA\" | base64 -d | sudo -n tee "
        "/usr/local/share/ca-certificates/drukbox.crt >/dev/null || exit 1",
        "sudo -n update-ca-certificates >/dev/null || exit 1",
    ]


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_create_vm_sets_git_up_with_sudo_for_a_github_secret(respx_mock):
    route = respx_mock.post("/exec").mock(
        return_value=httpx.Response(200, content=b'{"vm_name": "sb-1", "ssh_port": 22}'),
    )

    await _api().create_vm(name="sb-1", image="ubuntu:22.04", env={"GH_TOKEN": "drk.a.github.b"})

    lines = _setup_script(route.calls.last.request.content.decode())
    assert lines[-4] == (
        "sudo -n git config --system --replace-all credential.https://github.com.helper '' "
        "|| exit 1"
    )
    assert lines[-1] == (
        "sudo -n git config --system --add url.https://github.com/.insteadOf "
        "ssh://git@github.com/ || exit 1"
    )


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev", assert_all_called=False)
async def test_create_vm_refuses_a_setup_script_over_the_exe_limit(respx_mock):
    route = respx_mock.post("/exec")

    with pytest.raises(ExeCommandError, match=r"over the exe\.dev limit"):
        await _api().create_vm(name="sb-1", image="ubuntu:22.04", env={"BIG": "a" * 6000})

    assert route.call_count == 0


def _setup_script(body: str) -> list[str]:
    """The setup script exe receives, as lines."""
    quoted = re.search(r'--setup-script=("(?:[^"\\]|\\.)*")', body)
    assert quoted
    return json.loads(quoted.group(1)).split("\n")


@pytest.mark.asyncio
async def test_create_vm_rejects_invalid_setup_env_name():
    # environment.export raises for a key that is not a shell identifier.
    with pytest.raises(ValueError, match="invalid VM environment variable name"):
        await _api().create_vm(
            name="sb-1",
            image="ubuntu:22.04",
            env={"BAD-NAME": "value"},
            setup_script="#!/bin/bash\necho ready",
        )


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_create_vm_emits_tag_flag_for_each_tag(respx_mock):
    route = respx_mock.post("/exec").mock(
        return_value=httpx.Response(200, content=b'{"vm_name": "sb-1", "ssh_port": 22}'),
    )

    await _api().create_vm(
        name="sb-1",
        image="ubuntu:22.04",
        tags=["managed-by-drukbox-prod"],
    )

    body = route.calls.last.request.content.decode()
    assert "--tag=managed-by-drukbox-prod" in body


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_create_vm_emits_registry_auth_flag(respx_mock):
    route = respx_mock.post("/exec").mock(
        return_value=httpx.Response(200, content=b'{"vm_name": "sb-1", "ssh_port": 22}'),
    )

    await _api().create_vm(
        name="sb-1",
        image="ghcr.io/acme/templates:abc123",
        registry_auth="bot:secret",
    )

    body = route.calls.last.request.content.decode()
    assert "--registry-auth=bot:secret" in body


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_create_vm_omits_setup_script_when_none(respx_mock):
    route = respx_mock.post("/exec").mock(
        return_value=httpx.Response(200, content=b'{"vm_name": "sb-1", "ssh_port": 22}'),
    )

    await _api().create_vm(name="sb-1", image="ubuntu:22.04")

    body = route.calls.last.request.content.decode()
    assert "--setup-script" not in body


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_request_maps_auth_error(respx_mock):
    respx_mock.post("/exec").mock(
        return_value=httpx.Response(401, json={"error": "auth"}),
    )

    with pytest.raises(ExeAuthError):
        await _api()._request("whoami")


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_request_maps_command_error(respx_mock):
    respx_mock.post("/exec").mock(
        return_value=httpx.Response(400, content=b"bad command"),
    )

    with pytest.raises(ExeCommandError, match="bad command"):
        await _api()._request("bad")


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_request_sends_authorization_and_text_body(respx_mock):
    route = respx_mock.post("/exec").mock(return_value=httpx.Response(200, content=b"{}"))

    await _api()._request("whoami --json")

    call = route.calls.last
    assert call.request.headers["authorization"] == "Bearer token"
    assert call.request.headers["content-type"].startswith("text/plain")
    assert call.request.content == b"whoami --json"


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_request_names_exception_type_for_blank_transport_error(respx_mock):
    read_timeout = httpx.ReadTimeout("")
    respx_mock.post("/exec").mock(side_effect=read_timeout)

    with pytest.raises(ExeResponseError, match="ReadTimeout") as exc_info:
        await _api()._request("new --json")

    assert exc_info.value.__cause__ is read_timeout


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_create_vm_raises_read_timeout_floor_when_general_timeout_below(respx_mock):
    route = respx_mock.post("/exec").mock(
        return_value=httpx.Response(200, content=b'{"vm_name": "sb-1", "ssh_port": 22}'),
    )

    # The default 30s timeout sits below the creation floor.
    await ExeAPI(base_url="https://exe.dev", token="token").create_vm(
        name="sb-1", image="ubuntu:22.04"
    )

    timeout = route.calls.last.request.extensions["timeout"]
    assert timeout["read"] == 90.0
    assert timeout["connect"] == 5.0
    assert timeout["write"] == 30.0
    assert timeout["pool"] == 30.0


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_create_vm_keeps_configured_timeout_when_above_floor(respx_mock):
    route = respx_mock.post("/exec").mock(
        return_value=httpx.Response(200, content=b'{"vm_name": "sb-1", "ssh_port": 22}'),
    )

    await ExeAPI(base_url="https://exe.dev", token="token", timeout=120.0).create_vm(
        name="sb-1", image="ubuntu:22.04"
    )

    assert route.calls.last.request.extensions["timeout"]["read"] == 120.0


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_ordinary_command_after_create_vm_uses_general_timeout(respx_mock):
    route = respx_mock.post("/exec").mock(
        return_value=httpx.Response(200, content=b'{"vm_name": "sb-1", "ssh_port": 22}'),
    )

    # Creation must not leak its bigger budget into later commands on the client.
    api = ExeAPI(base_url="https://exe.dev", token="token")
    await api.create_vm(name="sb-1", image="ubuntu:22.04")
    await api.whoami()

    assert route.calls.last.request.extensions["timeout"]["read"] == 30.0


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_delete_vm_uses_rm_json_command(respx_mock):
    route = respx_mock.post("/exec").mock(
        return_value=httpx.Response(200, content=b'{"vm_name": "vm-1"}'),
    )

    payload = await _api().delete_vm("vm-1")

    assert payload == {"vm_name": "vm-1"}
    assert route.calls.last.request.content == b"rm --json vm-1"


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_delete_vm_maps_not_found_error(respx_mock):
    respx_mock.post("/exec").mock(
        return_value=httpx.Response(400, content=b"vm 'vm-1' not found"),
    )

    with pytest.raises(ExeVMNotFoundError, match="vm-1"):
        await _api().delete_vm("vm-1")


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_create_http_proxy_uses_expected_command(respx_mock):
    route = respx_mock.post("/exec").mock(return_value=httpx.Response(200, content=b""))

    await _api().create_http_proxy(
        name="mirror",
        target="https://httpbin.org/",
        headers={
            "Authorization": "Bearer token",
            "X-Test": "value",
        },
    )

    assert route.calls.last.request.content == (
        b"integrations add http-proxy --name=mirror --target=https://httpbin.org/ "
        b"--header='Authorization: Bearer token' --header='X-Test: value'"
    )


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_create_http_proxy_maps_already_exists(respx_mock):
    respx_mock.post("/exec").mock(
        return_value=httpx.Response(400, content=b"integration already exists"),
    )

    with pytest.raises(ExeIntegrationAlreadyExistsError):
        await _api().create_http_proxy(
            name="mirror",
            target="https://httpbin.org/",
            headers={"Authorization": "Bearer token"},
        )


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_create_http_proxy_accepts_empty_success_output(respx_mock):
    route = respx_mock.post("/exec").mock(return_value=httpx.Response(200, content=b""))

    await _api().create_http_proxy(
        name="mirror",
        target="https://httpbin.org/",
        headers={"Authorization": "Bearer token"},
    )

    assert route.calls.last.request.content == (
        b"integrations add http-proxy --name=mirror --target=https://httpbin.org/ "
        b"--header='Authorization: Bearer token'"
    )


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_update_http_proxy_uses_expected_command(respx_mock):
    route = respx_mock.post("/exec").mock(return_value=httpx.Response(200, content=b""))

    await _api().update_http_proxy(
        name="mirror",
        target="https://httpbin.org/",
        headers={"Authorization": "Bearer next-token"},
    )

    assert route.calls.last.request.content == (
        b"integrations edit mirror --target=https://httpbin.org/ "
        b"--header='Authorization: Bearer next-token'"
    )


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_list_http_proxies_uses_json_command(respx_mock):
    route = respx_mock.post("/exec").mock(
        return_value=httpx.Response(
            200,
            json=[{"name": "sb-one--acme", "type": "http-proxy"}],
        )
    )

    integrations = await _api().list_http_proxies()

    assert integrations == ["sb-one--acme"]
    assert route.calls.last.request.content == b"integrations list --json"


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_list_http_proxies_rejects_invalid_integration_name(respx_mock):
    respx_mock.post("/exec").mock(
        return_value=httpx.Response(200, json=[{"name": None, "type": "http-proxy"}])
    )

    with pytest.raises(ExeResponseError, match="invalid name"):
        await _api().list_http_proxies()


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_delete_http_proxy_maps_not_found(respx_mock):
    respx_mock.post("/exec").mock(
        return_value=httpx.Response(400, content=b"integration not found"),
    )

    with pytest.raises(ExeIntegrationNotFoundError):
        await _api().delete_http_proxy("missing")


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_attach_http_proxy_uses_expected_command(respx_mock):
    route = respx_mock.post("/exec").mock(return_value=httpx.Response(200, content=b""))

    await _api().attach_http_proxy("mirror", attach_vm="vm-1")

    assert route.calls.last.request.content == b"integrations attach mirror vm:vm-1"


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_attach_http_proxy_maps_missing_vm(respx_mock):
    respx_mock.post("/exec").mock(
        return_value=httpx.Response(400, content=b"vm vm-1 not found"),
    )

    with pytest.raises(ExeVMNotFoundError):
        await _api().attach_http_proxy("mirror", attach_vm="vm-1")


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_detach_http_proxy_uses_expected_command(respx_mock):
    route = respx_mock.post("/exec").mock(return_value=httpx.Response(200, content=b""))

    await _api().detach_http_proxy("mirror", attach_vm="vm-1")

    assert route.calls.last.request.content == b"integrations detach mirror vm:vm-1"


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_detach_http_proxy_maps_missing_vm(respx_mock):
    respx_mock.post("/exec").mock(
        return_value=httpx.Response(400, content=b"vm vm-1 not found"),
    )

    with pytest.raises(ExeVMNotFoundError):
        await _api().detach_http_proxy("mirror", attach_vm="vm-1")


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_attach_http_proxy_routes_integration_not_found_even_when_message_mentions_vm(
    respx_mock,
):
    respx_mock.post("/exec").mock(
        return_value=httpx.Response(400, content=b"integration 'mirror' not found for vm:vm-1"),
    )

    with pytest.raises(ExeIntegrationNotFoundError):
        await _api().attach_http_proxy("mirror", attach_vm="vm-1")


@pytest.mark.asyncio
@respx.mock(base_url="https://exe.dev")
async def test_detach_http_proxy_routes_integration_not_found_even_when_message_mentions_vm(
    respx_mock,
):
    respx_mock.post("/exec").mock(
        return_value=httpx.Response(400, content=b"http-proxy 'mirror' not found on vm:vm-1"),
    )

    with pytest.raises(ExeIntegrationNotFoundError):
        await _api().detach_http_proxy("mirror", attach_vm="vm-1")
