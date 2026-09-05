import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from sqlalchemy import func, select
from uuid6 import uuid7

from core.database import async_session_factory
from core.settings import get_settings
from hosts.models import Host, HostStatus
from hosts.service import HostService, utc_now
from networking.tailscale import NetworkTransportError
from networking.tailscale_settings import TailscaleSettings
from providers.exceptions import ProviderTransportError
from providers.exe.settings import ExeSettings


async def test_create_host_honors_requested_provider(client, monkeypatch, stub_provider):
    """A `provider` in the body provisions on that provider, not the default."""
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
        json={"provider": "stub"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["provider"] == "stub"
    assert payload["image"] == "stub:image"


async def test_create_host_rejects_unknown_provider(client):
    """An unregistered provider name returns 400 listing the available ones."""
    response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
        json={"provider": "does-not-exist"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "does-not-exist" in detail
    assert "available" in detail


async def test_delete_host_uses_the_hosts_own_provider(client, stub_provider):
    """Teardown routes to the host's stored provider, not a service default."""
    now = utc_now()
    host = Host(
        id=uuid7(),
        name="sb-stub-1",
        status=HostStatus.ACTIVE.value,
        provider="stub",
        image="stub:image",
        env={},
        external_ssh_host="203.0.113.1",
        external_ssh_port=22,
        known_hosts="",
        created_at=now,
        updated_at=now,
        last_error="",
    )
    async with async_session_factory() as session:
        session.add(host)
        await session.commit()

    response = await client.delete(
        f"/hosts/{host.id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 204
    assert stub_provider.deleted == ["sb-stub-1"]


async def test_create_host_creates_record_and_enqueues_provisioning(client, monkeypatch):
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert uuid.UUID(payload["id"]).version == 7
    assert payload["name"].startswith("sb-")
    assert payload["status"] == "provisioning"  # provision mock is a no-op
    assert payload["provider"] == "exe"
    assert payload["image"] == ExeSettings().default_image  # pyright: ignore[reportCallIssue]
    # internal_ssh_host is populated by provision() (mocked out here), so
    # the response shows the row's pre-provision state.
    assert payload["internal_ssh_host"] is None
    assert payload["tailscale_device_id"] is None
    assert "env" not in payload

    async with async_session_factory() as session:
        host = await session.get(Host, uuid.UUID(payload["id"]))
        assert host is not None
        assert host.image == ExeSettings().default_image  # pyright: ignore[reportCallIssue]
        assert host.env == {}

    mocked_provision.assert_awaited_once()
    assert mocked_provision.await_args is not None
    assert mocked_provision.await_args.args == (str(host.id),)


async def test_create_host_persists_env_without_returning_it(client, monkeypatch):
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)
    env = {
        "SANDBOX_GATEWAY_URL": "wss://gateway.example.ts.net/daemon",
        "SANDBOX_GATEWAY_DAEMON_TOKEN": "daemon-token",
        "SANDBOX_PROJECT_TEMPLATE_URL": "https://github.com/example/template.git",
        "SANDBOX_STORAGE_URL": "",
        "TAILSCALE_ADVERTISE_TAGS": "tag:sandbox",
    }

    response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
        json={"env": env},
    )

    assert response.status_code == 201
    payload = response.json()
    assert "env" not in payload

    async with async_session_factory() as session:
        host = await session.get(Host, uuid.UUID(payload["id"]))
        assert host is not None
        assert host.env == env

    mocked_provision.assert_awaited_once()
    assert mocked_provision.await_args is not None
    assert mocked_provision.await_args.args == (payload["id"],)


async def test_create_host_stores_and_returns_custom_image(client, monkeypatch):
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)
    image = "ghcr.io/drukbox/custom-sandbox:api-test"

    response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
        json={"image": image, "env": {}},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["image"] == image

    async with async_session_factory() as session:
        host = await session.get(Host, uuid.UUID(payload["id"]))
        assert host is not None
        assert host.image == image


async def test_create_host_stores_and_returns_expires_at(client, monkeypatch):
    from datetime import UTC, datetime, timedelta

    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)
    expires_at = datetime.now(UTC) + timedelta(hours=1)

    response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
        json={"env": {}, "expires_at": expires_at.isoformat()},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["expires_at"] is not None

    async with async_session_factory() as session:
        host = await session.get(Host, uuid.UUID(payload["id"]))
        assert host is not None
        assert host.expires_at is not None


async def test_create_host_rejects_naive_expires_at(client):
    # ISO 8601 without a tz offset is a naive datetime; the validator must reject.
    response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
        json={"env": {}, "expires_at": "2099-01-01T12:00:00"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "expires_at"]


async def test_create_host_rejects_past_expires_at(client):
    response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
        json={"env": {}, "expires_at": "2020-01-01T12:00:00+00:00"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "expires_at"]
    assert "in the future" in detail[0]["msg"]


async def test_create_host_defaults_expires_at_to_lease_ttl(client, monkeypatch):
    # Safe-by-default lifecycle: omitting expires_at yields a renewable lease,
    # not a permanent host, so an abandoned host self-reaps.
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())
    ttl = timedelta(seconds=get_settings().lease_default_ttl)

    before = utc_now()
    response = await client.post("/hosts", headers={"Authorization": "Bearer service-token"})
    after = utc_now()

    assert response.status_code == 201
    expires_at = datetime.fromisoformat(response.json()["expires_at"])
    assert before + ttl <= expires_at <= after + ttl


async def test_create_host_keeps_safety_ttl_while_provisioning(monkeypatch):
    # The in-flight row carries the short provisioning-grace TTL, not the full
    # default lease, so an abandoned provision reaps after the grace window;
    # the lease itself is stamped once the host is usable.
    inflight = {}

    async def capture_inflight_expiry(self, host_id):
        async with async_session_factory() as session:
            row = await session.get(Host, uuid.UUID(host_id))
            assert row is not None
            inflight["expires_at"] = row.expires_at

    monkeypatch.setattr("hosts.service.HostService.provision", capture_inflight_expiry)
    grace = timedelta(seconds=get_settings().provisioning_grace_seconds)
    ttl = timedelta(seconds=get_settings().lease_default_ttl)

    async with async_session_factory() as session:
        before = utc_now()
        host = await HostService(session).create_host(env={}, image=None)
        after = utc_now()

    assert before + grace <= inflight["expires_at"] <= after + grace
    assert host.expires_at is not None
    assert before + ttl <= host.expires_at <= after + ttl


async def test_create_host_preserves_renewal_made_during_provisioning(monkeypatch):
    # A client whose POST timed out can find the bootstrapping row and renew
    # it; the create's post-provision TTL rewrite must not clobber that newer
    # lease when provisioning finishes.
    renewal = {}

    async def renew_mid_provision(self, host_id):
        async with async_session_factory() as session:
            row = await session.get(Host, uuid.UUID(host_id))
            assert row is not None
            row.status = HostStatus.BOOTSTRAPPING.value
            await session.commit()
        async with async_session_factory() as session:
            renewed = await HostService(session).renew_host(
                uuid.UUID(host_id), expires_at=utc_now() + timedelta(days=7)
            )
            renewal["expires_at"] = renewed.expires_at

    monkeypatch.setattr("hosts.service.HostService.provision", renew_mid_provision)

    async with async_session_factory() as session:
        host = await HostService(session).create_host(env={}, image=None)

    assert host.expires_at == renewal["expires_at"]


async def test_create_host_explicit_null_expires_at_creates_permanent_host(client, monkeypatch):
    # expires_at: null is the caller's deliberate opt-in to a host with no
    # expiry — permanence must be a choice, never an accident.
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
        json={"expires_at": None},
    )

    assert response.status_code == 201
    assert response.json()["expires_at"] is None

    async with async_session_factory() as session:
        host = await session.get(Host, uuid.UUID(response.json()["id"]))
        assert host is not None
        assert host.expires_at is None


async def test_create_host_rejects_oversized_idempotency_key(client):
    response = await client.post(
        "/hosts",
        headers={
            "Authorization": "Bearer service-token",
            "Idempotency-Key": "x" * 256,
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["header", "Idempotency-Key"]


async def test_create_host_rejects_invalid_charset_idempotency_key(client):
    response = await client.post(
        "/hosts",
        headers={
            "Authorization": "Bearer service-token",
            "Idempotency-Key": "has space",
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["header", "Idempotency-Key"]


async def test_get_host_exposes_null_expires_at_by_default(client):
    host = await create_host_record(name="lb-sandbox-test", status="active")

    response = await client.get(
        f"/hosts/{host.id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 200
    assert response.json()["expires_at"] is None


async def test_get_host_never_returns_private_key(client):
    # Regression guard: the per-VM private key is returned exactly once at
    # POST time. GET must always return None — there's no column behind it,
    # and any future change that accidentally persists private_key would
    # break this assertion.
    host = await create_host_record(name="lb-sandbox-test", status="active")

    response = await client.get(
        f"/hosts/{host.id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 200
    assert response.json()["private_key"] is None


async def test_create_host_idempotency_key_returns_same_host_on_retry(client, monkeypatch):
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    first = await client.post(
        "/hosts",
        headers={
            "Authorization": "Bearer service-token",
            "Idempotency-Key": "drukbox-run-42",
        },
    )
    assert first.status_code == 201
    first_id = first.json()["id"]

    second = await client.post(
        "/hosts",
        headers={
            "Authorization": "Bearer service-token",
            "Idempotency-Key": "drukbox-run-42",
        },
    )
    assert second.status_code == 201
    assert second.json()["id"] == first_id

    # Only one host actually got created (kiq fires once per real create).
    assert mocked_provision.await_count == 1


async def test_create_host_idempotency_key_different_keys_create_different_hosts(
    client, monkeypatch
):
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    first = await client.post(
        "/hosts",
        headers={
            "Authorization": "Bearer service-token",
            "Idempotency-Key": "drukbox-run-A",
        },
    )
    second = await client.post(
        "/hosts",
        headers={
            "Authorization": "Bearer service-token",
            "Idempotency-Key": "drukbox-run-B",
        },
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]
    assert mocked_provision.await_count == 2


async def test_create_host_without_idempotency_key_creates_fresh_each_time(client, monkeypatch):
    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    first = await client.post("/hosts", headers={"Authorization": "Bearer service-token"})
    second = await client.post("/hosts", headers={"Authorization": "Bearer service-token"})

    assert first.json()["id"] != second.json()["id"]
    assert mocked_provision.await_count == 2


async def test_idempotency_loser_in_provisioning_is_marked_for_janitor(monkeypatch):
    # Race-loser that's fresh-created (in PROVISIONING) cannot be delete_host'd
    # (it's in DELETE_BLOCKED_STATUSES). The cleanup path must mark it with
    # expires_at=now so the janitor reaps it on the next cycle.
    from datetime import UTC, datetime

    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    async with async_session_factory() as session:
        service = HostService(session)
        loser = await service.create_host(env={}, image=None, expires_at=None)
        assert loser.status == HostStatus.PROVISIONING.value
        assert loser.expires_at is None
        await service._release_idempotency_loser(loser)

    async with async_session_factory() as session:
        reloaded = await session.get(Host, loser.id)
        assert reloaded is not None
        assert reloaded.expires_at is not None
        assert reloaded.expires_at <= datetime.now(UTC)


async def test_idempotency_loser_that_claimed_pool_host_is_returned_to_pool(monkeypatch):
    # Race-loser that had claimed a pool host should be UN-claimed (returned
    # to the pool with a fresh max-age TTL), NOT deleted.
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv("POOL_SIZE", "1")
    monkeypatch.setenv("POOL_HOST_MAX_AGE_HOURS", "4")
    get_settings.cache_clear()
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    pool_host = Host(
        id=uuid7(),
        name="lb-pool-1",
        status=HostStatus.ACTIVE.value,
        provider="exe",
        image=ExeSettings().default_image,  # pyright: ignore[reportCallIssue]
        env={},
        internal_ssh_host="lb-pool-1.example.ts.net",
        external_ssh_host="",
        external_ssh_port=22,
        known_hosts="",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=4),
        pool_member=True,
        last_error="",
    )
    async with async_session_factory() as session:
        session.add(pool_host)
        await session.commit()
        await session.refresh(pool_host)

    async with async_session_factory() as session:
        service = HostService(session)
        claimed = await service._try_claim_pool_host(provider="exe", expires_at=None)
        assert claimed is not None
        assert claimed.claimed_at is not None
        await service._release_idempotency_loser(claimed)

    async with async_session_factory() as session:
        reloaded = await session.get(Host, pool_host.id)
        assert reloaded is not None
        assert reloaded.claimed_at is None
        assert reloaded.expires_at is not None
        assert reloaded.expires_at > datetime.now(UTC) + timedelta(hours=3)

    get_settings.cache_clear()


async def test_record_idempotency_key_returns_false_on_duplicate(monkeypatch):
    # Regression: a losing-race duplicate insert must return False without
    # raising, and must leave host loaded for serialization. Recording on
    # self.session expired host mid-response and tripped MissingGreenlet.
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    async with async_session_factory() as session:
        service = HostService(session)
        host = await service.create_host(env={}, image=None, expires_at=None)

        assert await service._record_idempotency_key("dup-key", host) is True
        assert await service._record_idempotency_key("dup-key", host) is False

        # host stays usable — no MissingGreenlet on attribute access.
        assert host.id is not None
        assert host.status == HostStatus.PROVISIONING.value


async def test_create_host_idempotency_key_expired_creates_new_host(client, monkeypatch):
    # Manually insert an idempotency-key row pointing at a deleted host with
    # an expired timestamp; a fresh POST with the same key should NOT reuse it.
    from datetime import UTC, datetime, timedelta

    from hosts.models import IdempotencyKey

    mocked_provision = AsyncMock()
    monkeypatch.setattr("hosts.service.HostService.provision", mocked_provision)

    seed_host = await create_host_record(name="ch-seed", status="active")
    async with async_session_factory() as session:
        session.add(
            IdempotencyKey(
                key="stale-key",
                host_id=seed_host.id,
                created_at=datetime.now(UTC) - timedelta(hours=48),
                expires_at=datetime.now(UTC) - timedelta(hours=24),
            )
        )
        await session.commit()

    response = await client.post(
        "/hosts",
        headers={
            "Authorization": "Bearer service-token",
            "Idempotency-Key": "stale-key",
        },
    )

    assert response.status_code == 201
    # New host was created, not the stale one.
    assert response.json()["id"] != str(seed_host.id)


async def test_create_host_stores_and_returns_sizing(client, monkeypatch, stub_provider):
    """A sized request lands on the row and is reflected by both POST and GET."""
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
        json={"provider": "stub", "instance_type": "stub-large", "disk_gb": 200},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["instance_type"] == "stub-large"
    assert payload["disk_gb"] == 200

    fetched = await client.get(
        f"/hosts/{payload['id']}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert fetched.status_code == 200
    assert fetched.json()["instance_type"] == "stub-large"
    assert fetched.json()["disk_gb"] == 200


async def test_create_host_without_sizing_reflects_null(client, monkeypatch):
    """Omitted sizing means the provider's configured default, surfaced as null."""
    monkeypatch.setattr("hosts.service.HostService.provision", AsyncMock())

    response = await client.post("/hosts", headers={"Authorization": "Bearer service-token"})

    assert response.status_code == 201
    payload = response.json()
    assert payload["instance_type"] is None
    assert payload["disk_gb"] is None


async def test_create_host_rejects_sizing_the_provider_does_not_support(client):
    """exe exposes no per-request sizing; the request 400s before any row exists."""
    for field, value in (("instance_type", "t3.xlarge"), ("disk_gb", 200)):
        response = await client.post(
            "/hosts",
            headers={"Authorization": "Bearer service-token"},
            json={field: value},
        )

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "'exe'" in detail
        assert field in detail

    async with async_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Host)) == 0


async def test_create_host_rejects_blank_instance_type(client):
    response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
        json={"instance_type": "   "},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "instance_type"]
    assert "instance_type must not be blank" in detail[0]["msg"]


async def test_create_host_rejects_blank_image(client):
    response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
        json={"image": "   ", "env": {}},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "image"]
    assert "image must not be blank" in detail[0]["msg"]


async def test_create_host_rejects_reserved_env_keys(client):
    for key in ("TAILSCALE_AUTHKEY",):
        response = await client.post(
            "/hosts",
            headers={"Authorization": "Bearer service-token"},
            json={"env": {key: "caller-value"}},
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail[0]["loc"] == ["body", "env"]
        assert f"reserved env keys are not allowed: {key}" in detail[0]["msg"]


async def test_create_host_rejects_malformed_env_keys(client):
    # A key carrying '=' or whitespace would otherwise dodge the reserved-name
    # check and be re-parsed into a reserved var by a provider's env-file.
    for key in ("TAILSCALE_AUTHKEY=x", "FOO BAR"):
        response = await client.post(
            "/hosts",
            headers={"Authorization": "Bearer service-token"},
            json={"env": {key: "caller-value"}},
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail[0]["loc"] == ["body", "env"]
        assert "valid environment variable names" in detail[0]["msg"]


async def test_create_host_rejects_env_values_pam_would_change(client):
    # Every provider persists env through /etc/environment. pam_env cuts a
    # value at #, opens a quote it never closes, joins lines on a trailing
    # backslash, and stops at a line of 8192 bytes.
    for value, message in (
        ("ab#cd", "env value must be printable ASCII"),
        ('"abc', "env value must be printable ASCII"),
        ("a\\", "env value must be printable ASCII"),
        (" lead", "env value must be printable ASCII"),
        ("a\nb", "env value must be printable ASCII"),
        ("a" * 8187, "env entry is longer than 8191 bytes"),
    ):
        response = await client.post(
            "/hosts",
            headers={"Authorization": "Bearer service-token"},
            json={"env": {"KEY": value}},
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail[0]["loc"] == ["body", "env"]
        assert message in detail[0]["msg"]
        assert detail[0]["msg"].endswith(": KEY")


async def test_create_host_rejects_missing_or_bad_service_auth(client):
    missing_response = await client.post("/hosts")
    bad_response = await client.post(
        "/hosts",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert missing_response.status_code == 401
    assert bad_response.status_code == 403


async def test_create_host_rejects_basic_auth(client):
    response = await client.post("/hosts", auth=("user", "pass"))

    assert response.status_code == 401


async def test_list_hosts_returns_hosts_with_service_auth(client):
    host = await create_host_record(
        name="lb-sandbox-test",
        status="active",
    )

    response = await client.get(
        "/hosts",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 200
    assert response.json()[0]["id"] == str(host.id)
    assert response.json()[0]["name"] == "lb-sandbox-test"
    assert response.json()[0]["image"] == ExeSettings().default_image  # pyright: ignore[reportCallIssue]
    assert "env" not in response.json()[0]


async def test_list_hosts_rejects_basic_auth(client):
    await create_host_record(
        name="lb-sandbox-test",
        status="active",
    )

    response = await client.get("/hosts", auth=("user", "pass"))

    assert response.status_code == 401


async def test_get_host_returns_magic_dns_and_known_hosts(client):
    host = await create_host_record(
        name="lb-sandbox-test",
        status="active",
        known_hosts="lb-sandbox-test.example.ts.net ssh-ed25519 AAAATEST\n",
        tailscale_device_id="n123CNTRL",
    )

    response = await client.get(
        f"/hosts/{host.id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == str(host.id)
    assert payload["name"] == "lb-sandbox-test"
    assert payload["status"] == "active"
    assert payload["provider"] == "exe"
    assert payload["image"] == ExeSettings().default_image  # pyright: ignore[reportCallIssue]
    assert payload["internal_ssh_host"] == "lb-sandbox-test.example.ts.net"
    assert payload["external_ssh_host"] == ""
    assert payload["external_ssh_port"] == 22
    assert payload["known_hosts"] == "lb-sandbox-test.example.ts.net ssh-ed25519 AAAATEST\n"
    assert payload["tailscale_device_id"] == "n123CNTRL"
    assert payload["last_error"] == ""
    assert "env" not in payload


async def test_get_host_rejects_basic_auth(client):
    host = await create_host_record(
        name="lb-sandbox-test",
        status="active",
    )

    response = await client.get(f"/hosts/{host.id}", auth=("user", "pass"))

    assert response.status_code == 401


async def test_delete_host_deletes_vm_and_record_with_service_auth(client, monkeypatch):
    calls = []
    mocked_delete_device = AsyncMock(
        side_effect=lambda device_id: calls.append(("tailscale", device_id))
    )
    mocked_delete_vm = AsyncMock(side_effect=lambda name: calls.append(("exe", name)))
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", mocked_delete_device)
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", mocked_delete_vm)
    host = await create_host_record(
        name="lb-sandbox-test",
        status=HostStatus.ACTIVE.value,
        tailscale_device_id="n123CNTRL",
    )

    response = await client.delete(
        f"/hosts/{host.id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 204
    assert response.content == b""
    mocked_delete_device.assert_awaited_once_with("n123CNTRL")
    mocked_delete_vm.assert_awaited_once_with("lb-sandbox-test")
    assert calls == [("tailscale", "n123CNTRL"), ("exe", "lb-sandbox-test")]

    async with async_session_factory() as session:
        assert await session.get(Host, host.id) is None


async def test_delete_host_rejects_missing_or_bad_service_auth(client):
    host_id = uuid.UUID("00000000-0000-0000-0000-000000000021")

    missing_response = await client.delete(f"/hosts/{host_id}")
    bad_response = await client.delete(
        f"/hosts/{host_id}",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert missing_response.status_code == 401
    assert bad_response.status_code == 403


async def test_delete_host_rejects_basic_auth(client):
    host = await create_host_record(
        name="lb-sandbox-test",
        status=HostStatus.ACTIVE.value,
        tailscale_device_id="n123CNTRL",
    )

    response = await client.delete(f"/hosts/{host.id}", auth=("user", "pass"))

    assert response.status_code == 401


async def test_delete_host_returns_404_for_missing_host(client):
    host_id = uuid.UUID("00000000-0000-0000-0000-000000000022")

    response = await client.delete(
        f"/hosts/{host_id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "host not found"


async def test_delete_host_rejects_early_provisioning_states(client):
    for index, host_status in enumerate(
        (
            HostStatus.PROVISIONING.value,
            HostStatus.CREATING_NETWORK.value,
            HostStatus.CREATING_VM.value,
        ),
        start=1,
    ):
        host = await create_host_record(
            name=f"lb-sandbox-test-{index}",
            status=host_status,
        )

        response = await client.delete(
            f"/hosts/{host.id}",
            headers={"Authorization": "Bearer service-token"},
        )

        assert response.status_code == 409
        assert response.json()["detail"] == "host is still provisioning"


async def test_delete_host_preserves_record_when_vm_teardown_fails(client, monkeypatch):
    mocked_delete_device = AsyncMock()
    mocked_delete_vm = AsyncMock(side_effect=ProviderTransportError("exe.dev API failed"))
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", mocked_delete_device)
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", mocked_delete_vm)
    host = await create_host_record(
        name="lb-sandbox-test",
        status=HostStatus.ACTIVE.value,
        tailscale_device_id="n123CNTRL",
    )

    response = await client.delete(
        f"/hosts/{host.id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "host teardown could not be completed"
    mocked_delete_device.assert_awaited_once_with("n123CNTRL")
    mocked_delete_vm.assert_awaited_once_with("lb-sandbox-test")

    async with async_session_factory() as session:
        assert await session.get(Host, host.id) is not None


async def test_delete_host_skips_tailscale_release_when_device_id_missing(client, monkeypatch):
    # A VM-backed host without a tailscale_device_id is treated as already
    # released — delete proceeds to VM teardown and completes. This covers the
    # retry path after a previous teardown that succeeded on Tailscale but
    # failed before deleting the VM.
    mocked_delete_device = AsyncMock()
    mocked_delete_vm = AsyncMock()
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", mocked_delete_device)
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", mocked_delete_vm)
    host = await create_host_record(
        name="lb-sandbox-test",
        status=HostStatus.ACTIVE.value,
    )

    response = await client.delete(
        f"/hosts/{host.id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 204
    mocked_delete_device.assert_not_awaited()
    mocked_delete_vm.assert_awaited_once_with("lb-sandbox-test")

    async with async_session_factory() as session:
        assert await session.get(Host, host.id) is None


async def test_delete_host_clears_tailscale_device_id_when_vm_teardown_fails(client, monkeypatch):
    # First call: release_device succeeds, delete_vm fails. The host row must
    # be preserved AND have its tailscale_device_id cleared, so a retry does
    # not try to delete the already-released device.
    mocked_delete_device = AsyncMock()
    mocked_delete_vm = AsyncMock(side_effect=ProviderTransportError("exe.dev API failed"))
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", mocked_delete_device)
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", mocked_delete_vm)
    host = await create_host_record(
        name="lb-sandbox-test",
        status=HostStatus.ACTIVE.value,
        tailscale_device_id="n123CNTRL",
    )

    first = await client.delete(
        f"/hosts/{host.id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert first.status_code == 503
    mocked_delete_device.assert_awaited_once_with("n123CNTRL")

    async with async_session_factory() as session:
        persisted = await session.get(Host, host.id)
        assert persisted is not None
        assert persisted.tailscale_device_id is None

    # Retry: delete_vm now succeeds. release_device must NOT be called again.
    mocked_delete_vm.side_effect = None
    mocked_delete_device.reset_mock()

    second = await client.delete(
        f"/hosts/{host.id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert second.status_code == 204
    mocked_delete_device.assert_not_awaited()

    async with async_session_factory() as session:
        assert await session.get(Host, host.id) is None


async def test_delete_host_succeeds_when_vm_already_absent_at_provider(client, monkeypatch):
    # If exe.dev evicted the VM out from under us (or a previous delete
    # partially completed), the next delete should still clean up the DB row.
    from providers.exceptions import ProviderNotFoundError

    mocked_delete_device = AsyncMock()
    mocked_delete_vm = AsyncMock(
        side_effect=ProviderNotFoundError("vm 'lb-sandbox-test' not found")
    )
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", mocked_delete_device)
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", mocked_delete_vm)
    host = await create_host_record(
        name="lb-sandbox-test",
        status=HostStatus.ACTIVE.value,
        tailscale_device_id="n123CNTRL",
    )

    response = await client.delete(
        f"/hosts/{host.id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 204
    mocked_delete_device.assert_awaited_once_with("n123CNTRL")
    mocked_delete_vm.assert_awaited_once_with("lb-sandbox-test")

    async with async_session_factory() as session:
        assert await session.get(Host, host.id) is None


async def test_delete_host_preserves_record_when_tailscale_teardown_fails(client, monkeypatch):
    mocked_delete_device = AsyncMock(side_effect=NetworkTransportError("Tailscale API failed"))
    mocked_delete_vm = AsyncMock()
    monkeypatch.setattr("networking.tailscale.Tailscale.release_device", mocked_delete_device)
    monkeypatch.setattr("providers.exe.provider.ExeProvider.delete_vm", mocked_delete_vm)
    host = await create_host_record(
        name="lb-sandbox-test",
        status=HostStatus.ACTIVE.value,
        tailscale_device_id="n123CNTRL",
    )

    response = await client.delete(
        f"/hosts/{host.id}",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "host teardown could not be completed"
    mocked_delete_device.assert_awaited_once_with("n123CNTRL")
    mocked_delete_vm.assert_not_awaited()

    async with async_session_factory() as session:
        assert await session.get(Host, host.id) is not None


async def create_host_record(
    *,
    id: uuid.UUID | None = None,
    name: str,
    status: str,
    external_ssh_host: str = "",
    internal_ssh_host: str | None = None,
    known_hosts: str = "",
    env: dict[str, str] | None = None,
    image: str | None = None,
    tailscale_device_id: str | None = None,
    expires_at: datetime | None = None,
    claimed_at: datetime | None = None,
    pool_member: bool = False,
) -> Host:
    now = utc_now()
    settings = get_settings()
    # Default the internal hostname to the tailnet form so existing test
    # call sites (which were written for the Tailscale-on world) keep their
    # original semantics without explicitly passing it.
    if internal_ssh_host is None and settings.tailscale_enabled:
        internal_ssh_host = f"{name}.{TailscaleSettings().tailnet}"  # pyright: ignore[reportCallIssue]
    host = Host(
        id=id or uuid7(),
        name=name,
        status=status,
        image=image or ExeSettings().default_image,  # pyright: ignore[reportCallIssue]
        external_ssh_host=external_ssh_host,
        internal_ssh_host=internal_ssh_host,
        known_hosts=known_hosts,
        tailscale_device_id=tailscale_device_id,
        env=env or {},
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
        claimed_at=claimed_at,
        pool_member=pool_member,
    )
    async with async_session_factory() as session:
        session.add(host)
        await session.commit()
        await session.refresh(host)
    return host


async def test_renew_host_with_empty_body_extends_by_default_ttl(client):
    # Keepalive: an empty renew body bumps the lease to now + LEASE_DEFAULT_TTL.
    # The host here is demand-provisioned (pool_member=False, claimed_at NULL),
    # pinning that unclaimed non-pool hosts stay renewable by their caller.
    host = await create_host_record(
        name="lb-renew-default",
        status=HostStatus.ACTIVE.value,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    ttl = timedelta(seconds=get_settings().lease_default_ttl)

    before = utc_now()
    response = await client.post(
        f"/hosts/{host.id}/renew",
        headers={"Authorization": "Bearer service-token"},
    )
    after = utc_now()

    assert response.status_code == 200
    assert response.json()["id"] == str(host.id)
    expires_at = datetime.fromisoformat(response.json()["expires_at"])
    assert before + ttl <= expires_at <= after + ttl


async def test_renew_host_accepts_explicit_expires_at(client):
    host = await create_host_record(
        name="lb-renew-explicit",
        status=HostStatus.ACTIVE.value,
        expires_at=utc_now() + timedelta(hours=1),
    )
    requested = utc_now() + timedelta(days=30)

    response = await client.post(
        f"/hosts/{host.id}/renew",
        headers={"Authorization": "Bearer service-token"},
        json={"expires_at": requested.isoformat()},
    )

    assert response.status_code == 200
    renewed = datetime.fromisoformat(response.json()["expires_at"])
    assert abs((renewed - requested).total_seconds()) < 1


async def test_renew_host_renews_bootstrapping_host(client):
    # A host still waiting on device discovery / keyscan is already caller-owned;
    # its keepalive must not 409 just because activation hasn't finished.
    host = await create_host_record(
        name="lb-renew-boot",
        status=HostStatus.BOOTSTRAPPING.value,
        expires_at=utc_now() + timedelta(minutes=5),
    )

    response = await client.post(
        f"/hosts/{host.id}/renew",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 200
    assert datetime.fromisoformat(response.json()["expires_at"]) > utc_now()


async def test_renew_host_rejects_non_renewable_statuses(client):
    for index, host_status in enumerate(
        (
            HostStatus.PROVISIONING.value,
            HostStatus.CREATING_NETWORK.value,
            HostStatus.CREATING_VM.value,
            HostStatus.ERROR.value,
        ),
        start=1,
    ):
        host = await create_host_record(name=f"lb-renew-blocked-{index}", status=host_status)

        response = await client.post(
            f"/hosts/{host.id}/renew",
            headers={"Authorization": "Bearer service-token"},
        )

        assert response.status_code == 409
        assert response.json()["error_code"] == "HOST_STATE"


async def test_renew_host_rejects_unclaimed_pool_host(client):
    # Warm pool members are managed by pool maintenance; a caller may only
    # renew a host it owns (claimed, or demand-provisioned).
    host = await create_host_record(
        name="lb-renew-pool",
        status=HostStatus.ACTIVE.value,
        pool_member=True,
        expires_at=utc_now() + timedelta(hours=4),
    )

    response = await client.post(
        f"/hosts/{host.id}/renew",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "HOST_STATE"

    async with async_session_factory() as session:
        persisted = await session.get(Host, host.id)
        assert persisted is not None
        assert persisted.expires_at == host.expires_at


async def test_renew_host_allows_claimed_pool_host(client):
    host = await create_host_record(
        name="lb-renew-claimed",
        status=HostStatus.ACTIVE.value,
        pool_member=True,
        claimed_at=utc_now(),
        expires_at=utc_now() + timedelta(hours=4),
    )

    response = await client.post(
        f"/hosts/{host.id}/renew",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 200


async def test_renew_host_returns_404_for_missing_host(client):
    response = await client.post(
        f"/hosts/{uuid.uuid4()}/renew",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "host not found"


async def test_renew_host_rejects_past_expires_at(client):
    host = await create_host_record(name="lb-renew-past", status=HostStatus.ACTIVE.value)

    response = await client.post(
        f"/hosts/{host.id}/renew",
        headers={"Authorization": "Bearer service-token"},
        json={"expires_at": "2020-01-01T12:00:00+00:00"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "expires_at"]
    assert "in the future" in detail[0]["msg"]


async def test_renew_host_rejects_naive_expires_at(client):
    host = await create_host_record(name="lb-renew-naive", status=HostStatus.ACTIVE.value)

    response = await client.post(
        f"/hosts/{host.id}/renew",
        headers={"Authorization": "Bearer service-token"},
        json={"expires_at": "2099-01-01T12:00:00"},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail[0]["loc"] == ["body", "expires_at"]


async def test_renew_host_rejects_missing_or_bad_service_auth(client):
    host_id = uuid.uuid4()

    missing_response = await client.post(f"/hosts/{host_id}/renew")
    bad_response = await client.post(
        f"/hosts/{host_id}/renew",
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert missing_response.status_code == 401
    assert bad_response.status_code == 403


async def test_delete_host_maps_database_failure_to_503(client, monkeypatch):
    # A SQLAlchemyError during teardown must surface as a controlled 503, like
    # create_host's DB-failure path, not an unhandled 500.
    from sqlalchemy.exc import SQLAlchemyError

    monkeypatch.setattr(
        "hosts.service.HostService.delete_host",
        AsyncMock(side_effect=SQLAlchemyError("db down")),
    )
    response = await client.delete(
        f"/hosts/{uuid.uuid4()}",
        headers={"Authorization": "Bearer service-token"},
    )
    assert response.status_code == 503
    assert response.json()["error_code"] == "HOST_TEARDOWN"
