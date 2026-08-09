"""
Unified error handling for the presentation layer.

Goal
----
Before this, success responses used ``ApiResponse{success,message,data}`` but
errors fell through to FastAPI's default ``{"detail": ...}``.  Clients had to
handle two shapes and guess meaning from the HTTP status.

Here we install global exception handlers so *every* error returns the unified
``ErrorResponse{success:false, code, message, detail?}`` with a stable
``ErrorCode``.  Existing routers need no change: their ``HTTPException``s are
re-wrapped here.  New code can raise :class:`ApiError` to attach a semantic
code directly.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from omnibci.presentation.schemas.common import ErrorCode, ErrorResponse
from omnibci.application.services.command_result_service import (
    CommandExecutionError,
    CommandRejectedError,
    CommandTimeoutError,
)

logger = logging.getLogger(__name__)

# Attach only to routes that actually wait for a correlated worker result.
# Keeping it out of the app-wide response table avoids claiming that read-only
# GET endpoints can fail with a command timeout.
COMMAND_EXECUTION_RESPONSES = {
    504: {"model": ErrorResponse, "description": "Command execution timeout"},
}


# Map HTTP status → semantic ErrorCode (used when an HTTPException carries no
# explicit code).
_STATUS_TO_CODE = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_ERROR,
    500: ErrorCode.INTERNAL_ERROR,
    503: ErrorCode.SERVICE_UNAVAILABLE,
    504: ErrorCode.GATEWAY_TIMEOUT,
}


def status_to_code(status_code: int) -> ErrorCode:
    """Best-effort mapping from HTTP status to a stable ErrorCode."""
    return _STATUS_TO_CODE.get(status_code, ErrorCode.ERROR)


class ApiError(Exception):
    """
    Raise this (instead of HTTPException) when you want to attach a semantic
    error code explicitly, e.g. ``ApiError(409, ErrorCode.CONFLICT, "Engine
    already running")``.  Handled by :func:`register_exception_handlers`.
    """

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        detail=None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail


def command_error_to_api_error(exc: Exception) -> ApiError:
    """Translate correlated command failures into stable HTTP semantics."""
    if isinstance(exc, CommandRejectedError):
        result = exc.result
        return ApiError(
            409,
            ErrorCode.COMMAND_REJECTED,
            str(exc),
            detail={
                "command_id": result.command_id,
                "command": result.command,
                "error_code": result.error_code,
            },
        )
    if isinstance(exc, CommandTimeoutError):
        return ApiError(
            504,
            ErrorCode.COMMAND_TIMEOUT,
            str(exc),
            detail={
                "command_id": exc.command_id,
                "command": exc.command,
                "timeout_seconds": exc.timeout_s,
            },
        )
    if isinstance(exc, CommandExecutionError):
        result = exc.result
        return ApiError(
            500,
            ErrorCode.COMMAND_EXECUTION_FAILED,
            "Command execution failed",
            detail={
                "command_id": result.command_id,
                "command": result.command,
                "error_code": result.error_code,
            },
        )
    raise exc


def _json_error(status_code: int, code: ErrorCode, message: str, detail=None) -> JSONResponse:
    body = ErrorResponse(code=code, message=message, detail=detail)
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers that render every error as a unified ErrorResponse."""

    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _json_error(exc.status_code, exc.code, exc.message, exc.detail)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # exc.detail is usually a human string; if it's structured, keep it in
        # `detail` and use a generic message.
        if isinstance(exc.detail, str):
            message, detail = exc.detail, None
        else:
            message, detail = "Request failed", exc.detail
        return _json_error(exc.status_code, status_to_code(exc.status_code), message, detail)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _json_error(
            422,
            ErrorCode.VALIDATION_ERROR,
            "Request validation failed",
            detail=exc.errors(),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception: %s", exc)
        return _json_error(500, ErrorCode.INTERNAL_ERROR, "Internal server error")
