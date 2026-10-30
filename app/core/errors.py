"""Domain exceptions mapped to structured HTTP errors."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: Any = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details
        if code:
            self.code = code


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Unauthenticated(AppError):
    status_code = 401
    code = "unauthenticated"


class PermissionDenied(AppError):
    status_code = 403
    code = "permission_denied"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class QuotaExceeded(AppError):
    status_code = 402
    code = "quota_exceeded"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class ServiceUnavailable(AppError):
    """A required external provider is not configured or unreachable."""

    status_code = 503
    code = "service_unavailable"


class InvalidTransition(AppError):
    status_code = 409
    code = "invalid_transition"
