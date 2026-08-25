from unittest.mock import AsyncMock

import janitor


async def test_reap_runs_both_reapers(monkeypatch):
    """One cron entry sweeps hosts and templates."""
    hosts_reaper = AsyncMock()
    templates_reaper = AsyncMock()
    monkeypatch.setattr(janitor, "reap_expired_hosts", hosts_reaper)
    monkeypatch.setattr(janitor, "reap_templates", templates_reaper)

    await janitor.reap()

    hosts_reaper.assert_awaited_once_with()
    templates_reaper.assert_awaited_once_with()
