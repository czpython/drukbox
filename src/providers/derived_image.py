import contextlib
import hashlib
import tempfile
from collections.abc import Iterator
from pathlib import Path

from providers.docker.api import DockerCLI
from providers.docker.exceptions import DockerImageNotFoundError, DockerProviderError
from providers.exceptions import ProviderNotFoundError, ProviderTransportError


def derived_image_tag(
    *,
    base_image: str,
    setup_script: str,
    repository: str = "drukbox-template",
) -> str:
    identity = base_image.encode("utf-8") + b"\0" + setup_script.encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:12]
    return f"{repository}:{digest}"


@contextlib.contextmanager
def derived_image_context(*, base_image: str, setup_script: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="drukbox-template-") as directory:
        context = Path(directory)
        context.joinpath("setup.sh").write_bytes(setup_script.encode("utf-8"))
        context.joinpath("Dockerfile").write_text(
            f"FROM {base_image}\n"
            "COPY setup.sh /drukbox-setup.sh\n"
            "RUN sh /drukbox-setup.sh && rm /drukbox-setup.sh\n",
            encoding="utf-8",
        )
        yield context


async def build_derived_image(
    docker_cli: DockerCLI,
    *,
    base_image: str,
    setup_script: str,
    repository: str = "drukbox-template",
) -> str:
    tag = derived_image_tag(
        base_image=base_image,
        setup_script=setup_script,
        repository=repository,
    )
    try:
        with derived_image_context(base_image=base_image, setup_script=setup_script) as context:
            await docker_cli.build_image(tag, context)
    except DockerProviderError as exc:
        raise ProviderTransportError(str(exc)) from exc
    return tag


async def remove_derived_image(docker_cli: DockerCLI, image: str) -> None:
    try:
        await docker_cli.remove_image(image)
    except DockerImageNotFoundError as exc:
        raise ProviderNotFoundError(f"docker image '{image}' was not found") from exc
    except DockerProviderError as exc:
        raise ProviderTransportError(str(exc)) from exc
