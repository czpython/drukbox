import contextlib
from typing import Any

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
            params={"teamId": settings.team_id},
            timeout=httpx.Timeout(settings.api_timeout, connect=5),
        )

    async def create_sandbox(self, name: str, *, image: str, vcpus: int, label: str) -> str:
        created = await self.request(
            "POST",
            "v3/sandboxes",
            json={
                "projectId": self.settings.project_id,
                "name": name,
                "image": image,
                "resources": {"vcpus": vcpus},
                "timeout": self.settings.max_lifetime_seconds * 1000,
                "persistent": False,
                "tags": {"managed-by": label},
            },
        )
        return created["session"]["id"]

    async def bootstrap(self, session_id: str, script: str) -> None:
        commands = f"v2/sandboxes/sessions/{session_id}/cmd"
        # Vercel's rejection can quote the command, and the command carries
        # the Tailscale auth key, so the reason stays out of the error.
        try:
            started = await self.request(
                "POST",
                commands,
                json={
                    "command": "bash",
                    "args": ["-e", "-c", script],
                    "sudo": True,
                    "timeout": 120_000,
                },
            )
        except ProviderCommandError:
            raise ProviderCommandError("Vercel rejected the bootstrap command") from None

        finished = await self.request(
            "GET", f"{commands}/{started['command']['id']}", params={"wait": "true"}
        )
        if finished["command"]["exitCode"]:
            raise ProviderCommandError("Vercel sandbox bootstrap failed")

    async def delete_sandbox(self, name: str) -> None:
        await self.request(
            "DELETE",
            f"v2/sandboxes/{name}",
            params={"projectId": self.settings.project_id, "deleteOrphanSnapshots": "true"},
        )

    async def diagnose(self) -> str:
        await self.request(
            "GET", "v2/sandboxes", params={"project": self.settings.project_id, "limit": "1"}
        )
        return "Vercel API authentication succeeded"

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
            raise ProviderTransportError("Vercel API transport failed") from exc
        if response.status_code in {401, 403}:
            raise ProviderAuthError("Vercel API authentication failed")
        if response.status_code == 404:
            raise ProviderNotFoundError("Vercel resource was not found")
        if response.status_code >= 500 or response.status_code == 429:
            raise ProviderTransportError(f"Vercel API returned HTTP {response.status_code}")
        if not 200 <= response.status_code < 300:
            reason = f"Vercel API rejected request (HTTP {response.status_code})"
            with contextlib.suppress(ValueError, KeyError, TypeError):
                reason = f"{reason}: {response.json()['error']['message']}"
            raise ProviderCommandError(reason)
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderTransportError("Invalid Vercel API response") from exc
