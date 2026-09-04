from providers.aws.provider import AWSProvider
from providers.capabilities import ReverseTunnelCapability
from providers.docker.provider import DockerProvider
from providers.docker_sbx.provider import DockerSbxProvider
from providers.exe.provider import ExeProvider
from providers.exoscale.provider import ExoscaleProvider
from providers.hetzner.provider import HetznerProvider


def test_only_bare_providers_support_reverse_tunnels():
    provider_types = (
        AWSProvider,
        DockerProvider,
        DockerSbxProvider,
        ExeProvider,
        ExoscaleProvider,
        HetznerProvider,
    )

    supported = {
        provider_type.name
        for provider_type in provider_types
        if issubclass(provider_type, ReverseTunnelCapability)
    }

    assert supported == {"aws", "docker", "exoscale", "hetzner"}
