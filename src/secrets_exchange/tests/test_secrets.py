import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from secrets_exchange.secrets import (
    Secret,
    Secrets,
    SourceError,
    SourceUnavailableError,
)

SOURCE = {
    "url": "https://mint.test/box/github",
    "headers": {"Authorization": "Bearer d2d"},
    "refresh": "1h",
}
ENTRY = {"source": SOURCE, "placeholder_fingerprint": "abc"}


@pytest.fixture
async def secrets():
    async with httpx.AsyncClient() as client:
        yield Secrets(client)


@respx.mock
async def test_a_source_is_fetched_once_and_kept_until_it_ages(secrets) -> None:
    route = respx.get(SOURCE["url"]).respond(json={"value": "ghs_one"})

    first = await secrets.current(uuid.uuid4(), "github", ENTRY)
    second = await secrets.current(first_key := uuid.uuid4(), "github", ENTRY)

    assert first.value == second.value == "ghs_one"
    assert route.calls[0].request.headers["Authorization"] == "Bearer d2d"
    assert route.call_count == 2, "each entry has its own secret"
    assert (await secrets.current(first_key, "github", ENTRY)).value == "ghs_one"
    assert route.call_count == 2, "a held secret is not fetched again"


@respx.mock
async def test_a_static_entry_never_touches_the_source(secrets) -> None:
    route = respx.get(SOURCE["url"])

    secret = await secrets.current(uuid.uuid4(), "github", {"value": "ghs_static"})

    assert secret == Secret(value="ghs_static")
    assert route.call_count == 0


@respx.mock
async def test_the_source_expiry_wins_over_the_refresh_interval() -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    respx.get(SOURCE["url"]).respond(json={"value": "x", "expires_at": expires_at.isoformat()})
    async with httpx.AsyncClient() as client:
        secret = await Secret.fetch(SOURCE, client)
    assert secret.expires_at == expires_at


@respx.mock
async def test_a_value_near_its_end_is_fetched_again_and_the_old_one_serves_meanwhile(
    secrets,
) -> None:
    host_id = uuid.uuid4()
    soon = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    route = respx.get(SOURCE["url"]).respond(json={"value": "ghs_old", "expires_at": soon})
    assert (await secrets.current(host_id, "github", ENTRY)).value == "ghs_old"

    route.respond(json={"value": "ghs_new", "expires_at": soon})
    assert (await secrets.current(host_id, "github", ENTRY)).value == "ghs_old"

    await _eventually(lambda: route.call_count == 2)
    assert (await secrets.current(host_id, "github", ENTRY)).value == "ghs_new"


@respx.mock
@pytest.mark.parametrize(
    "answer",
    [
        {"status_code": 500},
        {"json": {"token": "wrong-shape"}},
        {"json": {"value": ""}},
        {"json": {"value": "x", "expires_at": "2026-01-01T00:00:00"}},
        {"text": "not json"},
    ],
)
async def test_nothing_usable_and_nothing_held_is_unavailable_then_waits(secrets, answer) -> None:
    host_id = uuid.uuid4()
    route = respx.get(SOURCE["url"]).respond(**answer)

    with pytest.raises(SourceUnavailableError):
        await secrets.current(host_id, "github", ENTRY)
    route.respond(json={"value": "ghs_fine"})
    with pytest.raises(SourceUnavailableError):
        await secrets.current(host_id, "github", ENTRY)

    assert route.call_count == 1, "the second request waits out the retry delay"


@respx.mock
async def test_a_wrong_answer_is_logged_without_its_content(secrets, caplog) -> None:
    respx.get(SOURCE["url"]).respond(json={"token": "secret-xyz"})

    with caplog.at_level(logging.WARNING), pytest.raises(SourceUnavailableError):
        await secrets.current(uuid.uuid4(), "github", ENTRY)

    assert "answer has the wrong shape" in caplog.text
    assert "secret-xyz" not in caplog.text


@respx.mock
async def test_an_answer_that_has_already_expired_is_a_failure(secrets) -> None:
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    respx.get(SOURCE["url"]).respond(json={"value": "ghs_dead", "expires_at": past})

    with pytest.raises(SourceUnavailableError):
        await secrets.current(uuid.uuid4(), "github", ENTRY)


async def test_a_value_that_expires_during_a_failed_fetch_is_not_served(
    secrets, monkeypatch
) -> None:
    key = (uuid.uuid4(), "github")

    async def slow_failure(cls, source, client):
        await asyncio.sleep(0.3)
        raise SourceError("ConnectError")

    with respx.mock:
        soon = (datetime.now(UTC) + timedelta(seconds=0.2)).isoformat()
        respx.get(SOURCE["url"]).respond(json={"value": "ghs_old", "expires_at": soon})
        assert (await secrets.current(*key, ENTRY)).value == "ghs_old"
    await asyncio.sleep(0.25)

    monkeypatch.setattr(Secret, "fetch", classmethod(slow_failure))
    with pytest.raises(SourceUnavailableError):
        await secrets.current(*key, ENTRY)


async def test_the_old_value_serves_while_a_fetch_is_under_way(secrets, monkeypatch) -> None:
    key = (uuid.uuid4(), "github")
    with respx.mock:
        soon = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
        respx.get(SOURCE["url"]).respond(json={"value": "ghs_old", "expires_at": soon})
        assert (await secrets.current(*key, ENTRY)).value == "ghs_old"
    release = asyncio.Event()
    fetches = 0

    async def paused_fetch(cls, source, client):
        nonlocal fetches
        fetches += 1
        await release.wait()
        return Secret(value="ghs_new", expires_at=datetime.now(UTC) + timedelta(hours=1))

    monkeypatch.setattr(Secret, "fetch", classmethod(paused_fetch))
    assert (await secrets.current(*key, ENTRY)).value == "ghs_old"
    assert (await secrets.current(*key, ENTRY)).value == "ghs_old"
    await asyncio.sleep(0)
    assert fetches == 1, "one fetch at a time per entry"

    release.set()
    async with asyncio.timeout(1):
        while (await secrets.current(*key, ENTRY)).value != "ghs_new":
            await asyncio.sleep(0.01)


async def test_the_retry_wait_starts_when_a_slow_fetch_fails(secrets, monkeypatch) -> None:
    key = (uuid.uuid4(), "github")
    attempts = 0

    async def slow_failure(cls, source, client):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.2)
        raise SourceError("ReadTimeout")

    monkeypatch.setattr(Secret, "fetch", classmethod(slow_failure))
    monkeypatch.setattr("secrets_exchange.secrets.FIRST_RETRY", timedelta(seconds=0.1))
    with pytest.raises(SourceUnavailableError):
        await secrets.current(*key, ENTRY)
    with pytest.raises(SourceUnavailableError):
        await secrets.current(*key, ENTRY)

    assert attempts == 1, "the wait counts from the failure, not from the start of the fetch"


async def _eventually(check) -> None:
    async with asyncio.timeout(1):
        while not check():
            await asyncio.sleep(0.01)
