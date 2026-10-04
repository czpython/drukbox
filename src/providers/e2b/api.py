import shlex
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from e2b import AsyncSandbox, CommandExitException, SandboxQuery, SandboxState
from e2b import exceptions as errors

from providers.exceptions import (
    ProviderAuthError,
    ProviderCommandError,
    ProviderNotFoundError,
    ProviderTransportError,
)

from .settings import E2BSettings


@contextmanager
def e2b_request() -> Iterator[None]:
    try:
        yield
    except errors.AuthenticationException:
        raise ProviderAuthError("E2B API authentication failed") from None
    except errors.NotFoundException:
        raise ProviderNotFoundError("E2B resource was not found") from None
    except (errors.InvalidArgumentException, CommandExitException):
        raise ProviderCommandError("E2B rejected the request or bootstrap command") from None
    except errors.SandboxException as exc:
        if exc.status_code == 403:
            raise ProviderAuthError("E2B API authentication failed") from None
        if exc.status_code == 404:
            raise ProviderNotFoundError("E2B resource was not found") from None
        if exc.status_code and 400 <= exc.status_code < 500 and exc.status_code != 429:
            raise ProviderCommandError("E2B API rejected the request") from None
        raise ProviderTransportError("E2B request failed") from None
    except (
        errors.ServiceBusyException,
        httpx.RequestError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ):
        raise ProviderTransportError("E2B request failed") from None


class E2BAPI:
    def __init__(self, settings: E2BSettings) -> None:
        self.settings = settings

    async def create_sandbox(self, name: str, *, image: str, label: str) -> AsyncSandbox:
        with e2b_request():
            return await AsyncSandbox.create(
                template=image,
                timeout=self.settings.session_timeout_seconds,
                metadata={"name": name, "managed-by": label},
                lifecycle={"on_timeout": "kill", "auto_resume": False},
                network={"allow_public_traffic": False},
                api_key=self.settings.api_key,
                request_timeout=self.settings.api_timeout,
                retries=0,
            )

    async def bootstrap(self, sandbox: AsyncSandbox, script: str) -> None:
        with e2b_request():
            await sandbox.commands.run(
                f"bash -e -c {shlex.quote(script)}",
                user="root",
                timeout=120,
                request_timeout=self.settings.api_timeout,
            )

    async def delete_sandbox(self, name: str, *, label: str) -> None:
        metadata = {"name": name, "managed-by": label}

        with e2b_request():
            pages = AsyncSandbox.list(
                query=SandboxQuery(
                    metadata=metadata, state=[SandboxState.RUNNING, SandboxState.PAUSED]
                ),
                api_key=self.settings.api_key,
                request_timeout=self.settings.api_timeout,
                retries=0,
            )
            sandbox_ids: list[str] = []
            while pages.has_next:
                for sandbox in await pages.next_items():
                    if all(sandbox.metadata.get(key) == value for key, value in metadata.items()):
                        sandbox_ids.append(sandbox.sandbox_id)
            if not sandbox_ids:
                raise ProviderNotFoundError(f"E2B sandbox {name!r} was not found")
            for sandbox_id in sandbox_ids:
                await AsyncSandbox.kill(
                    sandbox_id,
                    api_key=self.settings.api_key,
                    request_timeout=self.settings.api_timeout,
                    retries=0,
                )

    async def diagnose(self) -> str:
        with e2b_request():
            pages = AsyncSandbox.list(
                limit=1,
                api_key=self.settings.api_key,
                request_timeout=self.settings.api_timeout,
                retries=0,
            )
            await pages.next_items()
        return "E2B sandbox API authentication succeeded"
