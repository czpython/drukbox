import asyncio
import os

import pytest

from gateway.backend import SandboxSftpBackend
from gateway.tests.localprocess import SFTP_SERVER, LocalProcess

pytestmark = pytest.mark.skipif(
    not os.path.exists(SFTP_SERVER), reason="no local sftp-server to back the tests"
)


@pytest.fixture(autouse=True)
def _reset_open_count():
    LocalProcess.open_count = 0


def _backend(**kwargs) -> SandboxSftpBackend:
    return SandboxSftpBackend(LocalProcess, "sb-backend", **kwargs)


async def test_the_process_opens_lazily_on_first_session():
    backend = _backend()
    assert LocalProcess.open_count == 0
    async with backend.session() as handler:
        assert handler is not None
    assert LocalProcess.open_count == 1
    await backend.aclose()


async def test_many_operations_reuse_one_process():
    backend = _backend()
    for _ in range(15):
        async with backend.session():
            pass
    await backend.aclose()
    assert LocalProcess.open_count == 1


async def test_idle_close_and_lazy_reopen():
    backend = _backend(idle_close_seconds=0.05)
    async with backend.session():
        pass
    assert LocalProcess.open_count == 1

    await asyncio.sleep(0.2)
    async with backend.session():
        pass
    await backend.aclose()
    assert LocalProcess.open_count == 2


async def test_the_idle_timer_does_not_fire_while_an_operation_is_in_flight():
    backend = _backend(idle_close_seconds=0.05)
    async with backend.session():
        await asyncio.sleep(0.2)  # held open across the idle window
        async with backend.session():
            pass
    await backend.aclose()
    assert LocalProcess.open_count == 1


async def test_a_closed_backend_refuses_a_session():
    backend = _backend()
    async with backend.session():
        pass
    await backend.aclose()
    with pytest.raises(ConnectionError):
        async with backend.session():
            pass
