class ReverseTunnelError(RuntimeError):
    """A box reverse tunnel could not start or stay connected."""


class SecretProxyError(RuntimeError):
    """Base error for proxy control and request failures."""


class SecretProxyUnavailableError(SecretProxyError):
    """The proxy control path is unavailable or returned invalid data."""


class SecretProxyRejectedError(SecretProxyError):
    """The proxy rejected an invalid or unauthorized operation."""
