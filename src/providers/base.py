import abc
from dataclasses import dataclass
from typing import ClassVar, NamedTuple, Self


@dataclass(frozen=True)
class VMCreateResult:
    provider_id: str
    name: str
    ssh_username: str
    # Unset ssh_host/ssh_port mean the VM has no directly dialable address.
    ssh_host: str = ""
    ssh_port: int = 0
    private_key: str | None = None
    # The public half of a per-VM keypair. The service stores it so the SSH
    # gateway can authenticate callers of gateway providers.
    public_key: str | None = None


class TerminalSize(NamedTuple):
    columns: int
    rows: int

    def __str__(self):
        return f"{self.columns}x{self.rows}"


class SandboxProcess(abc.ABC):
    """One live process inside a sandbox. The gateway pumps bytes between an
    SSH channel and this object; the receive methods return b"" at the end."""

    @classmethod
    @abc.abstractmethod
    async def open(
        cls,
        name: str,
        *,
        command: str | None,
        terminal: TerminalSize | None,
    ) -> "SandboxProcess":
        """Open a process in the sandbox: a shell when command is None, with
        a PTY when terminal is not None. Raises neutral provider errors."""
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
    # The process class that serves this provider's hosts through the SSH
    # gateway. None means the hosts have their own dialable sshd and the
    # gateway plays no part.
    gateway_process_class: ClassVar[type[SandboxProcess] | None] = None

    @property
    def sftp_server_command(self) -> str:
        """The command that starts the OpenSSH SFTP server in this provider's
        image. The gateway runs it to serve SFTP. Empty when the gateway does
        not serve the provider."""
        return ""

    # Remediation slug attached to a failed /doctor probe. Owned here because
    # the provider is what knows how its own dependency gets fixed.
    diagnose_hint: ClassVar[str]
    # Time limit for the /doctor probe. Owned here for the same reason: the
    # provider knows the cost of its own probe. CLI-backed probes can be
    # slower than the default.
    diagnose_timeout_seconds: ClassVar[float] = 5.0
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
