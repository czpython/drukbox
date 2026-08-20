import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Self

import httpx

from .tailscale_settings import TailscaleSettings

logger = logging.getLogger(__name__)


class NetworkError(RuntimeError):
    """Base error for networking concerns."""


class NetworkAuthError(NetworkError):
    """Authentication/authorization failed against the network provider."""


class NetworkTransportError(NetworkError):
    """Transport or response error talking to the network provider."""


class DeviceDiscoveryTimeoutError(NetworkError):
    """Tailscale never reported the expected device within the timeout."""


_EMPTY_ENV: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True)
class JoinCredentials:
    env: Mapping[str, str] = field(default_factory=lambda: _EMPTY_ENV)
    device_ref: str | None = None


@dataclass(frozen=True)
class TailscaleAuthKey:
    id: str
    key: str
    description: str
    key_type: str


@dataclass(frozen=True)
class TailscaleDevice:
    id: str
    hostname: str


class TailscaleAPI:
    base_url = "https://api.tailscale.com"
    access_token_expiry_buffer_seconds = 60

    def __init__(
        self,
        *,
        oauth_client_id: str,
        oauth_client_secret: str,
        tailnet: str,
        auth_key_tags: tuple[str, ...],
        timeout: float,
        connect_timeout: float = 5.0,
    ) -> None:
        self.oauth_client_id = oauth_client_id
        self.oauth_client_secret = oauth_client_secret
        self.tailnet = tailnet
        self.auth_key_tags = auth_key_tags
        self.timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self._client: httpx.AsyncClient | None = None
        self._access_token = None
        self._access_token_expires_at = 0.0

    @classmethod
    def from_settings(cls) -> Self:
        # pydantic-settings resolves required fields from env at runtime;
        # pyright can't see that statically. If TAILSCALE_ENABLED=true but
        # any of the required envs are missing, this raises ValidationError
        # at construction with a clear field-by-field message.
        settings = TailscaleSettings()  # pyright: ignore[reportCallIssue]
        return cls(
            oauth_client_id=settings.oauth_client_id,
            oauth_client_secret=settings.oauth_client_secret,
            tailnet=settings.tailnet,
            auth_key_tags=settings.auth_tags,
            timeout=settings.api_timeout,
        )

    async def get_access_token(self) -> str:
        now = time.monotonic()

        if self._access_token and now < self._access_token_expires_at:
            return self._access_token

        payload = {
            "client_id": self.oauth_client_id,
            "client_secret": self.oauth_client_secret,
            "grant_type": "client_credentials",
        }

        response = await self._request(
            "POST",
            "/api/v2/oauth/token",
            data=payload,
            headers={"Accept": "application/json"},
        )
        expires_in = int(str(response["expires_in"]))
        self._access_token = str(response["access_token"])
        self._access_token_expires_at = now + expires_in - self.access_token_expiry_buffer_seconds
        return self._access_token

    async def create_auth_key(
        self,
        *,
        description: str,
        tags: tuple[str, ...] = (),
        expiry_seconds: int = 86400,
        preauthorized: bool = True,
        ephemeral: bool = False,
        reusable: bool = False,
    ) -> TailscaleAuthKey:
        access_token = await self.get_access_token()
        auth_key_tags = tags or self.auth_key_tags
        payload = {
            "keyType": "auth",
            "description": description,
            "expirySeconds": expiry_seconds,
            "capabilities": {
                "devices": {
                    "create": {
                        "reusable": reusable,
                        "ephemeral": ephemeral,
                        "preauthorized": preauthorized,
                        "tags": list(auth_key_tags),
                    }
                }
            },
        }
        response = await self._request(
            "POST",
            f"/api/v2/tailnet/{self.tailnet}/keys",
            json=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        return TailscaleAuthKey(
            id=str(response["id"]),
            key=str(response["key"]),
            description=str(response["description"]),
            key_type=str(response["keyType"]),
        )

    async def list_devices(self) -> list[TailscaleDevice]:
        access_token = await self.get_access_token()
        # fields=default keeps the response payload small — we only need
        # nodeId + hostname, and the full response includes route ads, key
        # expiry, etc. that we don't read here.
        response: Any = await self._request(
            "GET",
            f"/api/v2/tailnet/{self.tailnet}/devices",
            params={"fields": "default"},
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        return [
            TailscaleDevice(id=device["nodeId"], hostname=device["hostname"])
            for device in response["devices"]
        ]

    async def delete_device(self, device_id: str) -> None:
        access_token = await self.get_access_token()

        try:
            response = await self._get_client().delete(
                f"/api/v2/device/{device_id}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
        except httpx.RequestError as exc:
            raise NetworkTransportError(f"Tailscale API transport failed: {exc}") from exc

        if response.status_code == 401:
            raise NetworkAuthError("Tailscale API authentication failed")

        if response.status_code == 403:
            raise NetworkAuthError("Tailscale API request was rejected by access control")

        if response.status_code == 404:
            # Treat not-found as success: ephemeral devices auto-delete after
            # going offline, retries are legitimate, and operator cleanup via
            # the admin console can race us. The caller wants "this device is
            # gone" and 404 satisfies that.
            return

        if response.status_code not in {200, 204}:
            detail = response.text.strip()
            if not detail:
                detail = f"Tailscale API request failed with status {response.status_code}"
            raise NetworkTransportError(detail)

    async def aclose(self) -> None:
        if not self._client:
            return

        await self._client.aclose()
        self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        data: dict[str, str] | None = None,
        json: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, object]:
        try:
            response = await self._get_client().request(
                method,
                path,
                data=data,
                json=json,
                params=params,
                headers=headers,
            )
        except httpx.RequestError as exc:
            raise NetworkTransportError(f"Tailscale API transport failed: {exc}") from exc

        if response.status_code == 401:
            raise NetworkAuthError("Tailscale API authentication failed")

        if response.status_code == 403:
            raise NetworkAuthError("Tailscale API request was rejected by access control")

        try:
            response_data: dict[str, object] = response.json()
        except ValueError as exc:
            raise NetworkTransportError("Tailscale API returned non-JSON output") from exc

        if response.status_code >= 400:
            # Tailscale's REST API uses {"message": ...}; the OAuth token endpoint
            # uses {"error": ...}. Fall back to the raw body (always non-empty
            # here — we already parsed it as JSON) rather than KeyError out.
            detail = (
                response_data.get("message") or response_data.get("error") or response.text.strip()
            )
            raise NetworkTransportError(
                f"Tailscale API request failed with status {response.status_code}: {detail}"
            )
        return response_data

    def _get_client(self) -> httpx.AsyncClient:
        if not self._client:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self._client


class _DeviceWaiter:
    """One shared poller resolves many hostname waiters.

    Each provision registers ``(hostname → Future)`` and awaits it; a single
    background task polls ``list_devices()`` and resolves matching Futures.
    Collapses N concurrent provisions' independent pollers into one shared
    listing — under burst that's the difference between 1 API call per tick
    and N.
    """

    def __init__(self, api: TailscaleAPI, *, poll_interval: float = 2.0) -> None:
        self._api = api
        self._poll_interval = poll_interval
        self._waiters: dict[str, asyncio.Future[str]] = {}
        self._task: asyncio.Task[None] | None = None

    async def wait_for(self, host_name: str, timeout: float) -> str:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll())
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        # Hostnames are uuid7-derived and unique per provision; concurrent
        # waiters on the same hostname don't happen in practice.
        self._waiters[host_name] = fut
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError as exc:
            raise DeviceDiscoveryTimeoutError(
                f"Tailscale did not surface device for hostname {host_name!r} within {timeout}s"
            ) from exc
        finally:
            self._waiters.pop(host_name, None)

    async def _poll(self) -> None:
        # Exit when no one's waiting so the process isn't hammering the
        # Tailscale API forever after the last provision finishes; `wait_for`
        # spawns a fresh task next time someone shows up.
        while self._waiters:
            try:
                devices = await self._api.list_devices()
            except NetworkAuthError as exc:
                # Auth failures don't heal on retry; fail every waiter now so a
                # bad token surfaces as itself rather than a discovery timeout.
                self._fail_waiters(exc)
                return
            except Exception as exc:
                # Transient transport/listing errors: log and retry within the
                # caller's timeout budget.
                logger.warning("tailscale list_devices failed: %s", exc)
            else:
                # The set_result loop is sync — no awaits between checking
                # `not fut.done()` and resolving, so a concurrent timeout
                # can't race the resolution.
                for device in devices:
                    fut = self._waiters.get(device.hostname)
                    if fut is not None and not fut.done():
                        fut.set_result(device.id)
            await asyncio.sleep(self._poll_interval)

    def _fail_waiters(self, exc: Exception) -> None:
        for fut in self._waiters.values():
            if not fut.done():
                fut.set_exception(exc)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


class Tailscale:
    """High-level Tailscale operations consumed by HostService.

    Wraps :class:`TailscaleAPI` so callers think in terms of
    "issue join credentials" / "release device" rather than raw HTTP. There's
    only one network backend right now; if/when Modal-style direct SSH lands,
    re-introduce a NetworkProvider ABC.
    """

    diagnose_hint = "check_tailscale_oauth_and_api_reachability"

    def __init__(self, api: TailscaleAPI, *, poll_interval: float = 2.0) -> None:
        self.api = api
        self._waiter = _DeviceWaiter(api, poll_interval=poll_interval)

    @classmethod
    def from_settings(cls) -> Self:
        return cls(TailscaleAPI.from_settings())

    @property
    def tailnet(self) -> str:
        return self.api.tailnet

    async def issue_join_credentials(self, *, host_name: str) -> JoinCredentials:
        auth_key = await self.api.create_auth_key(
            description=f"drukbox {host_name}", ephemeral=True
        )
        return JoinCredentials(
            env=MappingProxyType(
                {
                    "TAILSCALE_AUTHKEY": auth_key.key,
                    "TAILSCALE_HOSTNAME": host_name,
                }
            )
        )

    async def release_device(self, device_ref: str) -> None:
        await self.api.delete_device(device_ref)

    async def wait_for_device(self, *, host_name: str, timeout: float) -> str:
        return await self._waiter.wait_for(host_name, timeout)

    def build_ssh_host(self, name: str) -> str:
        return f"{name}.{self.tailnet}"

    async def diagnose(self) -> str:
        # list_devices does the work: OAuth exchange, tailnet path, and a
        # signed API call all on the same hop. If it succeeds, we can mint
        # keys and discover devices.
        devices = await self.api.list_devices()
        return f"tailnet={self.tailnet} devices={len(devices)}"

    async def aclose(self) -> None:
        await self._waiter.aclose()
        await self.api.aclose()
