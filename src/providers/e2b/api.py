import shlex
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from e2b import AsyncSandbox, SandboxQuery
from e2b import exceptions as errors

from providers.exceptions import (
    ProviderAuthError,
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
)

from .settings import E2BSettings


class E2BAPI:
    def __init__(self, settings: E2BSettings) -> None:
        self.settings = settings

    async def create_sandbox(self, name: str, *, image: str, label: str) -> AsyncSandbox:
        with self.request():
            return await AsyncSandbox.create(
                template=image,
                timeout=self.settings.max_lifetime_seconds,
                metadata={"name": name, "managed-by": label},
                lifecycle={"on_timeout": "kill"},
                network={"allow_public_traffic": False},
                api_key=self.settings.api_key,
                request_timeout=self.settings.api_timeout,
                # A retried create can leave a second sandbox behind.
                retries=0,
            )

    async def bootstrap(self, sandbox: AsyncSandbox, script: str) -> None:
        # E2B's errors can quote the command and its output, and the command
        # carries the Tailscale auth key.
        try:
            with self.request():
                await sandbox.commands.run(
                    # eth0 has only a link-local address, which tailscaled reads
                    # as no network: `tailscale up` would wait forever.
                    "ip link set dummy0 up && ip addr replace 10.255.255.1/32 dev dummy0"
                    f" && bash -e -c {shlex.quote(script)}",
                    user="root",
                    timeout=120,
                    request_timeout=self.settings.api_timeout,
                )
        except ProviderCommandError:
            raise ProviderCommandError("E2B sandbox bootstrap failed") from None

    async def delete_sandbox(self, name: str, *, label: str) -> None:
        with self.request():
            sandboxes = await AsyncSandbox.list(
                query=SandboxQuery(metadata={"name": name, "managed-by": label}),
                api_key=self.settings.api_key,
                request_timeout=self.settings.api_timeout,
            ).next_items()
            if not sandboxes:
                raise ProviderNotFoundError(f"E2B sandbox {name!r} was not found")
            for sandbox in sandboxes:
                await AsyncSandbox.kill(
                    sandbox.sandbox_id,
                    api_key=self.settings.api_key,
                    request_timeout=self.settings.api_timeout,
                )

    async def diagnose(self) -> str:
        with self.request():
            await AsyncSandbox.list(
                limit=1, api_key=self.settings.api_key, request_timeout=self.settings.api_timeout
            ).next_items()
        return "E2B API authentication succeeded"

    @contextmanager
    def request(self) -> Iterator[None]:
        try:
            yield
        except errors.AuthenticationException:
            raise ProviderAuthError("E2B API authentication failed") from None
        except errors.NotFoundException:
            raise ProviderNotFoundError("E2B resource was not found") from None
        except (
            errors.TimeoutException,
            errors.RateLimitException,
            errors.ServiceBusyException,
            httpx.RequestError,
        ):
            raise ProviderTransportError("E2B request failed") from None
        except errors.SandboxException as exc:
            raise ProviderCommandError(f"E2B rejected the request: {exc}") from None
