import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from core.database import close_database
from core.exceptions import AppException
from core.settings import get_settings
from diagnostics.api import router as diagnostics_router
from host_secrets.api import router as host_secrets_router
from hosts.api import router as hosts_router
from http_proxies.api import router as http_proxies_router
from networking.tailscale import Tailscale
from providers.registry import iter_initialized_vm_providers
from secret_proxy.tunnels import ReverseTunnelManager
from templates.api import router as templates_router

_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)

logging.basicConfig(
    level=_log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


class _HealthzAccessFilter(logging.Filter):
    """Drop /healthz access-log lines so the liveness probe (Docker
    HEALTHCHECK, k8s) doesn't flood the log with one entry per check."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "GET /healthz " not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_HealthzAccessFilter())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Process-wide Tailscale client when enabled: reusing one
    # httpx.AsyncClient amortises the TLS handshake and OAuth token cache
    # across every provision instead of paying both per request. When
    # Tailscale is disabled the slot stays None and request handlers skip
    # the network layer entirely.
    settings = get_settings()
    tailscale: Tailscale | None = Tailscale.from_settings() if settings.tailscale_enabled else None
    reverse_tunnels = ReverseTunnelManager()
    app.state.tailscale = tailscale
    app.state.reverse_tunnels = reverse_tunnels
    try:
        await reverse_tunnels.start()
        yield
    finally:
        await reverse_tunnels.aclose()
        if tailscale:
            await tailscale.aclose()
        for vm_provider in iter_initialized_vm_providers():
            await vm_provider.aclose()
        await close_database()


app = FastAPI(title="Drukbox", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = [
        {key: value for key, value in error.items() if key in {"type", "loc", "msg"}}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(AppException)
async def app_exception_handler(_request: Request, exc: AppException) -> JSONResponse:
    payload: dict[str, str] = {"detail": exc.detail}
    if exc.error_code:
        payload["error_code"] = exc.error_code
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(hosts_router)
app.include_router(host_secrets_router)
app.include_router(http_proxies_router)
app.include_router(templates_router)
app.include_router(diagnostics_router)
