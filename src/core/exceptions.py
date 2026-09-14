from typing import ClassVar


class AppException(RuntimeError):
    """Base for application-level exceptions that map to HTTP responses.

    Subclasses set `status_code` (and optionally `error_code`) as class
    attributes; instances carry the human-readable detail as the exception
    message. A single FastAPI exception handler converts any AppException
    into a JSONResponse with the right status.
    """

    status_code: ClassVar[int] = 500
    error_code: ClassVar[str | None] = None
    headers: ClassVar[dict[str, str]] = {}

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ResourceNotFoundError(AppException):
    status_code = 404
    error_code = "NOT_FOUND"
