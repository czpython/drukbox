import asyncio
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx

from .exceptions import (
    BoatAuthError,
    BoatCommandError,
    BoatNotFoundError,
    BoatTransportError,
)
from .settings import BoatSettings


class BoatAPI:
    def __init__(self, settings: BoatSettings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.api_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.api_token}"},
            timeout=httpx.Timeout(settings.api_timeout, connect=5),
        )

    async def create_sandbox(self, name: str, *, image: str, instance_type: str) -> str:
        body: dict[str, Any] = {
            "type": instance_type,
            "ttlSeconds": None,
            "noEnv": True,
            "snapshots": False,
        }
        if image != "default":
            body["from"] = image
        for attempt in range(3):
            try:
                payload = await self.request(
                    "POST",
                    "sandboxes",
                    json=body,
                    headers={"Idempotency-Key": str(uuid5(NAMESPACE_URL, name))},
                )
            except BoatTransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep(1)
            else:
                return payload["sandbox"]["id"]
        raise AssertionError("creation attempts exhausted")

    async def name_sandbox(self, sandbox_id: str, name: str) -> None:
        await self.request("PATCH", f"sandboxes/{sandbox_id}", json={"name": name})

    async def wait_ready(self, sandbox_id: str) -> None:
        try:
            async with asyncio.timeout(self.settings.provision_timeout):
                while True:
                    sandbox = (await self.request("GET", f"sandboxes/{sandbox_id}"))["sandbox"]
                    if sandbox["state"] in {"ready", "idle", "running"}:
                        if sandbox["archiveAfter"]:
                            raise BoatCommandError("Boat did not disable automatic archival")
                        return
                    if sandbox["state"] in {"error", "cancelled", "archived", "archiving"}:
                        raise BoatCommandError(f"Boat sandbox entered state {sandbox['state']}")
                    await asyncio.sleep(1)
        except TimeoutError as exc:
            raise BoatTransportError("Boat sandbox did not become ready") from exc

    async def configure_ssh(self, sandbox_id: str, public_key: str) -> dict[str, Any]:
        return await self.request(
            "POST", f"sandboxes/{sandbox_id}/sshkey", json={"key": public_key}
        )

    async def run_command(self, sandbox_id: str, command: str) -> None:
        result = await self.request(
            "POST",
            f"sandboxes/{sandbox_id}/commands",
            json={"command": command, "timeoutSeconds": 120},
            timeout=130,
        )
        if result["timedOut"] or result["exitCode"] != 0 or not result["success"]:
            raise BoatCommandError("Boat sandbox bootstrap failed")

    async def find_sandbox(self, name: str) -> str:
        params = {"limit": "100"}
        while True:
            payload = await self.request("GET", "sandboxes", params=params)
            for sandbox in payload["sandboxes"]:
                if sandbox["name"] == name:
                    return sandbox["id"]
            page = payload.get("pageInfo")
            if not page or not page["hasMore"]:
                raise BoatNotFoundError(f"Boat sandbox {name!r} was not found")
            params["cursor"] = page["nextCursor"]

    async def delete_sandbox(self, sandbox_id: str) -> None:
        # Boat owns the accepted purge even after it hides the sandbox from lookups.
        await self.request(
            "DELETE",
            f"sandboxes/{sandbox_id}",
            headers={"X-Ascii-Confirm-Delete": sandbox_id},
        )

    async def diagnose(self) -> str:
        await self.request("GET", "sandboxes", params={"limit": "1"})
        return "Boat API authentication and sandbox access succeeded"

    async def aclose(self) -> None:
        await self.client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self.client.request(
                method,
                path,
                json=json,
                params=params,
                headers=headers,
                timeout=timeout or self.settings.api_timeout,
            )
        except httpx.RequestError as exc:
            raise BoatTransportError("Boat API transport failed") from exc
        if response.status_code in {401, 403}:
            raise BoatAuthError("Boat API authentication or authorization failed")
        if response.status_code == 404:
            raise BoatNotFoundError("Boat resource was not found")
        if response.status_code >= 500 or response.status_code == 429:
            raise BoatTransportError(f"Boat API returned HTTP {response.status_code}")
        if response.status_code >= 400:
            raise BoatCommandError(f"Boat API rejected the request: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BoatTransportError("Boat API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise BoatTransportError("Boat API returned a non-object response")
        return payload
