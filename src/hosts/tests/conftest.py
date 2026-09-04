from collections.abc import Iterator

import pytest

from providers import registry as registry_module
from providers.base import VMCreateResult, VMProvider


class StubVMProvider(VMProvider):
    """A registry-installable provider for exercising per-call selection
    without a real backend. Records teardown so tests can assert delete
    routed to the host's own provider."""

    name = "stub"
    diagnose_hint = "check_stub"
    supports_instance_type = True
    supports_disk_gb = True

    def __init__(self) -> None:
        self.deleted: list[str] = []

    @classmethod
    def from_settings(cls) -> "StubVMProvider":
        return cls()

    @property
    def default_image(self) -> str:
        return "stub:image"

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
        self.deleted.append(name)

    async def diagnose(self) -> str:
        return "stub ok"

    async def aclose(self) -> None:
        return


@pytest.fixture
def stub_provider() -> Iterator[StubVMProvider]:
    factories = dict(registry_module._factories)
    instances = dict(registry_module._instances)
    stub = StubVMProvider()
    registry_module._factories["stub"] = lambda: stub
    try:
        yield stub
    finally:
        registry_module._factories.clear()
        registry_module._factories.update(factories)
        registry_module._instances.clear()
        registry_module._instances.update(instances)
