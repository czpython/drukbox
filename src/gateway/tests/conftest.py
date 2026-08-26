import pytest

from gateway.tests.localprocess import SFTP_SERVER_COMMAND, LocalProcess


@pytest.fixture(autouse=True)
def _local_sftp_server_command(monkeypatch):
    # The tests back SFTP with the host's own sftp-server, which sits at a
    # different path than the sandbox image's. Point the backend at it.
    monkeypatch.setattr("gateway.backend._SFTP_SERVER_COMMAND", SFTP_SERVER_COMMAND)


@pytest.fixture(autouse=True)
def _local_home_root(tmp_path, monkeypatch):
    # Give each test a writable per-host home root, the way the sandbox
    # uses /home.
    monkeypatch.setattr(LocalProcess, "home_root", str(tmp_path))
