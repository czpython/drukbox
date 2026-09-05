from core.exceptions import AppException


class UnknownSecretServiceError(AppException):
    status_code = 422
    error_code = "UNKNOWN_SECRET_SERVICE"
