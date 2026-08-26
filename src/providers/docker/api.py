import contextlib
import io
from collections.abc import Iterator

import aiodocker
import aiohttp

from .exceptions import DockerImageNotFoundError, DockerTransportError, DockerVMNotFoundError

_MAX_ERROR_DETAIL_CHARS = 8_000


@contextlib.contextmanager
def _translated(not_found: type[Exception] | None = None) -> Iterator[None]:
    # aiodocker and aiohttp types stop here; nothing outside this package
    # sees them. A 404 becomes the caller-named not-found error; stream
    # errors (a failing build step, a rejected push) and transport
    # failures become DockerTransportError with the engine's message,
    # capped so a long build log stays readable.
    try:
        yield
    except aiodocker.DockerError as exc:
        detail = str(exc)[-_MAX_ERROR_DETAIL_CHARS:]
        if not_found and exc.status == 404:
            raise not_found(detail) from exc
        raise DockerTransportError(detail) from exc
    except (aiohttp.ClientError, ValueError, OSError) as exc:
        raise DockerTransportError(f"docker engine request failed: {exc}") from exc


class DockerAPI:
    """Async client for the Docker Engine API, through aiodocker.

    The daemon address resolves the way the docker CLI's own does —
    explicit ``DOCKER_HOST``, the active docker context, or the default
    socket — aiodocker implements that chain. The session is created
    lazily on first use: providers are constructed by the registry in
    sync code, and an aiohttp session must be born on the running loop.
    """

    def __init__(self, docker: aiodocker.Docker | None = None) -> None:
        self._docker = docker

    def _client(self) -> aiodocker.Docker:
        if not self._docker:
            self._docker = aiodocker.Docker()
        return self._docker

    async def run_container(
        self,
        *,
        name: str,
        image: str,
        env: dict[str, str],
        labels: dict[str, str],
    ) -> str:
        # Publish the in-container sshd on a random loopback host port: the
        # sandbox is reachable from the host that runs drukbox, never from the
        # network. The per-VM key remains the auth boundary.
        config = {
            "Image": image,
            "Env": [f"{key}={value}" for key, value in env.items()],
            "Labels": labels,
            "ExposedPorts": {"22/tcp": {}},
            "HostConfig": {
                "PortBindings": {"22/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]},
            },
        }
        with _translated():
            container = await self._client().containers.run(config, name=name)
        return container.id

    async def published_ssh_port(self, name: str) -> int:
        with _translated(not_found=DockerVMNotFoundError):
            bindings = await self._client().containers.container(name).port(22)
        # An absent binding means sshd's port never bound — the container
        # exited before publishing.
        if not bindings or not bindings[0].get("HostPort"):
            raise DockerTransportError(f"container {name!r} published no SSH port")
        return int(bindings[0]["HostPort"])

    async def remove_container(self, name: str) -> None:
        with _translated(not_found=DockerVMNotFoundError):
            await self._client().containers.container(name).delete(force=True, v=True)

    async def build_image(self, tag: str, context_tar: bytes) -> None:
        with _translated():
            await self._client().images.build(
                fileobj=io.BytesIO(context_tar),
                encoding="gzip",
                tag=tag,
            )

    async def remove_image(self, tag: str) -> None:
        with _translated(not_found=DockerImageNotFoundError):
            await self._client().images.delete(tag)

    async def push_image(
        self,
        tag: str,
        *,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        # Credentials travel per call as an X-Registry-Auth header; the
        # global docker credential store is never touched.
        auth = {"username": username, "password": password} if username and password else None
        with _translated():
            await self._client().images.push(tag, auth=auth)

    async def server_version(self) -> str:
        with _translated():
            version = await self._client().version()
        return str(version["Version"])

    async def aclose(self) -> None:
        if self._docker:
            await self._docker.close()
            self._docker = None
