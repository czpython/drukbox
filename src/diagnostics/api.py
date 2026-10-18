import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.settings import get_settings
from diagnostics.checks import DEFAULT_CHECK_TIMEOUT_SECONDS, Check, CheckStatus, run_check
from hosts.auth import require_auth
from networking.tailscale import NetworkError, Tailscale
from providers.registry import get_default_vm_provider
from secrets_exchange.client import SecretsExchange

router = APIRouter(prefix="/doctor", tags=["doctor"], dependencies=[Depends(require_auth)])


class CheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    status: CheckStatus
    detail: str | None
    latency_ms: int | None
    hint: str | None = None


class DoctorOut(BaseModel):
    ok: bool
    active_provider: str
    tailnet: bool
    checks: list[CheckOut]


@router.get("", response_model=DoctorOut)
async def doctor(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DoctorOut:
    settings = get_settings()
    try:
        provider = get_default_vm_provider()
        provider_hint = provider.diagnose_hint
        provider_timeout = provider.diagnose_timeout_seconds
        tailnet = provider.tailnet
    except Exception:
        # The provider can't even be constructed (bad config / missing dep). Use a
        # generic hint; the probe below re-resolves it so the construction error
        # surfaces as a failed check instead of a 500 — diagnosing exactly this is
        # the point of /doctor.
        provider_hint = "check_provider_configuration"
        provider_timeout = DEFAULT_CHECK_TIMEOUT_SECONDS
        tailnet = False

    async def _provider_probe() -> str:
        return await get_default_vm_provider().diagnose()

    async def _tailscale_probe() -> str:
        # Resolve lazily inside the probe (reusing the lifespan-installed client
        # when present) so a Tailscale construction failure becomes a failed
        # check rather than a 500 — same reasoning as the provider probe above.
        tailscale = getattr(request.app.state, "tailscale", None) or Tailscale.from_settings()
        if not tailscale:
            raise NetworkError(
                f"provider {settings.default_host_provider!r} joins the tailnet; set "
                "TAILSCALE_TAILNET, TAILSCALE_AUTH_TAGS, TAILSCALE_OAUTH_CLIENT_ID and "
                "TAILSCALE_OAUTH_CLIENT_SECRET"
            )
        return await tailscale.diagnose()

    tasks: list[asyncio.Future[Check]] = [
        asyncio.ensure_future(
            run_check("db", lambda: _ping_db(session), hint="check_database_url_and_engine"),
        ),
        asyncio.ensure_future(
            run_check("provider", _provider_probe, hint=provider_hint, timeout=provider_timeout),
        ),
        asyncio.ensure_future(
            run_check(
                "exchange",
                SecretsExchange.from_settings().diagnose,
                hint=SecretsExchange.diagnose_hint,
            ),
        ),
    ]
    if tailnet:
        tasks.append(
            asyncio.ensure_future(
                run_check("tailscale", _tailscale_probe, hint=Tailscale.diagnose_hint),
            )
        )
    checks = await asyncio.gather(*tasks)
    ok = all(check.status == "ok" for check in checks)
    return DoctorOut(
        ok=ok,
        active_provider=settings.default_host_provider,
        tailnet=tailnet,
        checks=[CheckOut.model_validate(check) for check in checks],
    )


async def _ping_db(session: AsyncSession) -> str:
    result = await session.execute(text("SELECT 1"))
    value = result.scalar()
    return f"select 1 -> {value}"
