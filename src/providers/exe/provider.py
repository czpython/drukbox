from typing import ClassVar, Self

from core.settings import get_settings
from providers.base import VMCreateResult, VMProvider
from providers.capabilities import HttpProxyCapability, TemplateCapability
from providers.derived_image import build_derived_image, remove_derived_image
from providers.docker.api import DockerCLI
from providers.docker.exceptions import DockerProviderError
from providers.exceptions import (
    ProviderCommandError,
    ProviderHttpProxyExistsError,
    ProviderHttpProxyNotFoundError,
    ProviderNotFoundError,
    ProviderTargetVMNotFoundError,
    ProviderTransportError,
)
from providers.exe.api import ExeAPI
from providers.exe.exceptions import (
    ExeIntegrationAlreadyExistsError,
    ExeIntegrationNotFoundError,
    ExeVMNotFoundError,
)
from providers.exe.settings import ExeSettings


class ExeProvider(VMProvider, HttpProxyCapability, TemplateCapability):
    name: ClassVar[str] = "exe"
    diagnose_hint: ClassVar[str] = "check_exe_dev_api_token_and_url"

    def __init__(
        self,
        api: ExeAPI,
        settings: ExeSettings,
        *,
        docker: DockerCLI,
        service_label: str = "drukbox",
    ) -> None:
        self.api = api
        self.settings = settings
        self.docker = docker
        self._service_label = service_label

    @classmethod
    def from_settings(cls) -> Self:
        core = get_settings()
        return cls(
            ExeAPI.from_settings(),
            ExeSettings(),  # pyright: ignore[reportCallIssue]
            docker=DockerCLI(),
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
        # Tags are operator-facing: `exe ls --tag=managed-by-<env>` shows what this deployment owns.
        payload = await self.api.create_vm(
            name=name,
            image=image,
            env=env,
            setup_script=setup_script,
            tags=[f"managed-by-{self._service_label}"],
        )
        return VMCreateResult(
            provider_id=str(payload["vm_name"]),
            name=str(payload["vm_name"]),
            ssh_port=int(payload["ssh_port"]),
            # exe.dev's `new --json` payload uses `ssh_dest` for the
            # reachable SSH address; KeyError here means the wire contract
            # drifted and is meant to be loud.
            ssh_host=str(payload["ssh_dest"]),
            ssh_username=self.settings.ssh_username,
        )

    async def delete_vm(self, name: str) -> None:
        try:
            await self.api.delete_vm(name)
        except ExeVMNotFoundError as exc:
            raise ProviderNotFoundError(str(exc)) from exc

    async def materialize_template(
        self,
        *,
        base_image: str,
        setup_script: str,
        label: str,
    ) -> str:
        registry = self.settings.template_registry
        username = self.settings.registry_username
        password = self.settings.registry_password

        if not (registry and username and password):
            missing_settings = [
                name
                for name, value in (
                    ("EXE_TEMPLATE_REGISTRY", registry),
                    ("EXE_REGISTRY_USERNAME", username),
                    ("EXE_REGISTRY_PASSWORD", password),
                )
                if not value
            ]
            raise ProviderCommandError(
                f"exe template registry is not configured; missing settings: "
                f"{', '.join(missing_settings)}"
            )

        tag = await build_derived_image(
            self.docker,
            base_image=base_image,
            setup_script=setup_script,
            repository=registry,
        )
        registry_host, *_ = registry.partition("/")
        try:
            await self.docker.login(registry_host, username, password)
            await self.docker.push_image(tag)
        except DockerProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc
        return tag

    async def delete_template(self, handle: str) -> None:
        # Registry deletion is registry-specific; this provider only owns the local build tag.
        await remove_derived_image(self.docker, handle)

    async def aclose(self) -> None:
        await self.api.aclose()

    async def diagnose(self) -> str:
        payload = await self.api.whoami()
        return str(payload["email"])

    async def create_http_proxy(
        self,
        *,
        name: str,
        target: str,
        headers: dict[str, str],
    ) -> None:
        try:
            await self.api.create_http_proxy(name=name, target=target, headers=headers)
        except ExeIntegrationAlreadyExistsError as exc:
            raise ProviderHttpProxyExistsError(str(exc)) from exc

    async def delete_http_proxy(self, name: str) -> None:
        try:
            await self.api.delete_http_proxy(name)
        except ExeIntegrationNotFoundError as exc:
            raise ProviderHttpProxyNotFoundError(str(exc)) from exc

    async def attach_http_proxy(self, name: str, *, attach_vm: str) -> None:
        try:
            await self.api.attach_http_proxy(name, attach_vm=attach_vm)
        except ExeVMNotFoundError as exc:
            raise ProviderTargetVMNotFoundError(str(exc)) from exc
        except ExeIntegrationNotFoundError as exc:
            raise ProviderHttpProxyNotFoundError(str(exc)) from exc

    async def detach_http_proxy(self, name: str, *, attach_vm: str) -> None:
        try:
            await self.api.detach_http_proxy(name, attach_vm=attach_vm)
        except ExeVMNotFoundError as exc:
            raise ProviderTargetVMNotFoundError(str(exc)) from exc
        except ExeIntegrationNotFoundError as exc:
            raise ProviderHttpProxyNotFoundError(str(exc)) from exc
