import uuid
from collections.abc import Callable

import httpx
import pytest
import respx

from host_secrets.exceptions import SecretRefreshError
from secrets_exchange.client import SecretsExchange

URL = "http://127.0.0.1:8781"


async def test_refresh_sends_no_token_and_no_body() -> None:
    host_id = uuid.uuid4()

    with respx.mock as router:
        route = router.post(f"{URL}/refresh/{host_id}/github").respond(200)
        await SecretsExchange(URL).refresh(host_id, "github")

    request = route.calls.last.request
    assert not request.content
    assert "Authorization" not in request.headers


@pytest.mark.parametrize(
    "fail",
    [
        lambda route: route.respond(503, headers={"Retry-After": "5"}),
        lambda route: route.mock(side_effect=httpx.ConnectError("refused")),
        lambda route: route.mock(side_effect=httpx.ReadTimeout("slow")),
    ],
)
async def test_refresh_raises_when_the_exchange_cannot_refresh(
    fail: Callable[[respx.Route], object],
) -> None:
    host_id = uuid.uuid4()

    with respx.mock as router:
        fail(router.post(f"{URL}/refresh/{host_id}/github"))

        with pytest.raises(SecretRefreshError):
            await SecretsExchange(URL).refresh(host_id, "github")


async def test_diagnose_reports_a_healthy_exchange() -> None:
    with respx.mock as router:
        router.get(f"{URL}/healthz").respond(json={"status": "ok"})
        assert await SecretsExchange(URL).diagnose() == "exchange healthy"


async def test_diagnose_raises_on_an_unhealthy_exchange() -> None:
    with respx.mock as router:
        router.get(f"{URL}/healthz").respond(503)
        with pytest.raises(httpx.HTTPStatusError):
            await SecretsExchange(URL).diagnose()


def test_from_settings_reads_the_exchange_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRETS_EXCHANGE_PORT", "9876")
    assert SecretsExchange.from_settings().url == "http://127.0.0.1:9876"
