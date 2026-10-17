import asyncio
import shlex
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from providers.exceptions import (
    ProviderAuthError,
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
)

from .settings import DaytonaSettings


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

        if snapshot.get("sandboxClass") != "linux-vm":
            raise ProviderCommandError("Daytona requires a Linux VM snapshot")
        if snapshot.get("state") != "active":
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

    async def get_sandbox(self, name: str, *, label: str) -> dict[str, Any]:
        sandbox = await self.request("GET", f"sandbox/{quote(name, safe='')}")
        labels = sandbox.get("labels")

        if (
            sandbox.get("name") != name
            or not isinstance(labels, dict)
            or labels.get("managed-by") != label
        ):
            raise ProviderCommandError("Daytona sandbox does not belong to this deployment")
        if not isinstance(sandbox.get("id"), str) or not sandbox["id"]:
            raise ProviderTransportError("Invalid Daytona sandbox ID")
        return sandbox

    async def wait_for_started(self, name: str, *, label: str) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self.settings.lifecycle_timeout_seconds):
                while True:
                    sandbox = await self.get_sandbox(name, label=label)

                    if sandbox.get("state") == "started":
                        if (
                            sandbox.get("sandboxClass") != "linux-vm"
                            or sandbox.get("autoStopInterval") != 0
                            or sandbox.get("autoPauseInterval") != 0
                            or sandbox.get("autoDeleteInterval") != -1
                            or sandbox.get("autoDestroyAt")
                        ):
                            raise ProviderCommandError(
                                "Daytona requires a Linux VM with automatic lifecycle disabled"
                            )
                        return sandbox
                    if sandbox.get("state") not in {
                        "creating",
                        "starting",
                        "pending_build",
                        "pulling_snapshot",
                    }:
                        raise ProviderTransportError("Daytona sandbox did not start")
                    await asyncio.sleep(1)
        except TimeoutError as exc:
            raise ProviderTransportError("Daytona sandbox start timed out") from exc

    async def bootstrap(self, sandbox: dict[str, Any], script: str) -> None:
        proxy = sandbox.get("toolboxProxyUrl")

        if not isinstance(proxy, str):
            raise ProviderTransportError("Missing Daytona toolbox URL")
        try:
            url = urlsplit(proxy)
            port = url.port
        except ValueError:
            raise ProviderTransportError("Invalid Daytona toolbox URL") from None
        if (
            url.scheme != "https"
            or not url.hostname
            or not url.hostname.endswith(".daytona.io")
            or url.username
            or url.password
            or url.query
            or url.fragment
            or port not in {None, 443}
        ):
            raise ProviderTransportError("Invalid Daytona toolbox URL")
        result = await self.request(
            "POST",
            f"{proxy.rstrip('/')}/{quote(sandbox['id'], safe='')}/process/execute",
            json={"command": f"sudo -n bash -e -c {shlex.quote(script)}", "timeout": 120},
        )

        if result.get("exitCode") != 0:
            raise ProviderCommandError("Daytona sandbox bootstrap failed")

    async def delete_sandbox(self, name: str, *, label: str) -> None:
        sandbox = await self.get_sandbox(name, label=label)
        path = f"sandbox/{quote(sandbox['id'], safe='')}"

        if sandbox.get("state") == "destroyed":
            return
        await self.request("DELETE", path)
        try:
            async with asyncio.timeout(self.settings.lifecycle_timeout_seconds):
                while True:
                    try:
                        sandbox = await self.request("GET", path)
                    except ProviderNotFoundError:
                        return
                    if sandbox.get("state") == "destroyed":
                        return
                    if sandbox.get("state") == "error":
                        raise ProviderTransportError("Daytona sandbox deletion failed")
                    await asyncio.sleep(1)
        except TimeoutError as exc:
            raise ProviderTransportError("Daytona sandbox deletion timed out") from exc

    async def diagnose(self) -> str:
        await self.request("GET", "sandbox", params={"limit": "1"})
        await self.check_snapshot(self.settings.default_image)
        return "Daytona API authentication succeeded; Linux VM snapshot is active"

    async def aclose(self) -> None:
        await self.client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, json=json, params=params)
        except httpx.RequestError as exc:
            raise ProviderTransportError("Daytona API transport failed") from exc
        if response.status_code in {401, 403}:
            raise ProviderAuthError("Daytona API authentication failed")
        if response.status_code == 404:
            raise ProviderNotFoundError("Daytona resource was not found")
        if response.status_code >= 500 or response.status_code == 429:
            raise ProviderTransportError(f"Daytona API returned HTTP {response.status_code}")
        if not 200 <= response.status_code < 300:
            raise ProviderCommandError(f"Daytona API rejected request: HTTP {response.status_code}")
        if response.status_code == 204:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderTransportError("Invalid Daytona API response") from exc
        if not isinstance(data, dict):
            raise ProviderTransportError("Invalid Daytona API response")
        return data
