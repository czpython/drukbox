class DockerSbxProviderError(RuntimeError):
    """Base error for the Docker Sandboxes provider."""


class DockerSbxNotFoundError(DockerSbxProviderError):
    """The sandbox was not found."""


class DockerSbxTransportError(DockerSbxProviderError):
    """The sbx command failed because of a transport problem."""
