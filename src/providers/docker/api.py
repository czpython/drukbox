import io

import aiodocker
import aiohttp

from .exceptions import DockerImageNotFoundError, DockerTransportError, DockerVMNotFoundError

_MAX_ERROR_DETAIL_CHARS = 8_000


def _detail(exc: Exception) -> str:
    # The engine's message, capped so a long build log stays readable.
    return str(exc)[-_MAX_ERROR_DETAIL_CHARS:]


class DockerAPI:
    """Async client for the Docker Engine API, through aiodocker.

    The daemon address resolves the way the docker CLI's own does —
    explicit ``DOCKER_HOST``, the active docker context, or the default
    socket — aiodocker implements that chain. The session is created
    lazily on first use: providers are constructed by the registry in
    sync code, and an aiohttp session must be born on the running loop.

    aiodocker and aiohttp exception types stop at this class; callers see
    only the ``Docker*Error`` hierarchy.
    """

    def __init__(self, docker: aiodocker.Docker | None = None) -> None:
        self._docker = docker

    def _get_client(self) -> aiodocker.Docker:
        if not self._docker:
            try:
                self._docker = aiodocker.Docker()
            except ValueError as exc:
                # No resolvable daemon address: no DOCKER_HOST, no docker
                # context, no socket at the default paths.
                raise DockerTransportError(str(exc)) from exc
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
        try:
            container = await self._get_client().containers.run(config, name=name)
        except (aiodocker.DockerError, aiohttp.ClientError) as exc:
            raise DockerTransportError(_detail(exc)) from exc
        return container.id

    async def published_ssh_port(self, name: str) -> int:
        try:
            bindings = await self._get_client().containers.container(name).port(22)
        except aiodocker.DockerError as exc:
            if exc.status == 404:
                raise DockerVMNotFoundError(str(exc)) from exc
            raise DockerTransportError(_detail(exc)) from exc
        except aiohttp.ClientError as exc:
            raise DockerTransportError(str(exc)) from exc
        # An absent binding means sshd's port never bound — the container
        # exited before publishing.
        if not bindings or not bindings[0].get("HostPort"):
            raise DockerTransportError(f"container {name!r} published no SSH port")
        return int(bindings[0]["HostPort"])

    async def remove_container(self, name: str) -> None:
        try:
            await self._get_client().containers.container(name).delete(force=True, v=True)
        except aiodocker.DockerError as exc:
            if exc.status == 404:
                raise DockerVMNotFoundError(str(exc)) from exc
            raise DockerTransportError(_detail(exc)) from exc
        except aiohttp.ClientError as exc:
            raise DockerTransportError(str(exc)) from exc

    async def build_image(self, image: str, context_tar: bytes) -> None:
        try:
            await self._get_client().images.build(
                fileobj=io.BytesIO(context_tar),
                encoding="gzip",
                tag=image,
            )
        except (aiodocker.DockerError, aiohttp.ClientError) as exc:
            raise DockerTransportError(_detail(exc)) from exc

    async def remove_image(self, image: str) -> None:
        try:
            await self._get_client().images.delete(image)
        except aiodocker.DockerError as exc:
            if exc.status == 404:
                raise DockerImageNotFoundError(str(exc)) from exc
            raise DockerTransportError(_detail(exc)) from exc
        except aiohttp.ClientError as exc:
            raise DockerTransportError(str(exc)) from exc

    async def push_image(
        self,
        image: str,
        *,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        # Credentials travel per call as an X-Registry-Auth header; the
        # global docker credential store is never touched.
        auth = {"username": username, "password": password} if username and password else None
        try:
            await self._get_client().images.push(image, auth=auth)
        except (aiodocker.DockerError, aiohttp.ClientError) as exc:
            raise DockerTransportError(_detail(exc)) from exc

    async def server_version(self) -> str:
        try:
            version = await self._get_client().version()
        except (aiodocker.DockerError, aiohttp.ClientError) as exc:
            raise DockerTransportError(_detail(exc)) from exc
        return str(version["Version"])

    async def aclose(self) -> None:
        if self._docker:
            await self._docker.close()
            self._docker = None
