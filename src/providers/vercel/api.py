import asyncio
from typing import Any
from urllib.parse import quote

import httpx

from providers.exceptions import (
    ProviderAuthError,
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
)

from .settings import VercelSettings


class VercelAPI:
    def __init__(self, settings: VercelSettings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url="https://vercel.com/api/",
            headers={"Authorization": f"Bearer {settings.token}"},
            timeout=httpx.Timeout(settings.api_timeout, connect=5),
        )

    async def create_sandbox(self, name: str, *, image: str, vcpus: int, label: str) -> str:
        data = await self.request(
            "POST",
            "v3/sandboxes",
            json={
                "name": name,
                "projectId": self.settings.project_id,
                "image": image,
                "resources": {"vcpus": vcpus},
                "timeout": self.settings.session_timeout_seconds * 1000,
                "persistent": False,
                "tags": {"managed-by": label},
            },
        )
        try:
            session = data["session"]
            session_id = session["id"]
            if (
                not isinstance(session_id, str)
                or not session_id
                or data["sandbox"]["name"] != name
                or data["sandbox"]["persistent"] is not False
                or session["timeout"] < self.settings.session_timeout_seconds * 1000
            ):
                raise ProviderTransportError("Unexpected Vercel sandbox configuration")
        except (KeyError, TypeError) as exc:
            raise ProviderTransportError("Invalid Vercel sandbox response") from exc
        return session_id

    async def bootstrap(self, session_id: str, script: str) -> None:
        path = f"v2/sandboxes/sessions/{quote(session_id, safe='')}/cmd"
        try:
            async with asyncio.timeout(130):
                data = await self.request(
                    "POST",
                    path,
                    json={
                        "command": "bash",
                        "args": ["-e", "-c", script],
                        "sudo": True,
                        "timeout": 120000,
                    },
                )
                command_id = data["command"]["id"]
                if not isinstance(command_id, str) or not command_id:
                    raise ProviderTransportError("Invalid Vercel command ID")
                result = await self.request(
                    "GET", f"{path}/{quote(command_id, safe='')}", params={"wait": "true"}
                )
                if result["command"]["exitCode"] != 0:
                    raise ProviderCommandError("Vercel sandbox bootstrap failed")
        except (KeyError, TypeError) as exc:
            raise ProviderTransportError("Invalid Vercel command response") from exc
        except TimeoutError as exc:
            raise ProviderTransportError("Vercel sandbox bootstrap timed out") from exc

    async def delete_sandbox(self, name: str) -> None:
        await self.request(
            "DELETE",
            f"v2/sandboxes/{quote(name, safe='')}",
            params={"projectId": self.settings.project_id, "deleteOrphanSnapshots": "true"},
        )

    async def diagnose(self) -> str:
        await self.request(
            "GET", "v2/sandboxes", params={"project": self.settings.project_id, "limit": "1"}
        )
        return "Vercel sandbox API authentication succeeded"

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
            response = await self.client.request(
                method, path, json=json, params={"teamId": self.settings.team_id, **(params or {})}
            )
        except httpx.RequestError as exc:
            raise ProviderTransportError("Vercel API transport failed") from exc
        if response.status_code in {401, 403}:
            raise ProviderAuthError("Vercel API authentication failed")
        if response.status_code == 404:
            raise ProviderNotFoundError("Vercel sandbox resource not found")
        if response.status_code >= 500 or response.status_code == 429:
            raise ProviderTransportError(f"Vercel API returned HTTP {response.status_code}")
        if not 200 <= response.status_code < 300:
            raise ProviderCommandError(f"Vercel API rejected request: HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderTransportError("Invalid Vercel API response") from exc
        if not isinstance(data, dict):
            raise ProviderTransportError("Invalid Vercel API response")
        return data
