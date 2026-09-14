from core.exceptions import AppException


class ServiceAccountExistsError(AppException):
    status_code = 409
    error_code = "SERVICE_ACCOUNT_EXISTS"
