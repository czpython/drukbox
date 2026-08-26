from core.exceptions import AppException


class UnknownTemplateError(AppException):
    status_code = 400
    error_code = "UNKNOWN_TEMPLATE"


class TemplateNotAvailableError(AppException):
    status_code = 409
    error_code = "TEMPLATE_NOT_AVAILABLE"


class TemplateStateError(AppException):
    status_code = 409
    error_code = "TEMPLATE_STATE"


class TemplateTeardownError(AppException):
    status_code = 503
    error_code = "TEMPLATE_TEARDOWN"
