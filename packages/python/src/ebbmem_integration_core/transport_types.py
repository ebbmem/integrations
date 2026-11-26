"""Transport contracts, capability flags, HTTP response container, and header/error helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ebbmem_integration_core.errors import (
    ApiValidationError,
    AuthError,
    EbbmemApiError,
    IdempotencyConflictError,
    NotFoundError,
    RateLimitError,
    ServerError,
)
from ebbmem_integration_core.reinforce import DEFAULT_REINFORCE_DELTA
from ebbmem_integration_core.retry import parse_retry_after
from ebbmem_integration_core.security import redact_secrets


@dataclass(frozen=True)
class SdkCapabilities:
    """Explicit capability flags for transport/SDK parity with P0-09 & P0-11."""

    tier_on_remember: bool = True
    session_id_on_remember: bool = True
    metadata_on_remember: bool = True
    recall_filters: bool = True
    recall_breakdown_and_id: bool = True
    reinforce_used_ids_and_recall_id: bool = True
    reinforce_default_delta_0_1: bool = True
    deterministic_idempotency_key: bool = True
    exact_key_get: bool = False
    exact_key_delete: bool = False
    metadata_filter_on_list_recall: bool = False
    namespace_prefix_list: bool = False
    notes: tuple[str, ...] = ()


@dataclass
class HttpResponse:
    """Minimal HTTP response container."""

    status: int
    headers: dict[str, str]
    body: Any


@runtime_checkable
class MemoryTransport(Protocol):
    """Synchronous transport contract."""

    capabilities: SdkCapabilities

    def remember(
        self,
        *,
        content: str,
        namespace: str | None = None,
        tier: str = "semantic",
        tags: Sequence[str] | None = None,
        importance: float | None = None,
        valence: float | None = None,
        arousal: float | None = None,
        session_id: str | None = None,
        timestamp_ms: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def recall(
        self,
        *,
        query: str,
        namespace: str | None = None,
        limit: int = 10,
        tier: str | None = None,
        tags: Sequence[str] | None = None,
        session_id: str | None = None,
        profile: str | None = None,
        min_score: float | None = None,
        domain: str | None = None,
        timeout_s: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def reinforce(
        self,
        *,
        memory_id: str,
        used_ids: Sequence[str] = (),
        recall_id: str | None = None,
        delta: float = DEFAULT_REINFORCE_DELTA,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def get(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def forget(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def purge(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def status(
        self,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def health(self) -> dict[str, Any]: ...


@runtime_checkable
class AsyncMemoryTransport(Protocol):
    """Asynchronous transport contract."""

    capabilities: SdkCapabilities

    async def remember(
        self,
        *,
        content: str,
        namespace: str | None = None,
        tier: str = "semantic",
        tags: Sequence[str] | None = None,
        importance: float | None = None,
        valence: float | None = None,
        arousal: float | None = None,
        session_id: str | None = None,
        timestamp_ms: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    async def recall(
        self,
        *,
        query: str,
        namespace: str | None = None,
        limit: int = 10,
        tier: str | None = None,
        tags: Sequence[str] | None = None,
        session_id: str | None = None,
        profile: str | None = None,
        min_score: float | None = None,
        domain: str | None = None,
        timeout_s: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    async def reinforce(
        self,
        *,
        memory_id: str,
        used_ids: Sequence[str] = (),
        recall_id: str | None = None,
        delta: float = DEFAULT_REINFORCE_DELTA,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    async def get(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    async def forget(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    async def purge(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    async def status(
        self,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    async def health(self) -> dict[str, Any]: ...


def _has_header(headers: Mapping[str, str], name: str) -> bool:
    target = name.lower()
    return any(k.lower() == target for k in headers)


def _get_header_ci(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    target = name.lower()
    for k, v in headers.items():
        if k.lower() == target:
            return v
    return None


def _set_header_ci(headers: dict[str, str], name: str, value: str) -> None:
    target = name.lower()
    for k in [k for k in headers if k.lower() == target]:
        del headers[k]
    headers[name] = value


def _setdefault_header_ci(headers: dict[str, str], name: str, value: str) -> None:
    if not _has_header(headers, name):
        headers[name] = value


def _merge_headers_ci(
    base: Mapping[str, str],
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    merged: dict[str, str] = {}
    for k, v in base.items():
        _set_header_ci(merged, k, v)
    if extra:
        for k, v in extra.items():
            _set_header_ci(merged, k, v)
    return merged


def _http_error_from_response(resp: HttpResponse) -> EbbmemApiError:
    body = resp.body if isinstance(resp.body, Mapping) else {}
    code = body.get("code") if isinstance(body.get("code"), str) else None
    if code is None and isinstance(body.get("error"), Mapping):
        nested_code = body["error"].get("code")
        if isinstance(nested_code, str):
            code = nested_code
    detail = body.get("detail") if isinstance(body.get("detail"), str) else None
    retry_after = parse_retry_after(resp.headers)
    msg = redact_secrets(
        f"EbbMem HTTP {resp.status}"
        + (f" [{code}]" if code else "")
        + (f": {detail}" if detail else "")
    )
    kwargs: dict[str, Any] = {
        "status": resp.status,
        "code": code,
        "detail": detail,
        "retry_after": retry_after,
        "body": resp.body,
    }
    if resp.status in {401, 403}:
        return AuthError(msg, **kwargs)
    if resp.status == 404:
        return NotFoundError(msg, **kwargs)
    if resp.status in {409, 410}:
        return IdempotencyConflictError(msg, **kwargs)
    if resp.status == 429:
        return RateLimitError(msg, **kwargs)
    if resp.status in {400, 422}:
        if code and code.startswith("EBB-100-02") and code != "EBB-100-023":
            return IdempotencyConflictError(msg, **kwargs)
        return ApiValidationError(msg, **kwargs)
    if resp.status >= 500:
        return ServerError(msg, **kwargs)
    return EbbmemApiError(msg, **kwargs)
