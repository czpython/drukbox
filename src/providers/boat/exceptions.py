from providers.exceptions import (
    ProviderAuthError,
    ProviderCommandError,
    ProviderError,
    ProviderNotFoundError,
    ProviderTransportError,
)


class BoatError(ProviderError): ...


class BoatAuthError(ProviderAuthError, BoatError): ...


class BoatCommandError(ProviderCommandError, BoatError): ...


class BoatNotFoundError(ProviderNotFoundError, BoatError): ...


class BoatTransportError(ProviderTransportError, BoatError): ...
