import asyncio
import contextlib
import shlex
from typing import Any
from urllib.parse import quote

import httpx

from providers.exceptions import (
    ProviderAuthError,
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
)

from .settings import DaytonaSettings

LINUX_SANDBOX_CLASSES = {"linux-vm", "container"}
STARTING_STATES = {"creating", "starting", "pending_build", "pulling_snapshot"}


class DaytonaAPI:
    def __init__(self, settings: DaytonaSettings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url="https://app.daytona.io/api/",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=httpx.Timeout(settings.api_timeout, connect=5),
        )

    async def check_snapshot(self, image: str) -> None:
        snapshot = await self.request("GET", f"snapshots/{quote(image, safe='')}")

        if snapshot["sandboxClass"] not in LINUX_SANDBOX_CLASSES:
            raise ProviderCommandError("Daytona requires a Linux VM or container snapshot")
        if snapshot["state"] != "active":
            raise ProviderCommandError("Daytona snapshot is not active")

    async def create_sandbox(self, name: str, *, image: str, label: str) -> None:
        await self.request(
            "POST",
            "sandbox",
            json={
                "name": name,
                "snapshot": image,
                "target": self.settings.target,
                "user": "root",
                "labels": {"managed-by": label},
                "public": False,
                "autoStopInterval": 0,
                "autoPauseInterval": 0,
                "autoDeleteInterval": -1,
                "ttlMinutes": 0,
            },
        )

    async def wait_for_started(self, name: str) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self.settings.lifecycle_timeout_seconds):
                sandbox = await self.request("GET", f"sandbox/{name}")
                while sandbox["state"] in STARTING_STATES:
                    await asyncio.sleep(1)
                    sandbox = await self.request("GET", f"sandbox/{name}")
        except TimeoutError as exc:
            raise ProviderTransportError("Daytona sandbox start timed out") from exc

        if sandbox["state"] != "started":
            raise ProviderTransportError("Daytona sandbox did not start")
        return sandbox

    async def bootstrap(self, sandbox: dict[str, Any], script: str) -> None:
        # Daytona's rejection can quote the command, and the command carries
        # the Tailscale auth key, so the reason stays out of the error.
        try:
            result = await self.request(
                "POST",
                f"{sandbox['toolboxProxyUrl']}/{sandbox['id']}/process/execute",
                json={"command": f"sudo -n bash -e -c {shlex.quote(script)}", "timeout": 120},
            )
        except ProviderCommandError:
            raise ProviderCommandError("Daytona rejected the bootstrap command") from None

        if result["exitCode"]:
            raise ProviderCommandError("Daytona sandbox bootstrap failed")

    async def delete_sandbox(self, name: str, *, label: str) -> None:
        sandbox = await self.request("GET", f"sandbox/{name}")

        if sandbox["labels"].get("managed-by") != label:
            raise ProviderCommandError("Daytona sandbox does not belong to this deployment")
        if sandbox["state"] == "destroyed":
            return
        path = f"sandbox/{sandbox['id']}"
        await self.request("DELETE", path)
        try:
            async with asyncio.timeout(self.settings.lifecycle_timeout_seconds):
                while True:
                    try:
                        sandbox = await self.request("GET", path)
                    except ProviderNotFoundError:
                        return
                    if sandbox["state"] == "destroyed":
                        return
                    if sandbox["state"] == "error":
                        raise ProviderTransportError("Daytona sandbox deletion failed")
                    await asyncio.sleep(1)
        except TimeoutError as exc:
            raise ProviderTransportError("Daytona sandbox deletion timed out") from exc

    async def diagnose(self) -> str:
        await self.check_snapshot(self.settings.default_image)
        return "Daytona API authentication succeeded; snapshot is active"

    async def aclose(self) -> None:
        await self.client.aclose()

    async def request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, json=json)
        except httpx.RequestError as exc:
            raise ProviderTransportError("Daytona API transport failed") from exc
        if response.status_code in {401, 403}:
            raise ProviderAuthError("Daytona API authentication failed")
        if response.status_code == 404:
            raise ProviderNotFoundError("Daytona resource was not found")
        if response.status_code >= 500 or response.status_code == 429:
            raise ProviderTransportError(f"Daytona API returned HTTP {response.status_code}")
        if not 200 <= response.status_code < 300:
            reason = f"Daytona API rejected request (HTTP {response.status_code})"
            with contextlib.suppress(ValueError, KeyError, TypeError):
                reason = f"{reason}: {response.json()['message']}"
            raise ProviderCommandError(reason)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderTransportError("Invalid Daytona API response") from exc
