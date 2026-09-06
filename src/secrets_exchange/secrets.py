import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

# A value is fetched again when less than this remains of its life.
MARGIN = timedelta(minutes=1)
# The wait after a failed fetch doubles with each failure, up to the longest.
FIRST_RETRY = timedelta(seconds=5)
LONGEST_RETRY = timedelta(minutes=1)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class IssuerError(Exception):
    """The message is safe to log."""


class IssuerUnavailableError(Exception):
    """No valid secret exists for the entry."""


class Secret(BaseModel):
    """An issuer answers with this shape."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    value: str = Field(min_length=1)
    expires_at: AwareDatetime | None = None

    @classmethod
    def static(cls, value: str) -> Self:
        return cls(value=value)

    @classmethod
    async def fetch(cls, issuer: dict[str, Any], client: httpx.AsyncClient) -> Self:
        try:
            response = await client.get(issuer["url"], headers=issuer["headers"])
            response.raise_for_status()
            secret = cls.model_validate(response.json())
        except httpx.HTTPStatusError as exc:
            raise IssuerError(f"status {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise IssuerError(type(exc).__name__) from exc
        except json.JSONDecodeError:
            raise IssuerError("answer is not JSON") from None
        except ValidationError:
            # The chain would carry the answer, and a token with it.
            raise IssuerError("answer has the wrong shape") from None
        if not secret.expires_at:
            interval = issuer["refresh"]
            lifetime = timedelta(seconds=int(interval[:-1]) * _UNITS[interval[-1]])
            secret = cls(value=secret.value, expires_at=datetime.now(UTC) + lifetime)
        if not secret.is_valid(datetime.now(UTC)):
            raise IssuerError("answer has already expired")
        return secret

    def is_valid(self, at: datetime) -> bool:
        return not self.expires_at or at < self.expires_at

    def is_stale(self, at: datetime) -> bool:
        return bool(self.expires_at) and at >= self.expires_at - MARGIN


@dataclass
class RefreshableSecret:
    """The latest value, the fetch that runs now, and the next permitted attempt."""

    latest: Secret | None = None
    fetching: asyncio.Task[None] | None = None
    next_attempt: datetime = field(default_factory=lambda: datetime.now(UTC))
    wait: timedelta = FIRST_RETRY

    async def refresh(self, issuer: dict[str, Any], client: httpx.AsyncClient) -> None:
        if datetime.now(UTC) < self.next_attempt:
            return
        try:
            self.latest = await Secret.fetch(issuer, client)
        except IssuerError as exc:
            logger.warning("issuer %s failed: %s", issuer["url"], exc)
            self.next_attempt = datetime.now(UTC) + self.wait
            self.wait = min(self.wait * 2, LONGEST_RETRY)
        else:
            self.wait = FIRST_RETRY

    def refresh_in_background(self, issuer: dict[str, Any], client: httpx.AsyncClient) -> None:
        if not self.fetching:
            self.fetching = asyncio.create_task(self.refresh(issuer, client))
            self.fetching.add_done_callback(lambda _: setattr(self, "fetching", None))


class Secrets:
    """The current secret per entry. A fetched value is kept in memory, served
    stale while a refresh runs or fails, and never written back."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._refreshable: dict[tuple[uuid.UUID, str], RefreshableSecret] = {}

    async def current(self, host_id: uuid.UUID, service: str, entry: dict[str, Any]) -> Secret:
        if "value" in entry:
            return Secret.static(entry["value"])
        refreshable = self._refreshable.setdefault((host_id, service), RefreshableSecret())
        now = datetime.now(UTC)
        if refreshable.latest and refreshable.latest.is_valid(now):
            if refreshable.latest.is_stale(now):
                refreshable.refresh_in_background(entry["issuer"], self._client)
            return refreshable.latest
        await refreshable.refresh(entry["issuer"], self._client)
        if refreshable.latest and refreshable.latest.is_valid(datetime.now(UTC)):
            return refreshable.latest
        raise IssuerUnavailableError(f"no valid secret for {host_id}/{service}")
