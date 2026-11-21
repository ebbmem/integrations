"""Exception hierarchy for ebbmem-integration-core."""

from __future__ import annotations

from typing import Any


def _redact(text: str) -> str:
    from ebbmem_integration_core.security import redact_secrets

    return redact_secrets(text)


def _redact_payload(value: Any) -> Any:
    from ebbmem_integration_core.security import redact_payload

    return redact_payload(value)


class IntegrationCoreError(Exception):
    """Base exception for ebbmem-integration-core with automatic secret redaction."""

    def __init__(self, message: str = "") -> None:
        super().__init__(_redact(message) if message else "")

    def __str__(self) -> str:
        return _redact(super().__str__())

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self)!r})"


class CredentialInUrlError(IntegrationCoreError, ValueError):
    """Raised when a base URL or request target embeds credentials."""


class CredentialConflictError(IntegrationCoreError, ValueError):
    """Raised when both bearer_token and api_key are supplied simultaneously."""


class ValidationError(IntegrationCoreError, ValueError):
    """Raised on invalid client-side inputs."""


class NamespaceError(ValidationError):
    """Raised when a namespace is malformed or conflicts between header and body."""


class ReservedMetadataError(ValidationError):
    """Raised when metadata contains reserved 'ebb.*' keys."""


class UnsupportedCapabilityError(IntegrationCoreError, NotImplementedError):
    """Raised when a wrapped SDK lacks a requested P0-09 or P0-11 capability."""


class EbbmemApiError(IntegrationCoreError):
    """HTTP or transport error returned while communicating with EbbMem."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        detail: str | None = None,
        retry_after: float | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = _redact(detail) if isinstance(detail, str) else detail
        self.retry_after = retry_after
        self.body = _redact_payload(body) if body is not None else None

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self)!r}, status={self.status!r}, code={self.code!r})"


class AuthError(EbbmemApiError):
    """Authentication or authorization failure (HTTP 401/403)."""


class NotFoundError(EbbmemApiError):
    """Uniform 404 resource/namespace isolation error."""


class ApiValidationError(EbbmemApiError):
    """Server-side request validation error (HTTP 400/422)."""


class RateLimitError(EbbmemApiError):
    """Rate limit exceeded error (HTTP 429)."""


class IdempotencyConflictError(EbbmemApiError):
    """Idempotency conflict or target purged error (HTTP 409/410/422)."""


class ServerError(EbbmemApiError):
    """Upstream 5xx server error."""


class TransportError(EbbmemApiError):
    """Network or connection failure."""


class RequestTimeoutError(TransportError, TimeoutError):
    """Request timed out before a response was received."""
