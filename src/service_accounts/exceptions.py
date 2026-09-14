from core.exceptions import AppException


class ServiceAccountExistsError(AppException):
    status_code = 409
    error_code = "SERVICE_ACCOUNT_EXISTS"


class ServiceAccountStateError(AppException):
    status_code = 409
    error_code = "SERVICE_ACCOUNT_STATE"


class ServiceAccountTokenRejectedError(AppException):
    status_code = 403
    error_code = "SERVICE_ACCOUNT_TOKEN_REJECTED"
