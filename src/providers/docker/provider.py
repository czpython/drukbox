import contextlib
from typing import ClassVar, Self

from core.settings import get_settings
from providers.base import VMCreateResult, VMProvider
from providers.capabilities import ProxyInjection, TemplateCapability
from providers.exceptions import (
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
)
from providers.ssh_keys import generate_ed25519_keypair

from .api import DockerAPI
from .exceptions import DockerProviderError, DockerVMNotFoundError
from .images import build_derived_image, remove_derived_image
from .settings import DockerSettings

_AUTHORIZED_KEY_ENV = "DRUKBOX_AUTHORIZED_KEY"
_ENV_KEYS_ENV = "DRUKBOX_ENV_KEYS"
_RESERVED_ENV_KEYS = frozenset({_AUTHORIZED_KEY_ENV, _ENV_KEYS_ENV})


class DockerProvider(VMProvider, TemplateCapability):
    name: ClassVar[str] = "docker"
    secret_injection = ProxyInjection()
    diagnose_hint: ClassVar[str] = "check_docker_daemon_is_running"
    # A local container has no path onto the tailnet; its hosts keep the
    # published 127.0.0.1 sshd port even on a tailnet-mode service.
    supports_tailnet: ClassVar[bool] = False

    def __init__(
        self,
        api: DockerAPI,
        settings: DockerSettings,
        *,
        service_label: str = "drukbox",
    ) -> None:
        self.api = api
        self.settings = settings
        self._service_label = service_label

    @classmethod
    def from_settings(cls) -> Self:
        core = get_settings()
        return cls(
            DockerAPI(),
            DockerSettings(),  # pyright: ignore[reportCallIssue]
            service_label=core.service_label,
        )

    @property
    def default_image(self) -> str:
        return self.settings.default_image

    @property
    def bootstrap_ssh_timeout_seconds(self) -> float:
        return self.settings.bootstrap_ssh_timeout_seconds

    async def create_vm(
        self,
        *,
        name: str,
        image: str,
        env: dict[str, str] | None = None,
        setup_script: str | None = None,
        instance_type: str | None = None,
        disk_gb: int | None = None,
    ) -> VMCreateResult:
        # A setup script only ever arrives when Tailscale is enabled, and a
        # local container has no path onto the tailnet. Fail loud rather than
        # silently start a box that never joins.
        if setup_script:
            raise ProviderCommandError(
                "docker provider runs sandboxes locally and does not support "
                "Tailscale networking; set TAILSCALE_ENABLED=false"
            )

        caller_env = env or {}
        # These names carry the per-VM public key and the env-key manifest the
        # entrypoint reads; a caller-supplied value would clobber the generated
        # key (locking the caller out) or rewrite the manifest. Reject rather
        # than let `**caller_env` silently win.
        if reserved := _RESERVED_ENV_KEYS.intersection(caller_env):
            raise ProviderCommandError(
                f"env keys reserved by the docker provider are not allowed: "
                f"{', '.join(sorted(reserved))}"
            )

        private_key, public_key = generate_ed25519_keypair()
        # The sandbox entrypoint seeds authorized_keys from the public key and
        # persists the named caller vars into the container's session env.
        container_env = {
            _AUTHORIZED_KEY_ENV: public_key,
            _ENV_KEYS_ENV: " ".join(caller_env),
            **caller_env,
        }
        labels = {"managed-by": self._service_label, "drukbox-host-name": name}

        try:
            await self.api.run_container(name=name, image=image, env=container_env, labels=labels)
        except DockerProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc

        try:
            ssh_port = await self.api.published_ssh_port(name)
        except DockerProviderError as exc:
            # Best-effort cleanup of the half-started container: a failure here
            # must not mask the original error or leak a Docker-specific
            # exception past the boundary. The janitor reaps it by name if this
            # doesn't land.
            with contextlib.suppress(DockerProviderError):
                await self.api.remove_container(name)
            raise ProviderTransportError(str(exc)) from exc

        return VMCreateResult(
            provider_id=name,
            name=name,
            ssh_port=ssh_port,
            ssh_host="127.0.0.1",
            ssh_username=self.settings.ssh_username,
            private_key=private_key,
        )

    async def delete_vm(self, name: str) -> None:
        try:
            await self.api.remove_container(name)
        except DockerVMNotFoundError as exc:
            raise ProviderNotFoundError(f"docker container '{name}' was not found") from exc
        except DockerProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc

    async def build_template_image(
        self,
        *,
        base_image: str,
        setup_script: str,
        label: str,
    ) -> str:
        return await build_derived_image(
            self.api,
            base_image=base_image,
            setup_script=setup_script,
        )

    async def delete_template_image(self, image: str) -> None:
        await remove_derived_image(self.api, image)

    async def diagnose(self) -> str:
        return f"docker server {await self.api.server_version()}"

    async def aclose(self) -> None:
        await self.api.aclose()
