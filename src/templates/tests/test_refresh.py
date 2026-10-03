from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from core.database import async_session_factory
from core.settings import get_settings
from hosts.service import utc_now
from providers.docker.api import DockerAPI
from providers.docker.exceptions import DockerTransportError
from providers.docker.provider import DockerProvider
from providers.docker.settings import DockerSettings
from providers.docker_sbx.api import SbxCLI
from providers.docker_sbx.exceptions import DockerSbxTransportError
from providers.docker_sbx.provider import DockerSbxProvider
from providers.docker_sbx.settings import DockerSbxSettings
from providers.exceptions import ProviderTransportError
from providers.exe.api import ExeAPI
from providers.exe.provider import ExeProvider
from providers.exe.settings import ExeSettings
from templates.janitor import reap_templates
from templates.models import Template
from templates.service import TemplateService

AUTH_HEADERS = {"Authorization": "Bearer service-token"}


async def test_a_moved_tag_builds_a_new_template_and_the_old_one_expires(client, template_provider):
    payload = {"provider": template_provider.name, "setup_script": "echo ready"}
    first = await client.post("/templates", headers=AUTH_HEADERS, json=payload)
    template_provider.base_image_ref = "stub@sha256:" + "b" * 64
    second = await client.post("/templates", headers=AUTH_HEADERS, json=payload)

    assert first.status_code == second.status_code == 202
    assert first.json()["id"] != second.json()["id"]
    assert "base_image_ref" not in second.json()
    assert template_provider.refreshed == ["stub:base", "stub:base"]
    assert template_provider.built == [
        ("stub@sha256:" + "a" * 64, "echo ready", ""),
        ("stub@sha256:" + "b" * 64, "echo ready", ""),
    ]
    async with async_session_factory() as session:
        templates = await TemplateService(session).list()
        assert templates[0].base_image_ref == template_provider.base_image_ref
        assert [str(template.id) for template in templates] == [
            second.json()["id"],
            first.json()["id"],
        ]
        older = templates[-1]
        older.created_at = utc_now() - timedelta(seconds=get_settings().template_unused_ttl + 1)
        await session.commit()

    await reap_templates()

    async with async_session_factory() as session:
        assert not await session.get(Template, older.id)
        assert await session.get(Template, templates[0].id)
    assert template_provider.deleted == [older.image]


async def test_build_uses_the_reference_recorded_before_the_tag_moves(template_provider):
    async with async_session_factory() as session:
        service = TemplateService(session)
        template, created = await service.get_or_create(
            provider=template_provider.name, base_image=None, setup_script="echo ready", label=""
        )
        assert created
        reference = template.base_image_ref
        template_provider.base_image_ref = "stub@sha256:" + "b" * 64
        await service.build(template.id)

    assert template_provider.built == [(reference, "echo ready", "")]


async def test_refresh_failure_returns_502_without_a_template(client, template_provider):
    template_provider.refresh_error = ProviderTransportError("registry unavailable")

    response = await client.post(
        "/templates",
        headers=AUTH_HEADERS,
        json={"provider": template_provider.name, "setup_script": "echo ready"},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "registry unavailable"
    async with async_session_factory() as session:
        assert await TemplateService(session).list() == []


@pytest.mark.parametrize("provider_name", ["docker", "exe", "docker-sbx"])
async def test_each_template_provider_refreshes_its_image_stores(
    provider_name, tmp_path, monkeypatch
):
    monkeypatch.setattr(get_settings(), "registry_host", "ghcr.io")
    monkeypatch.setattr(get_settings(), "registry_username", "bot")
    monkeypatch.setattr(get_settings(), "registry_password", SecretStr("secret"))
    image = "ghcr.io/acme/sandbox:latest"
    docker = MagicMock(spec=DockerAPI)
    reference = "sandbox@sha256:" + "a" * 64
    docker.pull_image = AsyncMock(return_value=reference)
    sbx = MagicMock(spec=SbxCLI)
    providers = {
        "docker": DockerProvider(docker, DockerSettings()),
        "exe": ExeProvider(
            MagicMock(spec=ExeAPI),
            ExeSettings(api_token="test", default_image="sandbox:latest"),
            docker=docker,
        ),
        "docker-sbx": DockerSbxProvider(
            sbx, DockerSbxSettings(workspace_root=tmp_path), docker=docker
        ),
    }

    assert await providers[provider_name].refresh_base_image(image) == reference

    docker.pull_image.assert_awaited_once_with(
        image, registry_auth={"username": "bot", "password": "secret"}
    )
    if provider_name == "docker-sbx":
        saved, archive = docker.save_image.await_args.args
        assert saved == image
        sbx.load_template.assert_awaited_once_with(archive)
        assert not archive.exists()
    else:
        docker.save_image.assert_not_awaited()
    docker.remove_image.assert_not_awaited()
    docker.pull_image.side_effect = DockerTransportError("registry unavailable")
    with pytest.raises(ProviderTransportError, match="registry unavailable"):
        await providers[provider_name].refresh_base_image(image)


async def test_sbx_loads_an_unchanged_base_image_once(tmp_path):
    docker = MagicMock(spec=DockerAPI)
    docker.pull_image = AsyncMock(return_value="sandbox@sha256:" + "a" * 64)
    sbx = MagicMock(spec=SbxCLI)
    provider = DockerSbxProvider(sbx, DockerSbxSettings(workspace_root=tmp_path), docker=docker)

    await provider.refresh_base_image("sandbox:latest")
    await provider.refresh_base_image("sandbox:latest")
    docker.pull_image.return_value = "sandbox@sha256:" + "b" * 64
    await provider.refresh_base_image("sandbox:latest")

    assert docker.pull_image.await_count == 3
    assert sbx.load_template.await_count == 2


async def test_sbx_load_failure_prevents_template_reuse(tmp_path):
    docker = MagicMock(spec=DockerAPI)
    docker.pull_image = AsyncMock(return_value="sandbox@sha256:" + "a" * 64)
    sbx = MagicMock(spec=SbxCLI)
    sbx.load_template.side_effect = DockerSbxTransportError("load failed")
    provider = DockerSbxProvider(sbx, DockerSbxSettings(workspace_root=tmp_path), docker=docker)

    with pytest.raises(ProviderTransportError, match="load failed"):
        await provider.refresh_base_image("sandbox:latest")
