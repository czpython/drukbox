from core.exceptions import AppException


class SecretsProxyNotConfiguredError(AppException):
    status_code = 409
    error_code = "SECRETS_PROXY_NOT_CONFIGURED"
