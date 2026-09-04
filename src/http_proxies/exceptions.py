from core.exceptions import AppException


class HTTPProxyError(AppException):
    status_code = 503
    error_code = "HTTP_PROXY"


class HTTPProxyExistsError(HTTPProxyError):
    status_code = 409
    error_code = "HTTP_PROXY_EXISTS"


class HTTPProxyNotFoundError(HTTPProxyError):
    status_code = 404
    error_code = "HTTP_PROXY_NOT_FOUND"


class HTTPProxyUnsupportedError(HTTPProxyError):
    status_code = 501
    error_code = "HTTP_PROXY_UNSUPPORTED"
