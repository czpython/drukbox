import io
import tarfile
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from core.settings import get_settings
from providers.docker.images import (
    build_derived_image,
    create_build_context,
    derive_image_name,
)


def test_create_build_context_contains_the_base_and_verbatim_script() -> None:
    setup_script = "printf 'first\\nsecond\\n' | tee /tmp/output"

    context_tar = create_build_context(base_image="sandbox:base", setup_script=setup_script)

    with tarfile.open(fileobj=io.BytesIO(context_tar), mode="r:gz") as archive:
        members = {member.name: archive.extractfile(member) for member in archive.getmembers()}
        assert set(members) == {"Dockerfile", "setup.sh"}
        setup_member = members["setup.sh"]
        dockerfile_member = members["Dockerfile"]
        assert setup_member and setup_member.read() == setup_script.encode("utf-8")
        assert dockerfile_member and dockerfile_member.read().decode("utf-8") == (
            "FROM sandbox:base\n"
            "COPY setup.sh /drukbox-setup.sh\n"
            "RUN sh /drukbox-setup.sh && rm /drukbox-setup.sh\n"
        )


def test_derive_image_name_is_deterministic_and_base_specific() -> None:
    first = derive_image_name(base_image="sandbox:base", setup_script="apt-get update")
    repeated = derive_image_name(base_image="sandbox:base", setup_script="apt-get update")
    different_base = derive_image_name(base_image="sandbox:other", setup_script="apt-get update")

    assert first == repeated
    assert first.startswith("drukbox-template:")
    assert len(first.removeprefix("drukbox-template:")) == 12
    assert different_base != first


async def test_build_derived_image_publishes_to_the_template_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "registry_host", "ghcr.io")
    monkeypatch.setattr(get_settings(), "registry_username", "bot")
    monkeypatch.setattr(get_settings(), "registry_password", SecretStr("secret"))
    monkeypatch.setattr(get_settings(), "template_repository", "acme/templates")
    docker = MagicMock(build_image=AsyncMock(), push_image=AsyncMock())

    image = await build_derived_image(
        docker, base_image="sandbox:base", setup_script="apt-get update"
    )

    assert image.startswith("ghcr.io/acme/templates:")
    assert docker.build_image.await_args.args[0] == image
    docker.push_image.assert_awaited_once_with(image, username="bot", password="secret")
