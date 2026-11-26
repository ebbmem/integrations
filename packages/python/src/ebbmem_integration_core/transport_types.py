"""Transport contracts, capability flags, HTTP response container, and header/error helpers."""

from __future__ import annotations

import json
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
    list_memories: bool = False
    episodic_remember: bool = False
    episodic_tail: bool = False
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

    def list_memories(
        self,
        *,
        namespace: str | None = None,
        tier: str | None = None,
        tags: Sequence[str] | None = None,
        tombstoned: bool = False,
        search: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
        min_importance: float | None = None,
        min_valence: float | None = None,
        max_valence: float | None = None,
        metadata_filter: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def episodic_remember(
        self,
        *,
        thread_key: str | None = None,
        session_id: str | None = None,
        content: str,
        role: str = "user",
        tags: Sequence[str] | None = None,
        namespace: str | None = None,
        idempotency_key: str | None = None,
        mcp_profile: str = "full",
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def episodic_tail(
        self,
        *,
        thread_key: str | None = None,
        session_id: str | None = None,
        limit: int = 10,
        namespace: str | None = None,
        mcp_profile: str = "full",
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

    async def list_memories(
        self,
        *,
        namespace: str | None = None,
        tier: str | None = None,
        tags: Sequence[str] | None = None,
        tombstoned: bool = False,
        search: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
        min_importance: float | None = None,
        min_valence: float | None = None,
        max_valence: float | None = None,
        metadata_filter: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    async def episodic_remember(
        self,
        *,
        thread_key: str | None = None,
        session_id: str | None = None,
        content: str,
        role: str = "user",
        tags: Sequence[str] | None = None,
        namespace: str | None = None,
        idempotency_key: str | None = None,
        mcp_profile: str = "full",
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]: ...

    async def episodic_tail(
        self,
        *,
        thread_key: str | None = None,
        session_id: str | None = None,
        limit: int = 10,
        namespace: str | None = None,
        mcp_profile: str = "full",
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
    err_obj = body.get("error") if isinstance(body.get("error"), Mapping) else None
    if code is None and err_obj is not None:
        nested_code = err_obj.get("code")
        if isinstance(nested_code, str):
            code = nested_code
        elif isinstance(err_obj.get("data"), Mapping):
            data_code = err_obj["data"].get("code")
            if isinstance(data_code, str):
                code = data_code
    detail = body.get("detail") if isinstance(body.get("detail"), str) else None
    if detail is None and err_obj is not None and isinstance(err_obj.get("message"), str):
        detail = err_obj.get("message")
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


def _mcp_error_from_payload(
    status: int,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
) -> EbbmemApiError:
    err_obj = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
    rpc_code = err_obj.get("code")
    effective_status = status
    if effective_status < 400:
        if rpc_code == -32003:
            effective_status = 403
        elif rpc_code == -32004 or rpc_code == -32601:
            effective_status = 404
        elif rpc_code == -32602:
            effective_status = 422
        else:
            effective_status = 400
    return _http_error_from_response(
        HttpResponse(status=effective_status, headers=dict(headers), body=dict(payload))
    )


def _extract_mcp_result_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    result = payload.get("result") if isinstance(payload.get("result"), Mapping) else payload
    for key in ("structuredContent", "structured_content"):
        structured = result.get(key)
        if isinstance(structured, Mapping):
            return dict(structured)
    content = result.get("content")
    if isinstance(content, Sequence) and not isinstance(content, str | bytes):
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    try:
                        parsed = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, Mapping):
                        return dict(parsed)
    return dict(result) if isinstance(result, Mapping) else {}
