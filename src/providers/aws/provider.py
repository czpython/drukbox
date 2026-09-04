import ipaddress
import logging
from typing import ClassVar, Self

import httpx

from core.settings import get_settings
from providers.base import VMCreateResult, VMProvider
from providers.capabilities import ReverseTunnelCapability, SecretProxyRoutingCapability
from providers.exceptions import ProviderNotFoundError, ProviderTransportError
from providers.secret_proxy import ProxySecretInjectionCapability
from providers.setup_script import inject_authorized_keys, inject_env_exports
from providers.ssh_keys import generate_ed25519_keypair
from secret_proxy.client import SecretProxyClient

from .api import AwsAPI
from .exceptions import AwsProviderError, AwsVMNotFoundError
from .settings import AwsSettings

logger = logging.getLogger(__name__)

_MANAGED_SG_NAME = "drukbox-managed"
_MANAGED_SG_DESCRIPTION = "SSH ingress for drukbox-managed sandbox VMs."


class AWSProvider(
    ProxySecretInjectionCapability,
    VMProvider,
    ReverseTunnelCapability,
    SecretProxyRoutingCapability,
):
    name: ClassVar[str] = "aws"
    diagnose_hint: ClassVar[str] = "check_aws_credentials_and_region"
    supports_instance_type = True
    supports_disk_gb = True

    def __init__(
        self,
        api: AwsAPI,
        settings: AwsSettings,
        *,
        tailscale_enabled: bool,
        service_label: str = "drukbox",
        secret_proxy: SecretProxyClient | None = None,
    ) -> None:
        self.api = api
        self.settings = settings
        self._tailscale_enabled = tailscale_enabled
        self._service_label = service_label
        self.secret_proxy = secret_proxy or SecretProxyClient.from_settings()
        self._cached_sg_id: str | None = settings.security_group_id

    @classmethod
    def from_settings(cls) -> Self:
        core = get_settings()
        aws_settings = AwsSettings()  # pyright: ignore[reportCallIssue]
        return cls(
            AwsAPI.from_settings(aws_settings),
            aws_settings,
            tailscale_enabled=core.tailscale_enabled,
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
        authorized_keys: tuple[str, ...] = (),
        instance_type: str | None = None,
        disk_gb: int | None = None,
    ) -> VMCreateResult:
        if image.startswith("ami-"):
            ami_id = image
        else:
            try:
                ami_id = await self.api.resolve_ssm_parameter(image)
            except AwsProviderError as exc:
                raise ProviderTransportError(str(exc)) from exc

        tags = {
            "Name": name,
            "managed-by": self._service_label,
            "drukbox-host-name": name,
        }

        # With Tailscale on, SSH access is via tailscaled-SSH using tailnet
        # ACLs; we don't need an EC2 KeyName at all, and no public path
        # means no SG ingress to manage.
        if self._tailscale_enabled:
            key_name: str | None = None
            private_key: str | None = None
            associate_public_ip = False
            security_group_id: str | None = None
        else:
            key_name = f"drukbox-{name}"
            private_key, public_key = generate_ed25519_keypair()
            try:
                await self.api.import_key_pair(
                    key_name=key_name, public_key_openssh=public_key, tags=tags
                )
            except AwsProviderError as exc:
                raise ProviderTransportError(str(exc)) from exc
            associate_public_ip = True
            try:
                security_group_id = await self._resolve_security_group_id()
            except AwsProviderError as exc:
                await self.api.delete_key_pair(key_name)
                raise ProviderTransportError(str(exc)) from exc

        user_data = inject_authorized_keys(
            setup_script or "",
            username=self.settings.ssh_username,
            authorized_keys=authorized_keys,
        )
        user_data = inject_env_exports(user_data, env)
        try:
            instance_id = await self.api.run_instance(
                client_token=name,
                ami_id=ami_id,
                instance_type=instance_type or self.settings.instance_type,
                key_name=key_name,
                root_gb=disk_gb or self.settings.root_gb,
                tags=tags,
                user_data=user_data,
                associate_public_ip=associate_public_ip,
                subnet_id=self.settings.subnet_id,
                security_group_id=security_group_id,
                instance_profile=self.settings.instance_profile,
            )
        except AwsProviderError as exc:
            if key_name:
                await self.api.delete_key_pair(key_name)
            raise ProviderTransportError(str(exc)) from exc

        if self._tailscale_enabled:
            ssh_host = ""
        else:
            try:
                # The public IP literal, never the public DNS name: EC2 public
                # DNS is split-horizon and resolves to the private IP inside
                # the VPC — a source path the managed SG's detected egress /32
                # never matches. Dials to the literal hairpin via the IGW and
                # arrive from the same IP the detector saw.
                ssh_host = await self.api.wait_for_running_with_ip(instance_id)
            except AwsProviderError as exc:
                # No teardown here: the instance exists and is tagged, so the
                # janitor reaps it (and its key pair) via tag-based delete_vm
                # when the errored host expires. The pre-instance failures above
                # delete the key pair inline only because there's no tagged
                # instance for the janitor to find.
                raise ProviderTransportError(str(exc)) from exc

        return VMCreateResult(
            provider_id=instance_id,
            name=name,
            ssh_port=22,
            ssh_host=ssh_host,
            ssh_username=self.settings.ssh_username,
            private_key=private_key,
        )

    async def delete_vm(self, name: str) -> None:
        try:
            instance_id = await self.api.find_instance_id_by_tag_name(
                name, managed_by=self._service_label
            )
            if not instance_id:
                raise ProviderNotFoundError(f"aws VM '{name}' was not found")
            await self.api.terminate_instance(instance_id)
            await self.api.delete_key_pair(f"drukbox-{name}")
        except AwsVMNotFoundError as exc:
            raise ProviderNotFoundError(str(exc)) from exc
        except AwsProviderError as exc:
            raise ProviderTransportError(str(exc)) from exc

    async def aclose(self) -> None:
        return

    async def diagnose(self) -> str:
        identity = await self.api.get_caller_identity()
        return f"account={identity['account']} arn={identity['arn']}"

    async def _resolve_security_group_id(self) -> str:
        if self._cached_sg_id:
            return self._cached_sg_id
        # A managed SG must live in the same VPC as the instance's subnet, or
        # EC2 rejects the launch. With a custom subnet, resolve its VPC; with no
        # subnet, leave it None so AWS places the SG in the default VPC.
        vpc_id: str | None = None
        if self.settings.subnet_id:
            vpc_id = await self.api.resolve_subnet_vpc(self.settings.subnet_id)
        ingress: list[str] = list(self.settings.ssh_cidrs)
        used_fallback = False
        if not ingress:
            if drukbox_ip := await _detect_outbound_ipv4():
                ingress.append(f"{drukbox_ip}/32")
            else:
                # A failed detection must not leave the SG with no SSH
                # ingress at all. Key-only auth still holds.
                logger.warning(
                    "egress IP detection failed; %s SSH ingress falls back to "
                    "0.0.0.0/0 — set AWS_SSH_CIDRS to restrict",
                    _MANAGED_SG_NAME,
                )
                ingress.append("0.0.0.0/0")
                used_fallback = True
        sg_id = await self.api.ensure_managed_security_group(
            name=_MANAGED_SG_NAME,
            description=_MANAGED_SG_DESCRIPTION,
            vpc_id=vpc_id,
            ingress_cidrs=tuple(ingress),
            tags={"managed-by": self._service_label},
        )
        if not used_fallback:
            # Don't cache a world-open fallback: it came from a transient egress
            # detection failure, so leave it re-resolvable. The next create
            # re-detects and ensure_managed_security_group narrows the rule.
            self._cached_sg_id = sg_id
        return sg_id


async def _detect_outbound_ipv4(timeout: float = 5.0) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get("https://checkip.amazonaws.com")
            response.raise_for_status()
    except httpx.HTTPError:
        return None
    try:
        addr = ipaddress.ip_address(response.text.strip())
    except ValueError:
        return None
    if not isinstance(addr, ipaddress.IPv4Address):
        return None
    return str(addr)
