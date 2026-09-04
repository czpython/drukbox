from providers.aws.provider import AWSProvider
from providers.capabilities import ReverseTunnelCapability, SecretProxyRoutingCapability
from providers.docker.provider import DockerProvider
from providers.docker_sbx.provider import DockerSbxProvider
from providers.exe.provider import ExeProvider
from providers.exoscale.provider import ExoscaleProvider
from providers.hetzner.provider import HetznerProvider


def test_each_provider_with_a_dialable_ssh_host_supports_reverse_tunnels():
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

    assert supported == {"aws", "docker", "exe", "exoscale", "hetzner"}


def test_every_provider_routes_secret_traffic_through_the_proxy():
    provider_types = (
        AWSProvider,
        DockerProvider,
        DockerSbxProvider,
        ExeProvider,
        ExoscaleProvider,
        HetznerProvider,
    )

    assert all(
        issubclass(provider_type, SecretProxyRoutingCapability) for provider_type in provider_types
    )
