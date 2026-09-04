import pytest

from core import settings as settings_module
from providers import registry as registry_module
from providers.base import VMCreateResult, VMProvider
from providers.exceptions import UnknownProviderError
from providers.registry import (
    get_default_vm_provider,
    get_vm_provider,
    register_vm_provider,
)


class StubVMProvider(VMProvider):
    name = "stub"
    diagnose_hint = "check_stub"

    @classmethod
    def from_settings(cls) -> "StubVMProvider":
        return cls()

    @property
    def default_image(self) -> str:
        return "stub:image"

    @property
    def bootstrap_ssh_timeout_seconds(self) -> float:
        return 30.0

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


@pytest.fixture(autouse=True)
def _reset_registry():
    factories_snapshot = dict(registry_module._factories)
    instances_snapshot = dict(registry_module._instances)
    try:
        yield
    finally:
        registry_module._factories.clear()
        registry_module._factories.update(factories_snapshot)
        registry_module._instances.clear()
        registry_module._instances.update(instances_snapshot)


def test_get_vm_provider_returns_registered_factory() -> None:
    register_vm_provider(StubVMProvider)
    provider = get_vm_provider("stub")
    assert isinstance(provider, StubVMProvider)


def test_get_vm_provider_caches_instances() -> None:
    register_vm_provider(StubVMProvider)
    first = get_vm_provider("stub")
    second = get_vm_provider("stub")
    assert first is second


def test_get_vm_provider_unknown_name_raises() -> None:
    with pytest.raises(UnknownProviderError):
        get_vm_provider("does-not-exist")


def test_get_default_vm_provider_uses_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_module.get_settings.cache_clear()
    monkeypatch.setenv("DEFAULT_HOST_PROVIDER", "stub")
    register_vm_provider(StubVMProvider)
    try:
        provider = get_default_vm_provider()
        assert isinstance(provider, StubVMProvider)
    finally:
        settings_module.get_settings.cache_clear()
