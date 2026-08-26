import hashlib
import io
import tarfile

from providers.exceptions import ProviderNotFoundError, ProviderTransportError

from .api import DockerAPI
from .exceptions import DockerImageNotFoundError, DockerProviderError


def derive_image_tag(
    *,
    base_image: str,
    setup_script: str,
    repository: str = "drukbox-template",
) -> str:
    identity = base_image.encode("utf-8") + b"\0" + setup_script.encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:12]
    return f"{repository}:{digest}"


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
    repository: str = "drukbox-template",
) -> str:
    tag = derive_image_tag(
        base_image=base_image,
        setup_script=setup_script,
        repository=repository,
    )
    context_tar = create_build_context(base_image=base_image, setup_script=setup_script)
    try:
        await docker.build_image(tag, context_tar)
    except DockerProviderError as exc:
        raise ProviderTransportError(str(exc)) from exc
    return tag


async def remove_derived_image(docker: DockerAPI, image: str) -> None:
    try:
        await docker.remove_image(image)
    except DockerImageNotFoundError as exc:
        raise ProviderNotFoundError(f"docker image '{image}' was not found") from exc
    except DockerProviderError as exc:
        raise ProviderTransportError(str(exc)) from exc
