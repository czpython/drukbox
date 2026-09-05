from core.exceptions import AppException


class SecretsExchangeNotConfiguredError(AppException):
    status_code = 409
    error_code = "SECRETS_EXCHANGE_NOT_CONFIGURED"
