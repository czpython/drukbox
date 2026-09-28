from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.docker_sbx.exceptions import DockerSbxTransportError
from providers.docker_sbx.provider import DockerSbxProvider
from providers.docker_sbx.settings import DockerSbxSettings


def _provider(api: MagicMock) -> DockerSbxProvider:
    return DockerSbxProvider(api, DockerSbxSettings(), docker=MagicMock())


@pytest.mark.asyncio
async def test_diagnose_reports_daemon_reachability():
    api = MagicMock()
    api.sandbox_count = AsyncMock(return_value=2)
    api.check_ssh_endpoint = AsyncMock()

    assert (
        await _provider(api).diagnose() == "sandboxd reachable, 2 sandbox(es), SSH endpoint ready"
    )


@pytest.mark.asyncio
async def test_diagnose_raises_when_the_ssh_endpoint_is_unreachable():
    api = MagicMock()
    api.sandbox_count = AsyncMock(return_value=2)
    api.check_ssh_endpoint = AsyncMock(
        side_effect=DockerSbxTransportError("the sandboxd SSH endpoint is disabled")
    )

    with pytest.raises(DockerSbxTransportError):
        await _provider(api).diagnose()


@pytest.mark.asyncio
async def test_diagnose_raises_so_doctor_can_classify_the_failure():
    api = MagicMock()
    api.sandbox_count = AsyncMock(
        side_effect=DockerSbxTransportError("Not authenticated to Docker")
    )

    with pytest.raises(DockerSbxTransportError):
        await _provider(api).diagnose()
