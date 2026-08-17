import abc
from dataclasses import dataclass
from typing import ClassVar, Self


@dataclass(frozen=True)
class VMCreateResult:
    provider_id: str
    name: str
    ssh_port: int
    ssh_username: str
    ssh_host: str = ""
    private_key: str | None = None


class VMProvider(abc.ABC):
    name: ClassVar[str]
    # Remediation slug attached to a failed /doctor probe. Owned here because
    # the provider is what knows how its own dependency gets fixed.
    diagnose_hint: ClassVar[str]
    # Which per-request sizing fields create_vm honors. HostService rejects a
    # sized request up front — before any host row or VM exists — when the
    # target provider leaves these False.
    supports_instance_type: ClassVar[bool] = False
    supports_disk_gb: ClassVar[bool] = False
    # Whether this provider's hosts can join the tailnet when the service runs
    # in Tailscale mode. A local provider leaves it False and its hosts keep
    # the external path only, even on a tailnet-mode service.
    supports_tailnet: ClassVar[bool] = True

    @classmethod
    @abc.abstractmethod
    def from_settings(cls) -> Self:
        """Construct the provider from process settings. Used as the registry factory."""
        ...

    @property
    @abc.abstractmethod
    def default_image(self) -> str:
        """Fallback image when the caller doesn't pass one."""
        ...

    @property
    @abc.abstractmethod
    def bootstrap_ssh_timeout_seconds(self) -> float:
        """How long HostService.scan_known_hosts retries ssh-keyscan for."""
        ...

    @abc.abstractmethod
    async def create_vm(
        self,
        *,
        name: str,
        image: str,
        env: dict[str, str] | None = None,
        setup_script: str | None = None,
        instance_type: str | None = None,
        disk_gb: int | None = None,
    ) -> VMCreateResult: ...

    @abc.abstractmethod
    async def delete_vm(self, name: str) -> None: ...

    @abc.abstractmethod
    async def diagnose(self) -> str:
        """Run one cheap, non-mutating probe against the provider.

        Returns a short detail string on success; raises on failure. The
        ``/doctor`` orchestrator wraps the call to classify the error and
        attach a remediation hint, so implementations should NOT catch.
        """
        ...

    @abc.abstractmethod
    async def aclose(self) -> None: ...
