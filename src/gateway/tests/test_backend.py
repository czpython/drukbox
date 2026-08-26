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


async def test_the_process_opens_once_and_is_reused():
    backend = _backend()
    assert LocalProcess.open_count == 0
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


async def test_a_failed_handshake_closes_the_process(monkeypatch):
    # A handshake failure must not leak the exec process, or the sandbox
    # would stay awake on a live session.
    closed = []
    real_open = LocalProcess.open

    async def open_and_track(cls, name, *, command, terminal):
        process = await real_open.__func__(cls, name, command=command, terminal=terminal)
        original_aclose = process.aclose

        async def tracked_aclose():
            closed.append(process)
            await original_aclose()

        process.aclose = tracked_aclose
        return process

    monkeypatch.setattr(LocalProcess, "open", classmethod(open_and_track))
    monkeypatch.setattr(
        "gateway.backend.SFTPClientHandler.start",
        _raise_handshake,
    )

    backend = _backend()
    with pytest.raises(RuntimeError, match="handshake"):
        async with backend.session():
            pass
    assert len(closed) == 1
    await backend.aclose()


async def _raise_handshake(self):
    raise RuntimeError("handshake failed")


async def test_a_closed_backend_refuses_a_session():
    backend = _backend()
    async with backend.session():
        pass
    await backend.aclose()
    with pytest.raises(ConnectionError):
        async with backend.session():
            pass
