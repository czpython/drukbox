import json
import shlex
from typing import Any, Self

import httpx

from providers import environment

from .exceptions import (
    ExeAuthError,
    ExeCommandError,
    ExeIntegrationAlreadyExistsError,
    ExeIntegrationNotFoundError,
    ExeResponseError,
    ExeVMNotFoundError,
)
from .settings import ExeSettings

# Creating the VM itself is fast, but `new` waits on the setup script, and the
# tailscale join inside it is slow: ~21s healthy, uncomfortably close to the
# default 30s budget. Creation alone gets this read floor; a configured timeout
# above it wins.
_CREATE_VM_READ_TIMEOUT_FLOOR = 90.0


def _encode_setup_script(script: str) -> str:
    """Encode a multi-line script as a double-quoted exe.dev argument value.

    exe.dev's command parser unescapes ``\\n``, ``\\"``, and ``\\\\`` inside
    double-quoted strings (see https://exe.dev/docs/cli-new). Newlines must be
    serialized as literal backslash-n; other shell metacharacters pass through
    unmodified inside the quotes.
    """
    escaped = script.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


class ExeAPI:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        default_image: str = "",
        timeout: float = 30.0,
        connect_timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.default_image = default_image
        self.timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self.create_vm_timeout = httpx.Timeout(
            timeout, connect=connect_timeout, read=max(timeout, _CREATE_VM_READ_TIMEOUT_FLOOR)
        )
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def from_settings(cls) -> Self:
        # pydantic-settings resolves required fields from env at runtime;
        # pyright can't see that statically.
        settings = ExeSettings()  # pyright: ignore[reportCallIssue]
        return cls(
            base_url=settings.api_url,
            token=settings.api_token,
            default_image=settings.default_image,
            timeout=settings.api_timeout,
        )

    async def create_vm(
        self,
        *,
        name: str | None = None,
        image: str | None = None,
        env: dict[str, str] | None = None,
        setup_script: str | None = None,
        tags: list[str] | None = None,
        no_email: bool = True,
        registry_auth: str | None = None,
    ) -> dict[str, Any]:
        command_parts = ["new", "--json"]

        if name:
            command_parts.append(f"--name={shlex.quote(name)}")

        vm_image = image or self.default_image

        if vm_image:
            command_parts.append(f"--image={shlex.quote(vm_image)}")

        if registry_auth:
            command_parts.append(f"--registry-auth={shlex.quote(registry_auth)}")

        if env:
            # exe puts --env in /etc/profile.d. Only a login shell reads it.
            shebang, _, body = (setup_script or "#!/bin/bash").partition("\n")
            parts = [shebang, *environment.export(env), environment.bashrc(env), body]
            setup_script = "\n".join(part for part in parts if part)
        if setup_script:
            command_parts.append(f"--setup-script={_encode_setup_script(setup_script)}")

        if tags:
            for tag in tags:
                command_parts.append(f"--tag={shlex.quote(tag)}")

        if no_email:
            command_parts.append("--no-email")

        if env:
            for key, value in env.items():
                command_parts.extend(["--env", shlex.quote(f"{key}={value}")])
        return await self._exec_dict(" ".join(command_parts), timeout=self.create_vm_timeout)

    async def list_vms(
        self,
        *,
        pattern: str | None = None,
        detailed: bool = True,
    ) -> list[dict[str, Any]]:
        command_parts = ["ls", "--json"]

        if detailed:
            command_parts.append("-l")
        if pattern:
            command_parts.append(shlex.quote(pattern))
        payload = await self._exec_dict(" ".join(command_parts))
        return payload["vms"]

    async def get_vm(self, name: str) -> dict[str, Any]:
        for vm in await self.list_vms(pattern=name):
            if vm["vm_name"] == name:
                return vm
        raise ExeVMNotFoundError(f"exe.dev VM '{name}' was not found")

    async def restart_vm(self, name: str) -> dict[str, Any]:
        return await self._exec_dict(f"restart --json {shlex.quote(name)}")

    async def rename_vm(self, name: str, *, new_name: str) -> dict[str, Any]:
        return await self._exec_dict(f"rename --json {shlex.quote(name)} {shlex.quote(new_name)}")

    async def delete_vm(self, name: str) -> dict[str, Any]:
        try:
            return await self._exec_dict(f"rm --json {shlex.quote(name)}")
        except ExeCommandError as exc:
            if "not found" in str(exc).lower():
                raise ExeVMNotFoundError(f"exe.dev VM '{name}' was not found") from exc
            raise

    async def create_http_proxy(
        self,
        *,
        name: str,
        target: str,
        headers: dict[str, str],
    ) -> None:
        command_parts = [
            "integrations",
            "add",
            "http-proxy",
            f"--name={shlex.quote(name)}",
            f"--target={shlex.quote(target)}",
        ]

        for header_name, header_value in headers.items():
            command_parts.append(f"--header={shlex.quote(f'{header_name}: {header_value}')}")

        try:
            await self._request(" ".join(command_parts))
        except ExeCommandError as exc:
            if "already exists" in str(exc).lower():
                raise ExeIntegrationAlreadyExistsError(
                    f"exe.dev integration '{name}' already exists"
                ) from exc
            raise

    async def update_http_proxy(
        self,
        *,
        name: str,
        target: str,
        headers: dict[str, str],
    ) -> None:
        command_parts = [
            "integrations",
            "edit",
            shlex.quote(name),
            f"--target={shlex.quote(target)}",
        ]

        for header_name, header_value in headers.items():
            command_parts.append(f"--header={shlex.quote(f'{header_name}: {header_value}')}")

        try:
            await self._request(" ".join(command_parts))
        except ExeCommandError as exc:
            if "not found" in str(exc).lower():
                raise ExeIntegrationNotFoundError(
                    f"exe.dev integration '{name}' was not found"
                ) from exc
            raise

    async def list_http_proxies(self) -> list[str]:
        response = await self._request("integrations list --json")
        payload = self._parse_json_response(response.text)

        if not isinstance(payload, list):
            raise ExeResponseError("exe.dev integrations list returned non-array JSON output")

        names: list[str] = []
        for integration in payload:
            if not isinstance(integration, dict) or not isinstance(integration.get("name"), str):
                raise ExeResponseError("exe.dev integration returned an invalid name")
            names.append(integration["name"])
        return names

    async def delete_http_proxy(self, name: str) -> None:
        try:
            await self._request(f"integrations remove {shlex.quote(name)}")
        except ExeCommandError as exc:
            if "not found" in str(exc).lower():
                raise ExeIntegrationNotFoundError(
                    f"exe.dev integration '{name}' was not found"
                ) from exc
            raise

    async def attach_http_proxy(self, name: str, *, attach_vm: str) -> None:
        target = shlex.quote(f"vm:{attach_vm}")
        try:
            await self._request(f"integrations attach {shlex.quote(name)} {target}")
        except ExeCommandError as exc:
            self._raise_attachment_error(exc, integration=name, attach_vm=attach_vm)

    async def detach_http_proxy(self, name: str, *, attach_vm: str) -> None:
        target = shlex.quote(f"vm:{attach_vm}")
        try:
            await self._request(f"integrations detach {shlex.quote(name)} {target}")
        except ExeCommandError as exc:
            self._raise_attachment_error(exc, integration=name, attach_vm=attach_vm)

    @staticmethod
    def _raise_attachment_error(
        exc: ExeCommandError,
        *,
        integration: str,
        attach_vm: str,
    ) -> None:
        detail = str(exc).lower()

        if "not found" not in detail:
            raise exc

        # Bias toward integration-not-found when the message uses integration
        # vocabulary; otherwise treat as VM-not-found. The exe.dev CLI surfaces
        # the target as `vm:<name>`, so loose substring matches like `"vm" in detail`
        # would misroute integration errors that echo the target token.
        if "integration" in detail or "http-proxy" in detail or "http_proxy" in detail:
            raise ExeIntegrationNotFoundError(
                f"exe.dev integration '{integration}' was not found"
            ) from exc
        raise ExeVMNotFoundError(f"exe.dev VM '{attach_vm}' was not found") from exc

    async def whoami(self) -> dict[str, Any]:
        return await self._exec_dict("whoami --json")

    async def aclose(self) -> None:
        if not self._client:
            return

        await self._client.aclose()
        self._client = None

    async def _exec_dict(
        self, command: str, *, timeout: httpx.Timeout | None = None
    ) -> dict[str, Any]:
        response = await self._request(command, timeout=timeout)
        payload = self._parse_json_response(response.text)

        if not isinstance(payload, dict):
            raise ExeResponseError("exe.dev API returned non-object JSON output")
        return payload

    async def _request(
        self, command: str, *, timeout: httpx.Timeout | None = None
    ) -> httpx.Response:
        try:
            response = await self._get_client().post(
                "/exec", content=command, timeout=timeout or self.timeout
            )
        except httpx.RequestError as exc:
            # {exc!r}, not {exc}: bare transport errors like ReadTimeout() stringify empty.
            raise ExeResponseError(f"exe.dev API transport failed: {exc!r}") from exc

        if response.status_code == 401:
            raise ExeAuthError("exe.dev API authentication failed")

        if 400 <= response.status_code < 500:
            detail = response.text.strip()
            if not detail:
                detail = f"exe.dev API command failed with status {response.status_code}"
            raise ExeCommandError(detail)

        if response.status_code >= 500:
            raise ExeResponseError(f"exe.dev API request failed with status {response.status_code}")
        return response

    def _get_client(self) -> httpx.AsyncClient:
        if not self._client:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "text/plain; charset=utf-8",
                    "Accept": "application/json",
                },
            )
        return self._client

    def _parse_json_response(self, text: str) -> dict[str, Any] | list[dict[str, Any]]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        chunks = [line.strip() for line in text.splitlines() if line.strip()]

        if not chunks:
            raise ExeResponseError("exe.dev API returned empty output")

        parsed_chunks: list[dict[str, Any] | list[dict[str, Any]]] = []

        for chunk in chunks:
            try:
                parsed_chunks.append(json.loads(chunk))
            except json.JSONDecodeError as exc:
                raise ExeResponseError("exe.dev API returned non-JSON output") from exc

        if len(parsed_chunks) == 1:
            return parsed_chunks[0]

        if all(isinstance(chunk, dict) for chunk in parsed_chunks):
            merged: dict[str, Any] = {}
            for chunk in parsed_chunks:
                if isinstance(chunk, dict):
                    merged.update(chunk)
            return merged
        raise ExeResponseError("exe.dev API returned unsupported multi-part JSON output")
