import io
import re
import tarfile

from uuid6 import uuid7

from core.settings import get_settings
from providers.exceptions import ProviderNotFoundError, ProviderTransportError

from .api import DockerAPI
from .exceptions import DockerImageNotFoundError, DockerProviderError


def derive_image_name(
    *,
    label: str,
    repository: str = "drukbox-template",
) -> str:
    purpose = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:95].rstrip("-")
    return f"{repository}:{purpose or 'template'}-{uuid7().hex}"


def create_build_context(*, base_image: str, setup_script: str) -> bytes:
    """A gzipped tar holding the synthesized Dockerfile and the setup script."""
    dockerfile = (
        f"FROM {base_image}\n"
        "COPY setup.sh /drukbox-setup.sh\n"
        "RUN sh /drukbox-setup.sh && rm /drukbox-setup.sh\n"
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in (("Dockerfile", dockerfile), ("setup.sh", setup_script)):
            data = content.encode("utf-8")
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


async def build_derived_image(
    docker: DockerAPI,
    *,
    base_image: str,
    setup_script: str,
    label: str,
) -> str:
    settings = get_settings()
    image = derive_image_name(
        label=label,
        repository=(
            f"{settings.registry_host}/{settings.template_repository}"
            if settings.template_repository
            else "drukbox-template"
        ),
    )
    context_tar = create_build_context(base_image=base_image, setup_script=setup_script)
    try:
        await docker.build_image(image, context_tar)
        if settings.template_repository:
            return await docker.push_image(
                image,
                username=settings.registry_username,
                password=settings.registry_password.get_secret_value(),
            )
    except DockerProviderError as exc:
        raise ProviderTransportError(str(exc)) from exc
    return image


async def remove_derived_image(docker: DockerAPI, image: str) -> None:
    try:
        # Keep the unique build tag in the pinned reference so cleanup removes
        # this build's tag, even when another build has the same digest.
        await docker.remove_image(image.partition("@")[0])
    except DockerImageNotFoundError as exc:
        raise ProviderNotFoundError(f"docker image '{image}' was not found") from exc
    except DockerProviderError as exc:
        raise ProviderTransportError(str(exc)) from exc
