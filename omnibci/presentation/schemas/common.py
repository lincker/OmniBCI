"""
Shared response envelope used by every endpoint.
"""
from enum import Enum
from typing import Any, Generic, Optional, TypeVar
from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    success: bool = True
    message: str = "ok"
    data: Optional[T] = None


class ErrorCode(str, Enum):
    """
    Stable, machine-readable error codes for clients to branch on instead of
    guessing from the HTTP status.  The string value is the wire format.
    """
    # Generic HTTP-class codes (fallback for un-coded HTTPException / validation)
    BAD_REQUEST = "BAD_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    GATEWAY_TIMEOUT = "GATEWAY_TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    ERROR = "ERROR"  # generic fallback

    # Domain-specific codes (raised explicitly by routers via ApiError) so the
    # frontend can branch on the *reason*, not just the HTTP status class.
    ENGINE_NOT_READY = "ENGINE_NOT_READY"        # engine/processor not initialised
    COMMAND_REJECTED = "COMMAND_REJECTED"        # state/input precondition rejected
    COMMAND_EXECUTION_FAILED = "COMMAND_EXECUTION_FAILED"  # device/plugin/internal fault
    COMMAND_TIMEOUT = "COMMAND_TIMEOUT"          # no terminal consumer result in time

    # Host configuration and Plugin Profiles.
    CONFIG_KEY_NOT_ALLOWED = "CONFIG_KEY_NOT_ALLOWED"      # key outside the whitelist
    CONFIG_PROFILE_NOT_FOUND = "CONFIG_PROFILE_NOT_FOUND"  # unknown Plugin Profile


class ErrorResponse(BaseModel):
    """
    Unified error envelope — mirrors ApiResponse so clients see one consistent
    shape across success and failure (`success` discriminates).
    """
    success: bool = False
    code: ErrorCode = ErrorCode.ERROR
    message: str
    detail: Optional[Any] = None
