import abc
from dataclasses import dataclass
from typing import ClassVar, NamedTuple, Self

from providers.capabilities import ProxyInjection, SecretInjectionCapability


@dataclass(frozen=True)
class VMCreateResult:
    provider_id: str
    name: str
    ssh_username: str
    # Empty when the VM has no address of its own.
    ssh_host: str = ""
    ssh_port: int = 0
    private_key: str | None = None
    # The gateway authenticates callers with it.
    public_key: str | None = None


class TerminalSize(NamedTuple):
    columns: int
    rows: int

    def __str__(self):
        return f"{self.columns}x{self.rows}"


class SandboxProcess(abc.ABC):
    """One live process in a sandbox. The receive methods return b"" at the end."""

    @classmethod
    @abc.abstractmethod
    async def open(
        cls,
        name: str,
        *,
        command: str | None,
        terminal: TerminalSize | None,
    ) -> "SandboxProcess":
        """A shell when command is None. A PTY when terminal is given."""
        ...  # pragma: no cover

    @abc.abstractmethod
    async def receive(self, max_bytes: int) -> bytes: ...  # pragma: no cover

    @abc.abstractmethod
    async def receive_stderr(self, max_bytes: int) -> bytes: ...  # pragma: no cover

    @abc.abstractmethod
    def send(self, data: bytes) -> None: ...  # pragma: no cover

    @abc.abstractmethod
    def send_eof(self) -> None: ...  # pragma: no cover

    @abc.abstractmethod
    def resize(self, size: TerminalSize) -> None: ...  # pragma: no cover

    @abc.abstractmethod
    async def wait(self) -> int: ...  # pragma: no cover

    @abc.abstractmethod
    async def aclose(self) -> None: ...  # pragma: no cover


class VMProvider(abc.ABC):
    name: ClassVar[str]
    secrets: SecretInjectionCapability = ProxyInjection()
    # None when the hosts have an sshd of their own and no gateway.
    gateway_process_class: ClassVar[type[SandboxProcess] | None] = None
    # The remediation slug of a failed /doctor probe.
    diagnose_hint: ClassVar[str]
    diagnose_timeout_seconds: ClassVar[float] = 5.0
    # HostService rejects a sized request before any row or VM exists.
    supports_instance_type: ClassVar[bool] = False
    supports_disk_gb: ClassVar[bool] = False
    # A local provider's hosts keep the external path only.
    supports_tailnet: ClassVar[bool] = True

    @classmethod
    @abc.abstractmethod
    def from_settings(cls) -> Self: ...

    @property
    @abc.abstractmethod
    def default_image(self) -> str: ...

    @property
    @abc.abstractmethod
    def bootstrap_ssh_timeout_seconds(self) -> float: ...

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
        """One cheap read-only probe. Raise on failure: /doctor classifies the error."""
        ...

    @abc.abstractmethod
    async def aclose(self) -> None: ...
