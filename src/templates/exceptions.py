from core.exceptions import AppException


class TemplateStateError(AppException):
    status_code = 409
    error_code = "TEMPLATE_STATE"


class TemplateTeardownError(AppException):
    status_code = 503
    error_code = "TEMPLATE_TEARDOWN"
