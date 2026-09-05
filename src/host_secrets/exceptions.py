from core.exceptions import AppException


class UnknownSecretServiceError(AppException):
    status_code = 422
    error_code = "UNKNOWN_SECRET_SERVICE"


class SecretDeliveryUnsupportedError(AppException):
    status_code = 409
    error_code = "SECRET_DELIVERY_UNSUPPORTED"
