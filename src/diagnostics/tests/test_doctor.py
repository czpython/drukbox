import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from networking.tailscale import Tailscale
from providers.exe.provider import ExeProvider
from providers.registry import reset_vm_provider_cache


@pytest.fixture(autouse=True)
def _reset_doctor_state() -> None:
    # Tests install class-method patches and instantiate providers lazily;
    # clear cached singletons + the lifespan-installed tailscale slot so a
    # previous test's bindings don't leak in. The FastAPI app is module-scoped
    # so app.state survives between tests.
    from api.app import app

    reset_vm_provider_cache()
    with contextlib.suppress(KeyError):
        del app.state.tailscale


async def test_doctor_requires_service_auth(client) -> None:
    """No bearer token — endpoint returns 401."""
    response = await client.get("/doctor")
    assert response.status_code == 401


async def test_doctor_rejects_unknown_token(client) -> None:
    """An unrecognised token returns 403, not 401."""
    response = await client.get(
        "/doctor",
        headers={"Authorization": "Bearer not-our-token"},
    )
    assert response.status_code == 403


async def test_doctor_reports_ok_when_all_probes_pass(client) -> None:
    """Happy path: flat names, ok=True, no hint on the checks."""
    with (
        patch.object(ExeProvider, "diagnose", new=AsyncMock(return_value="exe ok")),
        patch.object(Tailscale, "diagnose", new=AsyncMock(return_value="tailnet ok")),
    ):
        response = await client.get(
            "/doctor",
            headers={"Authorization": "Bearer service-token"},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["active_provider"] == "exe"
    assert body["tailscale_enabled"] is True
    assert [check["name"] for check in body["checks"]] == ["db", "provider", "tailscale"]
    assert all(check["hint"] is None for check in body["checks"])
    provider = next(check for check in body["checks"] if check["name"] == "provider")
    assert provider["detail"] == "exe ok"


async def test_doctor_propagates_failure_with_owner_hint(client) -> None:
    """A failed probe surfaces with the owner-specific hint."""
    with (
        patch.object(
            Tailscale,
            "diagnose",
            new=AsyncMock(side_effect=RuntimeError("401 Unauthorized")),
        ),
        patch.object(ExeProvider, "diagnose", new=AsyncMock(return_value="exe ok")),
    ):
        response = await client.get(
            "/doctor",
            headers={"Authorization": "Bearer service-token"},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is False
    tailscale = next(check for check in body["checks"] if check["name"] == "tailscale")
    assert tailscale["status"] == "fail"
    assert tailscale["hint"] == "check_tailscale_oauth_and_api_reachability"


async def test_doctor_omits_tailscale_when_disabled(client, monkeypatch) -> None:
    """With TAILSCALE_ENABLED=false there is no tailscale row at all."""
    from core import settings as settings_module

    monkeypatch.setenv("TAILSCALE_ENABLED", "false")
    settings_module.get_settings.cache_clear()

    with patch.object(ExeProvider, "diagnose", new=AsyncMock(return_value="exe ok")):
        response = await client.get(
            "/doctor",
            headers={"Authorization": "Bearer service-token"},
        )

    body = response.json()
    assert body["tailscale_enabled"] is False
    assert [check["name"] for check in body["checks"]] == ["db", "provider"]
    settings_module.get_settings.cache_clear()


async def test_doctor_reports_provider_construction_failure(client) -> None:
    """If the provider can't even be constructed, /doctor stays 200 with a failed
    provider check and a generic hint — not an unstructured 500."""
    with (
        patch(
            "diagnostics.api.get_default_vm_provider",
            side_effect=RuntimeError("EXE_API_TOKEN missing"),
        ),
        patch.object(Tailscale, "diagnose", new=AsyncMock(return_value="tailnet ok")),
    ):
        response = await client.get(
            "/doctor",
            headers={"Authorization": "Bearer service-token"},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is False
    assert body["active_provider"] == "exe"
    provider = next(check for check in body["checks"] if check["name"] == "provider")
    assert provider["status"] == "fail"
    assert provider["hint"] == "check_provider_configuration"
    assert "EXE_API_TOKEN missing" in provider["detail"]


async def test_doctor_reports_tailscale_construction_failure(client) -> None:
    """A Tailscale construction failure (bad config) stays 200 with a failed
    tailscale check and its hint, not a 500."""
    with (
        patch(
            "diagnostics.api.Tailscale.from_settings",
            side_effect=RuntimeError("bad tailscale config"),
        ),
        patch.object(ExeProvider, "diagnose", new=AsyncMock(return_value="exe ok")),
    ):
        response = await client.get(
            "/doctor",
            headers={"Authorization": "Bearer service-token"},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is False
    tailscale = next(check for check in body["checks"] if check["name"] == "tailscale")
    assert tailscale["status"] == "fail"
    assert tailscale["hint"] == "check_tailscale_oauth_and_api_reachability"
    assert "bad tailscale config" in tailscale["detail"]


async def test_doctor_runs_db_probe_against_real_session(client) -> None:
    """The db row round-trips through the test DB, not a mock."""
    fake_provider = MagicMock()
    fake_provider.name = "exe"
    fake_provider.diagnose_hint = "check_exe_api_token"
    fake_provider.diagnose_timeout_seconds = 5.0
    fake_provider.diagnose = AsyncMock(return_value="exe ok")

    with (
        patch("diagnostics.api.get_default_vm_provider", return_value=fake_provider),
        patch.object(Tailscale, "diagnose", new=AsyncMock(return_value="tailnet ok")),
    ):
        response = await client.get(
            "/doctor",
            headers={"Authorization": "Bearer service-token"},
        )

    body = response.json()
    db = next(check for check in body["checks"] if check["name"] == "db")
    assert db["status"] == "ok"
    assert db["detail"] == "select 1 -> 1"


async def test_doctor_provider_probe_uses_the_provider_probe_timeout(client) -> None:
    """A probe slower than the default budget passes when the provider declares a larger one."""

    async def slow_diagnose(self) -> str:
        await asyncio.sleep(0.05)
        return "slow but healthy"

    with (
        patch.object(ExeProvider, "diagnose_timeout_seconds", 30.0),
        patch.object(ExeProvider, "diagnose", new=slow_diagnose),
        patch.object(Tailscale, "diagnose", new=AsyncMock(return_value="tailnet ok")),
        patch("diagnostics.api.DEFAULT_CHECK_TIMEOUT_SECONDS", 0.01),
        patch("diagnostics.checks.DEFAULT_CHECK_TIMEOUT_SECONDS", 0.01),
    ):
        response = await client.get(
            "/doctor",
            headers={"Authorization": "Bearer service-token"},
        )

    provider = next(check for check in response.json()["checks"] if check["name"] == "provider")
    assert provider["status"] == "ok"
    assert provider["detail"] == "slow but healthy"
