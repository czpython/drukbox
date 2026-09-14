from typing import ClassVar

from core.exceptions import AppException


class SecretsProxyNotConfiguredError(AppException):
    status_code = 409
    error_code = "SECRETS_PROXY_NOT_CONFIGURED"


class SecretStaticError(AppException):
    status_code = 409
    error_code = "SECRET_STATIC"


class SecretRefreshError(AppException):
    status_code = 503
    error_code = "SECRET_REFRESH"
    headers: ClassVar[dict[str, str]] = {"Retry-After": "5"}
