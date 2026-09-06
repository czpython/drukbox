"""The secret the exchange hands the proxy for one entry."""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from providers.base import SecretInjectionCapability
from providers.exceptions import ProviderError

logger = logging.getLogger(__name__)

# A fetched value is fetched again when less than this remains of its life.
MARGIN = timedelta(minutes=1)
# After a failed fetch the exchange waits before it asks the issuer again.
# The wait doubles with each failure, up to the longest.
FIRST_RETRY = timedelta(seconds=5)
LONGEST_RETRY = timedelta(minutes=1)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class IssuerError(Exception):
    """One fetch gave no usable answer. The message is safe to log."""


class IssuerUnavailableError(Exception):
    """No valid secret exists for the entry."""


class Secret(BaseModel):
    """A value and when it expires. An issuer answers with this shape."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    value: str = Field(min_length=1)
    expires_at: AwareDatetime | None = None

    @classmethod
    def static(cls, value: str) -> Self:
        return cls(value=value)

    @classmethod
    async def fetch(cls, issuer: dict[str, Any], client: httpx.AsyncClient) -> Self:
        """Ask the issuer for its current value. Raises ``IssuerError``."""
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
            # The chain would carry the answer, and a token under a wrong key with it.
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
    """A secret that knows how to refresh itself. It holds the latest value from
    the issuer, the value a provider that holds values has, the fetch that runs
    now, and the time of the next permitted attempt."""

    latest: Secret | None = None
    pushed: Secret | None = None
    fetching: asyncio.Task[None] | None = None
    next_attempt: datetime = field(default_factory=lambda: datetime.now(UTC))
    wait: timedelta = FIRST_RETRY

    async def refresh(self, issuer: dict[str, Any], client: httpx.AsyncClient) -> None:
        """Fetch the secret once. Do nothing before the next permitted attempt."""
        if datetime.now(UTC) < self.next_attempt:
            return
        try:
            self.latest = await Secret.fetch(issuer, client)
        except IssuerError as exc:
            # The URL is an address, not a secret. It makes the log useful.
            logger.warning("issuer %s failed: %s", issuer["url"], exc)
            self.retry_later()
        else:
            self.wait = FIRST_RETRY

    def retry_later(self) -> None:
        """Wait before the next attempt, longer after each failure, up to the longest."""
        self.next_attempt = datetime.now(UTC) + self.wait
        self.wait = min(self.wait * 2, LONGEST_RETRY)

    def unpushed(self, at: datetime) -> Secret | None:
        """The latest value, when it is valid and the provider does not have it yet."""
        if self.latest and self.latest is not self.pushed and self.latest.is_valid(at):
            return self.latest
        return None

    def refresh_in_background(self, issuer: dict[str, Any], client: httpx.AsyncClient) -> None:
        """Start a fetch when none runs."""
        if not self.fetching:
            self.fetching = asyncio.create_task(self.refresh(issuer, client))
            self.fetching.add_done_callback(lambda _: setattr(self, "fetching", None))


class Secrets:
    """The current secret for each entry.

    A static value comes from the entry. A refreshable secret is fetched on
    first use and kept in memory. The exchange serves stale while it
    revalidates, and serves stale on error, as RFC 5861 names it. Only a
    request with no valid secret waits for a fetch. A provider that holds the
    value never asks, so ``push`` hands it a fresh value before the old one
    expires. Nothing is written back to the database.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._refreshable: dict[tuple[uuid.UUID, str], RefreshableSecret] = {}

    def keep(self, host_ids: set[uuid.UUID]) -> None:
        """Forget the secrets of every other host. Nothing is fetched for a dead box."""
        self._refreshable = {
            key: secret for key, secret in self._refreshable.items() if key[0] in host_ids
        }

    async def current(self, host_id: uuid.UUID, service: str, entry: dict[str, Any]) -> Secret:
        """Raises ``IssuerUnavailableError`` when no valid secret exists."""
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

    async def push(
        self,
        host_id: uuid.UUID,
        vm: str,
        service: str,
        entry: dict[str, Any],
        injection: SecretInjectionCapability,
    ) -> None:
        """Hand the provider that holds the value a fresh one before the old one expires.

        A push that fails waits like a fetch that fails, and the same value goes
        again after the wait. The first push happens at first sight of the entry,
        since the boot value came from the API process.
        """
        refreshable = self._refreshable.setdefault((host_id, service), RefreshableSecret())
        now = datetime.now(UTC)
        if now < refreshable.next_attempt or (
            refreshable.pushed and not refreshable.pushed.is_stale(now)
        ):
            return
        if not refreshable.unpushed(now):
            await refreshable.refresh(entry["issuer"], self._client)
        if secret := refreshable.unpushed(now):
            try:
                await injection.push_secret(vm=vm, name=service, value=secret.value)
            except ProviderError as exc:
                logger.warning("push of %s to %s failed: %s", service, vm, exc)
                refreshable.retry_later()
            else:
                logger.info("pushed %s to %s", service, vm)
                refreshable.pushed = secret
                refreshable.wait = FIRST_RETRY
