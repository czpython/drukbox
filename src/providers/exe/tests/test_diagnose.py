from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.exe.provider import ExeProvider
from providers.exe.settings import ExeSettings


def _settings() -> ExeSettings:
    return ExeSettings(api_token="test-token", default_image="img:latest")


@pytest.mark.asyncio
async def test_diagnose_returns_email_from_whoami() -> None:
    """The probe surfaces the authenticated identity directly from whoami."""
    api = MagicMock()
    api.whoami = AsyncMock(return_value={"email": "ops@example.com"})
    provider = ExeProvider(api, _settings(), docker=MagicMock())

    assert await provider.diagnose() == "ops@example.com"


@pytest.mark.asyncio
async def test_diagnose_raises_on_whoami_failure() -> None:
    """A whoami error surfaces so the orchestrator can classify it."""
    api = MagicMock()
    api.whoami = AsyncMock(side_effect=RuntimeError("403"))
    provider = ExeProvider(api, _settings(), docker=MagicMock())

    with pytest.raises(RuntimeError, match="403"):
        await provider.diagnose()
