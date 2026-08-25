from collections.abc import Iterator

import pytest

from providers import registry as registry_module
from providers.base import VMCreateResult, VMProvider
from providers.capabilities import TemplateCapability
from providers.derived_image import derived_image_tag
from providers.exceptions import ProviderError


class StubTemplateProvider(TemplateCapability, VMProvider):
    name = "template-stub"
    diagnose_hint = "check_template_stub"

    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []
        self.deleted: list[str] = []
        self.build_error: Exception | None = None
        self.delete_error: ProviderError | None = None

    @classmethod
    def from_settings(cls) -> "StubTemplateProvider":
        return cls()

    @property
    def default_image(self) -> str:
        return "stub:base"

    @property
    def bootstrap_ssh_timeout_seconds(self) -> float:
        return 0.1

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
        return VMCreateResult(provider_id=name, name=name, ssh_port=22, ssh_username="stub")

    async def delete_vm(self, name: str) -> None:
        return

    async def diagnose(self) -> str:
        return "stub ok"

    async def aclose(self) -> None:
        return

    async def create_template(
        self,
        *,
        base_image: str,
        setup_script: str,
        label: str,
    ) -> str:
        self.created.append((base_image, setup_script, label))
        if self.build_error:
            raise self.build_error
        return derived_image_tag(base_image=base_image, setup_script=setup_script)

    async def delete_template(self, handle: str) -> None:
        self.deleted.append(handle)
        if self.delete_error:
            raise self.delete_error


@pytest.fixture
def template_provider() -> Iterator[StubTemplateProvider]:
    factories = dict(registry_module._factories)
    instances = dict(registry_module._instances)
    provider = StubTemplateProvider()
    registry_module._factories[provider.name] = lambda: provider
    try:
        yield provider
    finally:
        registry_module._factories.clear()
        registry_module._factories.update(factories)
        registry_module._instances.clear()
        registry_module._instances.update(instances)
