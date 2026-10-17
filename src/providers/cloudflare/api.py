import httpx

from providers.exceptions import (
    ProviderAuthError,
    ProviderCommandError,
    ProviderTransportError,
)

from .settings import CloudflareSettings


class CloudflareAPI:
    def __init__(self, settings: CloudflareSettings) -> None:
        self.client = httpx.AsyncClient(
            base_url=settings.worker_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.worker_token}"},
            timeout=httpx.Timeout(settings.api_timeout, connect=5),
        )

    async def create_sandbox(
        self, name: str, *, image: str, instance: str, script: str, label: str
    ) -> None:
        await self.request(
            "PUT",
            f"sandboxes/{name}",
            status="active",
            json={
                "image": image,
                "instance": instance,
                "script": script,
                "label": label,
            },
        )

    async def delete_sandbox(self, name: str) -> None:
        await self.request("DELETE", f"sandboxes/{name}", status="deleted")

    async def diagnose(self) -> str:
        await self.request("GET", "health", status="ok")
        return "Cloudflare Worker authentication succeeded"

    async def aclose(self) -> None:
        await self.client.aclose()

    async def request(
        self, method: str, path: str, *, status: str, json: dict[str, str] | None = None
    ) -> None:
        try:
            response = await self.client.request(method, path, json=json)
        except httpx.RequestError as exc:
            raise ProviderTransportError("Cloudflare Worker transport failed") from exc
        if response.status_code in {401, 403}:
            raise ProviderAuthError("Cloudflare Worker authentication failed")
        if response.status_code >= 500 or response.status_code == 429:
            raise ProviderTransportError(f"Cloudflare Worker returned HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ProviderCommandError(
                f"Cloudflare Worker rejected request: HTTP {response.status_code}"
            )
        try:
            if response.status_code not in {200, 201} or response.json()["status"] != status:
                raise ProviderTransportError("Unexpected Cloudflare Worker response")
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderTransportError("Invalid Cloudflare Worker response") from exc
