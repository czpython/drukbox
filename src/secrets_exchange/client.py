import uuid
from typing import Self

import httpx

from host_secrets.exceptions import SecretRefreshError
from secrets_exchange.settings import SecretsExchangeSettings


class SecretsExchange:
    diagnose_hint = "start_secrets_exchange_beside_the_api"

    def __init__(self, url: str) -> None:
        self.url = url

    @classmethod
    def from_settings(cls) -> Self:
        return cls(SecretsExchangeSettings().url)

    async def refresh(self, host_id: uuid.UUID, service: str) -> None:
        try:
            async with httpx.AsyncClient(base_url=self.url, timeout=30, trust_env=False) as client:
                response = await client.post(f"/refresh/{host_id}/{service}")
        except httpx.RequestError as exc:
            raise SecretRefreshError("secrets exchange unavailable") from exc
        if response.status_code == httpx.codes.SERVICE_UNAVAILABLE:
            raise SecretRefreshError("the issuer or provider did not supply a value")
        response.raise_for_status()

    async def diagnose(self) -> str:
        async with httpx.AsyncClient(base_url=self.url, trust_env=False) as client:
            (await client.get("/healthz")).raise_for_status()
        return "exchange healthy"
