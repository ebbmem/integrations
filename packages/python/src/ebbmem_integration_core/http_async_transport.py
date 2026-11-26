"""Zero-dependency Python stdlib async HTTP transport for IC-1."""

from __future__ import annotations

import asyncio
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any

from ebbmem_integration_core.errors import RequestTimeoutError, TransportError
from ebbmem_integration_core.http_transport import StdlibHttpTransport
from ebbmem_integration_core.reinforce import DEFAULT_REINFORCE_DELTA, validate_delta
from ebbmem_integration_core.retry import (
    RETRYABLE_STATUS_CODES,
    RetryPolicy,
    is_idempotent_request,
    parse_retry_after,
    validate_idempotency_key,
)
from ebbmem_integration_core.security import TELEMETRY_ENABLED_DEFAULT
from ebbmem_integration_core.transport_types import (
    HttpResponse,
    _http_error_from_response,
    _setdefault_header_ci,
)


class AsyncStdlibHttpTransport:
    """Async counterpart to StdlibHttpTransport with non-blocking executor dispatch."""

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str | None = None,
        api_key: str | None = None,
        retry_policy: RetryPolicy | None = None,
        telemetry_enabled: bool = TELEMETRY_ENABLED_DEFAULT,
        log_sink: list[str] | None = None,
        user_agent: str = "ebbmem-integration-core/0.1.0",
    ) -> None:
        self._sync = StdlibHttpTransport(
            base_url,
            bearer_token=bearer_token,
            api_key=api_key,
            retry_policy=retry_policy,
            telemetry_enabled=telemetry_enabled,
            log_sink=log_sink,
            user_agent=user_agent,
        )
        self.base_url = self._sync.base_url
        self.retry_policy = self._sync.retry_policy
        self.telemetry_enabled = self._sync.telemetry_enabled
        self.log_sink = self._sync.log_sink
        self.capabilities = self._sync.capabilities

    def __repr__(self) -> str:
        return f"AsyncStdlibHttpTransport(base_url={self.base_url!r})"

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> HttpResponse:
        return await asyncio.to_thread(
            self._sync.request,
            method,
            path,
            json_body=json_body,
            headers=headers,
            query=query,
            timeout_s=timeout_s,
        )

    async def request_with_retry(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> HttpResponse:
        policy = retry_policy if retry_policy is not None else self.retry_policy
        can_retry = is_idempotent_request(method, path, headers=headers, json_body=json_body)
        max_attempts = (1 + policy.max_retries) if can_retry else 1

        last_resp: HttpResponse | None = None
        for attempt in range(max_attempts):
            try:
                resp = await self.request(
                    method,
                    path,
                    json_body=json_body,
                    headers=headers,
                    query=query,
                    timeout_s=timeout_s,
                )
            except (RequestTimeoutError, TransportError):
                if not can_retry or attempt + 1 >= max_attempts:
                    raise
                await policy.async_sleep(policy.delay_for_attempt(attempt))
                continue

            last_resp = resp
            if resp.status not in RETRYABLE_STATUS_CODES or attempt + 1 >= max_attempts:
                return resp

            retry_after = parse_retry_after(resp.headers) if resp.status == 429 else None
            await policy.async_sleep(policy.delay_for_attempt(attempt, retry_after_s=retry_after))

        assert last_resp is not None
        return last_resp

    async def _call_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        resp = await self.request_with_retry(
            method,
            path,
            json_body=json_body,
            headers=headers,
            query=query,
            timeout_s=timeout_s,
        )
        if resp.status >= 400:
            raise _http_error_from_response(resp)
        if isinstance(resp.body, dict):
            return resp.body
        return {}

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
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"content": content, "tier": tier}
        if namespace is not None:
            body["namespace"] = namespace
        if tags is not None:
            body["tags"] = list(tags)
        if importance is not None:
            body["importance"] = importance
        if valence is not None:
            body["valence"] = valence
        if arousal is not None:
            body["arousal"] = arousal
        if session_id is not None:
            body["session_id"] = session_id
        if timestamp_ms is not None:
            body["timestamp_ms"] = timestamp_ms
        if metadata is not None:
            body["metadata"] = dict(metadata)

        req_headers = dict(headers or {})
        if namespace is not None:
            _setdefault_header_ci(req_headers, "X-Ebb-Namespace", namespace)
        if idempotency_key is not None:
            validate_idempotency_key(idempotency_key)
            body["idempotency_key"] = idempotency_key
            _setdefault_header_ci(req_headers, "Idempotency-Key", idempotency_key)

        return await self._call_json(
            "POST",
            "/api/v1/memory/remember",
            json_body=body,
            headers=req_headers,
        )

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
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query, "limit": limit, "reinforce": False}
        if namespace is not None:
            body["namespace"] = namespace
        if tier is not None:
            body["tier"] = tier
        if tags:
            body["tags"] = list(tags)
        if session_id is not None:
            body["session_id"] = session_id
        if profile is not None:
            body["profile"] = profile
        if min_score is not None:
            body["min_score"] = min_score
        if domain is not None:
            body["domain"] = domain

        req_headers = dict(headers or {})
        if namespace is not None:
            _setdefault_header_ci(req_headers, "X-Ebb-Namespace", namespace)

        return await self._call_json(
            "POST",
            "/api/v1/memory/recall",
            json_body=body,
            headers=req_headers,
            timeout_s=timeout_s,
        )

    async def reinforce(
        self,
        *,
        memory_id: str,
        used_ids: Sequence[str] = (),
        recall_id: str | None = None,
        delta: float = DEFAULT_REINFORCE_DELTA,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        validated_delta = validate_delta(delta)
        body: dict[str, Any] = {
            "memory_id": memory_id,
            "delta": validated_delta,
        }
        if used_ids:
            body["used_ids"] = list(used_ids)
        if recall_id is not None:
            body["recall_id"] = recall_id
        if namespace is not None:
            body["namespace"] = namespace

        req_headers = dict(headers or {})
        if namespace is not None:
            _setdefault_header_ci(req_headers, "X-Ebb-Namespace", namespace)

        return await self._call_json(
            "POST",
            "/api/v1/memory/reinforce",
            json_body=body,
            headers=req_headers,
        )

    async def get(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        req_headers = dict(headers or {})
        query: dict[str, str] | None = None
        if namespace is not None:
            _setdefault_header_ci(req_headers, "X-Ebb-Namespace", namespace)
            query = {"namespace": namespace}
        encoded_id = urllib.parse.quote(memory_id, safe="")
        return await self._call_json(
            "GET",
            f"/api/v1/memory/{encoded_id}",
            headers=req_headers,
            query=query,
        )

    async def forget(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        req_headers = dict(headers or {})
        query: dict[str, str] | None = None
        if namespace is not None:
            _setdefault_header_ci(req_headers, "X-Ebb-Namespace", namespace)
            query = {"namespace": namespace}
        encoded_id = urllib.parse.quote(memory_id, safe="")
        return await self._call_json(
            "DELETE",
            f"/api/v1/memory/{encoded_id}",
            headers=req_headers,
            query=query,
        )

    async def purge(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        req_headers = dict(headers or {})
        query: dict[str, str] | None = None
        if namespace is not None:
            _setdefault_header_ci(req_headers, "X-Ebb-Namespace", namespace)
            query = {"namespace": namespace}
        encoded_id = urllib.parse.quote(memory_id, safe="")
        return await self._call_json(
            "DELETE",
            f"/api/v1/memory/{encoded_id}/purge",
            headers=req_headers,
            query=query,
        )

    async def status(
        self,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        req_headers = dict(headers or {})
        query: dict[str, str] | None = None
        if namespace is not None:
            _setdefault_header_ci(req_headers, "X-Ebb-Namespace", namespace)
            query = {"namespace": namespace}
        return await self._call_json(
            "GET",
            "/api/v1/memory/status",
            headers=req_headers,
            query=query,
        )

    async def health(self) -> dict[str, Any]:
        return await self._call_json("GET", "/healthz")
