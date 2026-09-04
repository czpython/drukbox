import asyncio
import ipaddress
import re
import secrets
import socket
from typing import cast
from urllib.parse import urlsplit

from secret_proxy.exceptions import SecretProxyRejectedError

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
ROUTING_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "expect",
        "forwarded",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "via",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
    }
)


class SecretRules:
    def __init__(self, *, allow_private_upstreams: bool = False) -> None:
        self.allow_private_upstreams = allow_private_upstreams
        self._rules: dict[str, dict[str, dict[str, object]]] = {}
        self._tokens: dict[str, str] = {}

    async def put(
        self,
        *,
        vm: str,
        host: str,
        env_var: str,
        headers: dict[str, str],
        placeholder: str,
        value: str,
    ) -> None:
        if not vm or ":" in vm or not env_var or not placeholder:
            raise SecretProxyRejectedError("secret rule identifiers are invalid")
        if any(
            not _HEADER_NAME.fullmatch(name)
            or name.lower() in ROUTING_HEADERS
            or "\r" in value
            or "\n" in value
            for name, value in headers.items()
        ):
            raise SecretProxyRejectedError("secret rule headers are invalid")
        hostname, port = self.split_host(host)
        await self.resolve(hostname, port)
        normalized_host = self.normalize_host(hostname, port)
        new_header_names = {name.lower() for name in headers}
        for name, rule in self._rules.get(vm, {}).items():
            existing_headers = cast(dict[str, str], rule["headers"])
            if (
                name != env_var
                and rule["host"] == normalized_host
                and (
                    rule["placeholder"] == placeholder
                    or new_header_names.intersection(
                        existing_name.lower() for existing_name in existing_headers
                    )
                )
            ):
                raise SecretProxyRejectedError("secret rules for one host conflict")
        self._rules.setdefault(vm, {})[env_var] = {
            "host": normalized_host,
            "headers": dict(headers),
            "placeholder": placeholder,
            "value": value,
        }
        self._tokens.setdefault(vm, secrets.token_urlsafe(32))

    def delete(self, *, vm: str, env_var: str) -> None:
        vm_rules = self._rules.get(vm)
        if not vm_rules or env_var not in vm_rules:
            raise SecretProxyRejectedError(f"secret '{env_var}' is not registered for VM '{vm}'")
        del vm_rules[env_var]
        if not vm_rules:
            del self._rules[vm]
            del self._tokens[vm]

    def names(self, *, vm: str) -> list[str]:
        return sorted(self._rules.get(vm, {}))

    def route(self, *, vm: str) -> dict[str, str]:
        token = self._tokens.get(vm)
        if not token:
            raise SecretProxyRejectedError(f"VM '{vm}' has no registered secret route")
        return {"username": vm, "password": token}

    def authenticate(self, *, vm: str, token: str) -> bool:
        expected = self._tokens.get(vm, "")
        return bool(expected) and secrets.compare_digest(expected, token)

    def for_host(self, *, vm: str, host: str) -> list[dict[str, object]]:
        hostname, port = self.split_host(host)
        normalized_host = self.normalize_host(hostname, port)
        return [
            rule for rule in self._rules.get(vm, {}).values() if rule["host"] == normalized_host
        ]

    async def resolve(self, hostname: str, port: int) -> list[str]:
        try:
            addresses = await asyncio.get_running_loop().getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        except OSError as error:
            raise SecretProxyRejectedError(
                f"upstream host '{hostname}' could not be resolved"
            ) from error

        resolved = sorted({address[4][0] for address in addresses})
        if not resolved:
            raise SecretProxyRejectedError(f"upstream host '{hostname}' has no address")
        if not self.allow_private_upstreams:
            unsafe = [
                address for address in resolved if not ipaddress.ip_address(address).is_global
            ]
            if unsafe:
                raise SecretProxyRejectedError(
                    f"upstream host '{hostname}' resolves to a private or reserved address"
                )
        return resolved

    @staticmethod
    def split_host(host: str) -> tuple[str, int]:
        parsed = urlsplit(f"//{host}")
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise SecretProxyRejectedError(
                "upstream host must be a host name with an optional port"
            )
        try:
            port = parsed.port or 443
        except ValueError as error:
            raise SecretProxyRejectedError("upstream host has an invalid port") from error
        try:
            hostname = parsed.hostname.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise SecretProxyRejectedError("upstream host is invalid") from error
        return hostname, port

    @staticmethod
    def normalize_host(hostname: str, port: int) -> str:
        host = f"[{hostname}]" if ":" in hostname else hostname
        return host if port == 443 else f"{host}:{port}"
