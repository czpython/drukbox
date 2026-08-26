import io
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiodocker import DockerError

from providers.docker.api import DockerAPI
from providers.docker.exceptions import (
    DockerImageNotFoundError,
    DockerTransportError,
    DockerVMNotFoundError,
)


def _fake_docker(**overrides: object) -> SimpleNamespace:
    """One canonical aiodocker fake used across tests; overrides per case."""
    container = SimpleNamespace(
        port=AsyncMock(return_value=[{"HostIp": "127.0.0.1", "HostPort": "49160"}]),
        delete=AsyncMock(),
    )
    fake = SimpleNamespace(
        containers=SimpleNamespace(
            run=AsyncMock(return_value=SimpleNamespace(id="abc123")),
            container=MagicMock(return_value=container),
        ),
        images=SimpleNamespace(
            build=AsyncMock(),
            delete=AsyncMock(),
            push=AsyncMock(),
        ),
        version=AsyncMock(return_value={"Version": "29.6.2"}),
        close=AsyncMock(),
    )
    for name, value in overrides.items():
        setattr(fake, name, value)
    return fake


def _api(fake: SimpleNamespace) -> DockerAPI:
    return DockerAPI(docker=fake)  # type: ignore[arg-type]


async def test_run_container_publishes_on_loopback_and_passes_env_in_the_body() -> None:
    fake = _fake_docker()

    container_id = await _api(fake).run_container(
        name="sb-test",
        image="sandbox:latest",
        env={"KEY": "value", "MULTI": "line one\nline two"},
        labels={"managed-by": "drukbox"},
    )

    assert container_id == "abc123"
    config = fake.containers.run.await_args.args[0]
    assert fake.containers.run.await_args.kwargs == {"name": "sb-test"}
    assert config["Image"] == "sandbox:latest"
    assert config["Env"] == ["KEY=value", "MULTI=line one\nline two"]
    assert config["Labels"] == {"managed-by": "drukbox"}
    assert config["HostConfig"]["PortBindings"] == {
        "22/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]
    }


async def test_run_container_translates_engine_errors() -> None:
    fake = _fake_docker()
    fake.containers.run.side_effect = DockerError(409, "name already in use")

    with pytest.raises(DockerTransportError, match="name already in use"):
        await _api(fake).run_container(name="sb-test", image="sandbox:latest", env={}, labels={})


async def test_published_ssh_port_reads_the_loopback_binding() -> None:
    fake = _fake_docker()

    assert await _api(fake).published_ssh_port("sb-test") == 49160
    fake.containers.container.assert_called_once_with("sb-test")


async def test_published_ssh_port_raises_when_container_published_nothing() -> None:
    fake = _fake_docker()
    fake.containers.container.return_value.port.return_value = None

    with pytest.raises(DockerTransportError, match="published no SSH port"):
        await _api(fake).published_ssh_port("sb-test")


async def test_missing_container_maps_to_not_found() -> None:
    fake = _fake_docker()
    fake.containers.container.return_value.port.side_effect = DockerError(404, "No such container")

    with pytest.raises(DockerVMNotFoundError):
        await _api(fake).published_ssh_port("sb-test")


async def test_remove_container_forces_and_drops_volumes() -> None:
    fake = _fake_docker()

    await _api(fake).remove_container("sb-test")

    fake.containers.container.return_value.delete.assert_awaited_once_with(force=True, v=True)


async def test_build_image_sends_the_context_tar_under_the_tag() -> None:
    fake = _fake_docker()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz"):
        pass
    context_tar = buffer.getvalue()

    await _api(fake).build_image("drukbox-template:123456789abc", context_tar)

    kwargs = fake.images.build.await_args.kwargs
    assert kwargs["tag"] == "drukbox-template:123456789abc"
    assert kwargs["encoding"] == "gzip"
    assert kwargs["fileobj"].getvalue() == context_tar


async def test_build_failure_keeps_the_engine_detail() -> None:
    fake = _fake_docker()
    fake.images.build.side_effect = DockerError(0, "RUN sh /drukbox-setup.sh: exit code 127")

    with pytest.raises(DockerTransportError, match="exit code 127"):
        await _api(fake).build_image("drukbox-template:123456789abc", b"")


async def test_missing_image_maps_to_not_found() -> None:
    fake = _fake_docker()
    fake.images.delete.side_effect = DockerError(404, "No such image")

    with pytest.raises(DockerImageNotFoundError):
        await _api(fake).remove_image("drukbox-template:missing")


async def test_push_image_sends_per_call_credentials() -> None:
    fake = _fake_docker()

    await _api(fake).push_image(
        "ghcr.io/acme/template:tag", username="builder", password="registry-secret"
    )

    fake.images.push.assert_awaited_once_with(
        "ghcr.io/acme/template:tag",
        auth={"username": "builder", "password": "registry-secret"},
    )


async def test_push_image_without_credentials_sends_no_auth() -> None:
    fake = _fake_docker()

    await _api(fake).push_image("ghcr.io/acme/template:tag")

    assert fake.images.push.await_args.kwargs == {"auth": None}


async def test_server_version_reads_the_engine_version() -> None:
    fake = _fake_docker()

    assert await _api(fake).server_version() == "29.6.2"


async def test_unreachable_daemon_maps_to_transport_error() -> None:
    fake = _fake_docker()
    fake.version.side_effect = OSError("connection refused")

    with pytest.raises(DockerTransportError, match="connection refused"):
        await _api(fake).server_version()


async def test_aclose_closes_the_client_once_created() -> None:
    fake = _fake_docker()
    api = _api(fake)

    await api.server_version()
    await api.aclose()
    await api.aclose()

    fake.close.assert_awaited_once_with()
