from unittest.mock import AsyncMock, MagicMock

import pytest

from providers.aws.exceptions import AwsTransportError
from providers.aws.provider import AWSProvider
from providers.aws.settings import AwsSettings


def _settings() -> AwsSettings:
    return AwsSettings(region="eu-central-1", default_image="ami-deadbeef")


@pytest.mark.asyncio
async def test_diagnose_returns_account_and_arn_from_caller_identity() -> None:
    """sts:GetCallerIdentity proves auth + region reachability in one call."""
    api = MagicMock()
    api.get_caller_identity = AsyncMock(
        return_value={
            "account": "111122223333",
            "arn": "arn:aws:iam::111122223333:user/drukbox",
        }
    )
    provider = AWSProvider(api, _settings())

    detail = await provider.diagnose()
    assert "account=111122223333" in detail
    assert "arn=arn:aws:iam::111122223333:user/drukbox" in detail


@pytest.mark.asyncio
async def test_diagnose_raises_on_caller_identity_failure() -> None:
    """STS errors propagate; the orchestrator owns classification + hint."""
    api = MagicMock()
    api.get_caller_identity = AsyncMock(
        side_effect=AwsTransportError("AccessDenied: not authorized")
    )
    provider = AWSProvider(api, _settings())

    with pytest.raises(AwsTransportError, match="AccessDenied"):
        await provider.diagnose()
