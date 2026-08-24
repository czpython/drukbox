class DockerProviderError(RuntimeError):
    """Base error for the Docker provider."""


class DockerVMNotFoundError(DockerProviderError):
    """No container matched the lookup."""


class DockerImageNotFoundError(DockerProviderError):
    """No image matched the lookup."""


class DockerTransportError(DockerProviderError):
    """Invoking the docker CLI failed for transport reasons."""
