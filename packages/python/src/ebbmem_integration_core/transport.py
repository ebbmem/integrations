"""HTTP stdlib transport (sync & async) and released-SDK adapter for ebbmem-client 0.2.0."""

from __future__ import annotations

import asyncio
import http.client
import inspect
import json
import types
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Protocol, Union, get_args, get_origin, runtime_checkable

from ebbmem_integration_core.errors import (
    ApiValidationError,
    AuthError,
    EbbmemApiError,
    IdempotencyConflictError,
    NotFoundError,
    RateLimitError,
    RequestTimeoutError,
    ServerError,
    TransportError,
    UnsupportedCapabilityError,
    ValidationError,
)
from ebbmem_integration_core.reinforce import DEFAULT_REINFORCE_DELTA, validate_delta
from ebbmem_integration_core.retry import (
    RETRYABLE_STATUS_CODES,
    RetryPolicy,
    is_idempotent_request,
    parse_retry_after,
    validate_idempotency_key,
)
from ebbmem_integration_core.security import (
    TELEMETRY_ENABLED_DEFAULT,
    redact_payload,
    redact_secrets,
    validate_auth_credentials,
    validate_base_url,
    validate_request_target,
)


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


class StdlibHttpTransport:
    """Zero-dependency Python stdlib HTTP transport implementing full IC-1 REST semantics."""

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
        self.base_url = validate_base_url(base_url)
        self._auth_headers = validate_auth_credentials(
            bearer_token=bearer_token,
            api_key=api_key,
        )
        self.retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        self.telemetry_enabled = bool(telemetry_enabled)
        self.log_sink: list[str] = log_sink if log_sink is not None else []
        self.user_agent = user_agent
        self.capabilities = SdkCapabilities(
            exact_key_get=False,
            exact_key_delete=False,
            metadata_filter_on_list_recall=False,
            namespace_prefix_list=False,
            notes=(
                "Built-in stdlib HTTP transport supports full IC-1 REST verbs; "
                "P0-09 exact-key lookup/delete, metadata filters, and namespace listing "
                "are not yet enabled on the base transport.",
            ),
        )

    def __repr__(self) -> str:
        auth_mode = (
            "bearer"
            if "Authorization" in self._auth_headers
            else ("api_key" if "X-API-Key" in self._auth_headers else "none")
        )
        return f"StdlibHttpTransport(base_url={self.base_url!r}, auth={auth_mode!r})"

    def __str__(self) -> str:
        return self.__repr__()

    def _log(self, message: str) -> None:
        self.log_sink.append(redact_secrets(message))

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> HttpResponse:
        validate_request_target(path, query=query)
        parsed_path = urllib.parse.urlsplit(path)
        url = f"{self.base_url}{path}"
        if query:
            sep = "&" if (parsed_path.query or "?" in path) else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(query)}"

        req_headers = _merge_headers_ci(
            {
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                **self._auth_headers,
            },
            headers,
        )

        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            _setdefault_header_ci(req_headers, "Content-Type", "application/json")

        req = urllib.request.Request(
            url=url,
            data=data,
            headers=req_headers,
            method=method.upper(),
        )
        effective_timeout = self.retry_policy.timeout_s if timeout_s is None else timeout_s
        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                raw = resp.read().decode("utf-8")
                parsed = json.loads(raw) if raw else None
                resp_headers = {k: v for k, v in resp.headers.items()}
                self._log(f"{method.upper()} {path} -> {resp.status}")
                return HttpResponse(status=resp.status, headers=resp_headers, body=parsed)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except TimeoutError as read_exc:
                self._log(f"{method.upper()} {path} -> timeout")
                raise RequestTimeoutError("Request timed out reading error body") from read_exc
            except (OSError, http.client.HTTPException) as read_exc:
                self._log(
                    f"{method.upper()} {path} -> connection_error ({type(read_exc).__name__})"
                )
                raise TransportError(
                    redact_secrets(f"Transport error ({type(read_exc).__name__}): {read_exc}")
                ) from read_exc
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = {"raw": redact_secrets(raw)}
            resp_headers = {k: v for k, v in exc.headers.items()}
            self._log(f"{method.upper()} {path} -> {exc.code}")
            return HttpResponse(status=exc.code, headers=resp_headers, body=parsed)
        except TimeoutError as exc:
            self._log(f"{method.upper()} {path} -> timeout")
            raise RequestTimeoutError(f"Request timed out: {type(exc).__name__}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                self._log(f"{method.upper()} {path} -> timeout")
                raise RequestTimeoutError("Request timed out") from exc
            self._log(f"{method.upper()} {path} -> connection_error ({type(reason).__name__})")
            raise TransportError(redact_secrets(f"Transport connection error: {reason}")) from exc
        except (OSError, http.client.HTTPException) as exc:
            self._log(f"{method.upper()} {path} -> connection_error ({type(exc).__name__})")
            raise TransportError(
                redact_secrets(f"Transport error ({type(exc).__name__}): {exc}")
            ) from exc

    def request_with_retry(
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
                resp = self.request(
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
                policy.sleep(policy.delay_for_attempt(attempt))
                continue

            last_resp = resp
            if resp.status not in RETRYABLE_STATUS_CODES or attempt + 1 >= max_attempts:
                return resp

            retry_after = parse_retry_after(resp.headers) if resp.status == 429 else None
            policy.sleep(policy.delay_for_attempt(attempt, retry_after_s=retry_after))

        assert last_resp is not None
        return last_resp

    def _call_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        resp = self.request_with_retry(
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

        return self._call_json(
            "POST",
            "/api/v1/memory/remember",
            json_body=body,
            headers=req_headers,
        )

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

        return self._call_json(
            "POST",
            "/api/v1/memory/recall",
            json_body=body,
            headers=req_headers,
            timeout_s=timeout_s,
        )

    def reinforce(
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

        return self._call_json(
            "POST",
            "/api/v1/memory/reinforce",
            json_body=body,
            headers=req_headers,
        )

    def get(
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
        return self._call_json(
            "GET",
            f"/api/v1/memory/{encoded_id}",
            headers=req_headers,
            query=query,
        )

    def forget(
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
        return self._call_json(
            "DELETE",
            f"/api/v1/memory/{encoded_id}",
            headers=req_headers,
            query=query,
        )

    def purge(
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
        return self._call_json(
            "DELETE",
            f"/api/v1/memory/{encoded_id}/purge",
            headers=req_headers,
            query=query,
        )

    def status(
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
        return self._call_json(
            "GET",
            "/api/v1/memory/status",
            headers=req_headers,
            query=query,
        )

    def health(self) -> dict[str, Any]:
        return self._call_json("GET", "/healthz")


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


def _to_plain_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, Mapping):
        return dict(obj)
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        res = to_dict()
        if isinstance(res, Mapping):
            return dict(res)
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        res = model_dump()
        if isinstance(res, Mapping):
            return dict(res)
    attrs = getattr(obj, "__dict__", None)
    if isinstance(attrs, dict):
        return {k: v for k, v in attrs.items() if not k.startswith("_")}
    return {}


def _validate_sdk_config(sdk_client: Any) -> None:
    config = getattr(sdk_client, "config", None)
    if config is None:
        return
    base_url = getattr(config, "base_url", None)
    if isinstance(base_url, str) and base_url:
        validate_base_url(base_url)
    token = getattr(config, "token", None)
    api_key = getattr(config, "api_key", None)
    validate_auth_credentials(bearer_token=token, api_key=api_key)


def _parse_sdk_error_body(raw_body: Any) -> Any:
    if raw_body is None:
        return None
    if isinstance(raw_body, Mapping):
        return dict(raw_body)
    if isinstance(raw_body, bytes):
        text = raw_body.decode("utf-8", errors="replace")
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if isinstance(raw_body, str):
        if not raw_body.strip():
            return None
        try:
            return json.loads(raw_body)
        except json.JSONDecodeError:
            return raw_body
    return raw_body


def _translate_sdk_exception(exc: Exception) -> EbbmemApiError:
    status: int | None = None
    code: str | None = None
    raw_body: Any = None
    retry_after: float | None = None
    headers: Mapping[str, Any] | None = None
    saw_timeout = False
    saw_connection = False

    cur: BaseException | None = exc
    seen: set[int] = set()
    for _ in range(16):
        if cur is None or id(cur) in seen:
            break
        seen.add(id(cur))
        if isinstance(cur, TimeoutError):
            saw_timeout = True
        if "ConnectionError" in type(cur).__name__:
            saw_connection = True
        if status is None:
            cur_status = getattr(cur, "status", None)
            if isinstance(cur_status, int) and not isinstance(cur_status, bool):
                status = cur_status
        if code is None:
            cur_code = getattr(cur, "code", None)
            if isinstance(cur_code, str) and cur_code:
                code = cur_code
        if raw_body is None:
            cur_body = getattr(cur, "body", None)
            if cur_body is not None:
                raw_body = cur_body
        if retry_after is None:
            cur_retry = getattr(cur, "retry_after", None)
            if isinstance(cur_retry, int | float) and not isinstance(cur_retry, bool):
                retry_after = float(cur_retry)
        if headers is None:
            cur_headers = getattr(cur, "headers", None)
            if isinstance(cur_headers, Mapping):
                headers = cur_headers
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)

    parsed_body = _parse_sdk_error_body(raw_body)
    redacted_body = redact_payload(parsed_body) if parsed_body is not None else None
    detail: str | None = None
    if isinstance(parsed_body, Mapping):
        if code is None:
            body_code = parsed_body.get("code")
            if isinstance(body_code, str) and body_code:
                code = body_code
            else:
                nested_err = parsed_body.get("error")
                if isinstance(nested_err, Mapping):
                    nested_code = nested_err.get("code")
                    if isinstance(nested_code, str) and nested_code:
                        code = nested_code
        raw_detail = parsed_body.get("detail")
        if isinstance(raw_detail, str) and raw_detail:
            detail = raw_detail
        elif isinstance(parsed_body.get("message"), str) and parsed_body.get("message"):
            detail = str(parsed_body["message"])
        elif isinstance(parsed_body.get("error"), Mapping):
            err_msg = parsed_body["error"].get("message")
            if isinstance(err_msg, str) and err_msg:
                detail = err_msg

    if retry_after is None and headers is not None:
        retry_after = parse_retry_after({str(k): str(v) for k, v in headers.items()})

    raw_msg = str(exc).strip()
    if not raw_msg and detail:
        raw_msg = detail
    message = redact_secrets(raw_msg)

    if isinstance(exc, TimeoutError) or (status is None and saw_timeout):
        return RequestTimeoutError(message or "SDK request timed out", body=redacted_body)
    if status in {401, 403}:
        return AuthError(message, status=status, code=code, detail=detail, body=redacted_body)
    if status == 404:
        return NotFoundError(message, status=status, code=code, detail=detail, body=redacted_body)
    if status in {409, 410}:
        return IdempotencyConflictError(
            message, status=status, code=code, detail=detail, body=redacted_body
        )
    if status == 429:
        return RateLimitError(
            message,
            status=status,
            code=code,
            detail=detail,
            retry_after=retry_after,
            body=redacted_body,
        )
    if status in {400, 422}:
        if isinstance(code, str) and code.startswith("EBB-100-02") and code != "EBB-100-023":
            return IdempotencyConflictError(
                message, status=status, code=code, detail=detail, body=redacted_body
            )
        return ApiValidationError(
            message, status=status, code=code, detail=detail, body=redacted_body
        )
    if isinstance(status, int) and status >= 500:
        return ServerError(message, status=status, code=code, detail=detail, body=redacted_body)
    if saw_connection:
        return TransportError(message, body=redacted_body)
    return EbbmemApiError(message, status=status, code=code, detail=detail, body=redacted_body)


def _annotation_accepts_float(annotation: Any) -> bool:
    if annotation is float:
        return True
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        return _annotation_accepts_float(args[0]) if args else False
    if origin in (Union, types.UnionType):
        return any(_annotation_accepts_float(arg) for arg in get_args(annotation))
    return False


def _model_field_accepts_float(req_cls: Any, field_name: str) -> bool:
    fields = getattr(req_cls, "model_fields", None)
    if not isinstance(fields, Mapping) or field_name not in fields:
        return True
    field_info = fields[field_name]
    annotation = getattr(field_info, "annotation", None)
    if annotation is None:
        return True
    return _annotation_accepts_float(annotation)


def _load_sdk_models() -> dict[str, Any]:
    """Import ebbmem_client generated models if ebbmem-client is installed."""
    try:
        from ebbmem_client.generated.models.memory_id import MemoryId
        from ebbmem_client.generated.models.recall_request_model import RecallRequestModel
        from ebbmem_client.generated.models.reinforce_request_model import ReinforceRequestModel
        from ebbmem_client.generated.models.remember_request_model import RememberRequestModel
        from ebbmem_client.generated.models.used_ids_inner import UsedIdsInner
    except ImportError:
        return {}
    models: dict[str, Any] = {
        "MemoryId": MemoryId,
        "RecallRequestModel": RecallRequestModel,
        "ReinforceRequestModel": ReinforceRequestModel,
        "RememberRequestModel": RememberRequestModel,
        "UsedIdsInner": UsedIdsInner,
    }
    try:
        from ebbmem_client.generated.models.session_id import SessionId

        models["SessionId"] = SessionId
    except ImportError:
        pass
    return models


def _detect_sdk_capabilities(sdk_client: Any, models: Mapping[str, Any]) -> SdkCapabilities:
    raw = getattr(sdk_client, "raw", None)
    raw_mem = getattr(raw, "memory", None)
    has_raw = raw_mem is not None
    recall_cls = models.get("RecallRequestModel")
    recall_fields = getattr(recall_cls, "model_fields", {}) if recall_cls is not None else {}
    has_recall_filters = (
        all(f in recall_fields for f in ("tier", "tags", "session_id"))
        if recall_fields
        else bool(getattr(sdk_client, "supports_recall_filters", False))
    )
    has_exact_key = callable(getattr(raw_mem, "get_by_key", None)) or callable(
        getattr(getattr(sdk_client, "memory", None), "get_by_key", None)
    )
    has_ns_list = callable(getattr(raw_mem, "list_namespaces", None)) or callable(
        getattr(getattr(sdk_client, "memory", None), "list_namespaces", None)
    )
    return SdkCapabilities(
        tier_on_remember=has_raw,
        session_id_on_remember=has_raw,
        metadata_on_remember=has_raw,
        recall_filters=has_recall_filters,
        recall_breakdown_and_id=True,
        reinforce_used_ids_and_recall_id=has_raw,
        reinforce_default_delta_0_1=True,
        deterministic_idempotency_key=True,
        exact_key_get=has_exact_key,
        exact_key_delete=has_exact_key,
        metadata_filter_on_list_recall=bool(getattr(sdk_client, "supports_metadata_filter", False)),
        namespace_prefix_list=has_ns_list,
        notes=(
            "Released ebbmem-client 0.2.0 facade omits tier/session_id/metadata on remember, "
            "profile/min_score/domain on recall, and used_ids/recall_id on reinforce (where "
            "facade delta defaults to 1.0). Adapter bridges these via raw.memory generated models "
            "(wrapping SessionId and rejecting float valence/arousal where RememberRequestModel "
            "enforces StrictInt), while P0-09 features (get_by_key, delete_by_key, "
            "metadata_filter, list_namespaces) and recall tier/tags/session_id filters raise "
            "UnsupportedCapabilityError when absent.",
        ),
    )


class EbbmemClientSdkAdapter:
    """Thin adapter wrapping released sync ebbmem-client (0.2.0+) behind IC-1 invariants."""

    def __init__(self, sdk_client: Any) -> None:
        _validate_sdk_config(sdk_client)
        self._sdk = sdk_client
        self._models = _load_sdk_models()
        self.capabilities = _detect_sdk_capabilities(sdk_client, self._models)

    def __repr__(self) -> str:
        return f"EbbmemClientSdkAdapter(has_raw={getattr(self._sdk, 'raw', None) is not None})"

    def _invoke(
        self,
        name: str,
        op: Callable[[dict[str, str]], Any],
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        invoke_fn = getattr(self._sdk, "_invoke", None)
        try:
            if callable(invoke_fn):
                return invoke_fn(name, op, idempotency_key=idempotency_key)
            return op({})
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

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
    ) -> dict[str, Any]:
        if idempotency_key is not None:
            validate_idempotency_key(idempotency_key)
        header_idem_key = _get_header_ci(headers, "idempotency-key")
        effective_idempotency_key = (
            idempotency_key
            if idempotency_key is not None
            else (header_idem_key if isinstance(header_idem_key, str) and header_idem_key else None)
        )
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "remember", None)):
            req_cls = self._models.get("RememberRequestModel")
            session_id_cls = self._models.get("SessionId")
            if isinstance(valence, bool) or isinstance(arousal, bool):
                raise ValidationError("valence and arousal must be numeric, not bool")
            if req_cls is not None:
                if (
                    valence is not None
                    and isinstance(valence, float)
                    and not _model_field_accepts_float(req_cls, "valence")
                ) or (
                    arousal is not None
                    and isinstance(arousal, float)
                    and not _model_field_accepts_float(req_cls, "arousal")
                ):
                    raise UnsupportedCapabilityError(
                        "Released ebbmem-client 0.2.0 RememberRequestModel requires integer "
                        "valence [-128, 127] and arousal [0, 255]; float values are unsupported "
                        "(use StdlibHttpTransport)"
                    )
            effective_session_id: Any = session_id
            if session_id is not None and session_id_cls is not None:
                if not isinstance(session_id, session_id_cls):
                    try:
                        effective_session_id = session_id_cls(session_id)
                    except ValueError as exc:
                        raise ValidationError(redact_secrets(str(exc))) from exc
            payload: dict[str, Any] = {
                "content": content,
                "tier": tier,
                "tags": list(tags) if tags is not None else None,
                "importance": importance,
                "valence": valence,
                "arousal": arousal,
                "session_id": effective_session_id,
                "timestamp_ms": timestamp_ms,
                "namespace": namespace,
                "metadata": dict(metadata) if metadata is not None else None,
                "idempotency_key": idempotency_key,
            }
            clean_payload = {k: v for k, v in payload.items() if v is not None}
            if req_cls is not None:
                try:
                    model = req_cls(**clean_payload)
                except ValueError as exc:
                    raise ValidationError(redact_secrets(str(exc))) from exc
            else:
                model = clean_payload
            result = self._invoke(
                "remember",
                lambda hdrs: raw_mem.remember(
                    model,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
                idempotency_key=effective_idempotency_key,
            )
            return _to_plain_dict(result)

        if (
            any(
                x is not None
                for x in (valence, arousal, session_id, timestamp_ms, metadata, namespace)
            )
            or tier != "semantic"
            or bool(headers)
        ):
            raise UnsupportedCapabilityError(
                "Wrapped SDK lacks raw.memory.remember needed for IC-1 remember fields/headers"
            )
        try:
            result = self._sdk.memory.remember(
                content,
                tags=list(tags) if tags is not None else None,
                importance=importance,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
        return _to_plain_dict(result)

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
    ) -> dict[str, Any]:
        if (tier is not None or tags or session_id is not None) and (
            not self.capabilities.recall_filters
        ):
            raise UnsupportedCapabilityError(
                "Released ebbmem-client 0.2.0 RecallRequestModel lacks tier/tags/session_id filters"
            )
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "recall", None)):
            req_cls = self._models.get("RecallRequestModel")
            payload: dict[str, Any] = {
                "query": query,
                "limit": limit,
                "reinforce": False,
                "profile": profile,
                "min_score": min_score,
                "domain": domain,
                "namespace": namespace,
            }
            if self.capabilities.recall_filters:
                if tier is not None:
                    payload["tier"] = tier
                if tags:
                    payload["tags"] = list(tags)
                if session_id is not None:
                    payload["session_id"] = session_id
            clean_payload = {k: v for k, v in payload.items() if v is not None}
            model = req_cls(**clean_payload) if req_cls is not None else clean_payload
            result = self._invoke(
                "recall",
                lambda hdrs: raw_mem.recall(
                    model,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                    _request_timeout=timeout_s,
                ),
            )
            return _to_plain_dict(result)

        if (
            any(
                x is not None
                for x in (namespace, profile, min_score, domain, tier, session_id, timeout_s)
            )
            or bool(tags)
            or bool(headers)
        ):
            raise UnsupportedCapabilityError(
                "Wrapped SDK lacks raw.memory.recall needed for IC-1 recall fields/headers"
            )
        try:
            result = self._sdk.memory.recall(query, limit=limit)
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
        return _to_plain_dict(result)

    def reinforce(
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
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "reinforce", None)):
            req_cls = self._models.get("ReinforceRequestModel")
            mem_id_cls = self._models.get("MemoryId")
            used_inner_cls = self._models.get("UsedIdsInner")
            if req_cls is not None and mem_id_cls is not None:
                try:
                    payload: dict[str, Any] = {
                        "memory_id": mem_id_cls(memory_id),
                        "delta": validated_delta,
                        "used_ids": (
                            [used_inner_cls(uid) for uid in used_ids]
                            if used_inner_cls is not None and used_ids
                            else (list(used_ids) if used_ids else None)
                        ),
                        "recall_id": recall_id,
                        "namespace": namespace,
                    }
                    clean_payload = {k: v for k, v in payload.items() if v is not None}
                    model = req_cls(**clean_payload)
                except ValueError as exc:
                    raise ValidationError(redact_secrets(str(exc))) from exc
            else:
                model = {
                    "memory_id": memory_id,
                    "delta": validated_delta,
                    **({"used_ids": list(used_ids)} if used_ids else {}),
                    **({"recall_id": recall_id} if recall_id is not None else {}),
                    **({"namespace": namespace} if namespace is not None else {}),
                }
            result = self._invoke(
                "reinforce",
                lambda hdrs: raw_mem.reinforce(
                    model,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
            )
            return _to_plain_dict(result)

        if used_ids or recall_id is not None or namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Released ebbmem-client 0.2.0 facade lacks used_ids/recall_id/namespace/headers "
                "on reinforce"
            )
        try:
            result = self._sdk.memory.reinforce(memory_id, delta=validated_delta)
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
        return _to_plain_dict(result)

    def get(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "get_memory", None)):
            result = self._invoke(
                "get",
                lambda hdrs: raw_mem.get_memory(
                    memory_id,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
            )
            return _to_plain_dict(result)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped SDK lacks raw.memory.get_memory needed for namespace/headers"
            )
        try:
            return _to_plain_dict(self._sdk.memory.get(memory_id))
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def forget(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "forget_memory", None)):
            result = self._invoke(
                "forget",
                lambda hdrs: raw_mem.forget_memory(
                    memory_id,
                    purge=False,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
            )
            return _to_plain_dict(result)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped SDK lacks raw.memory.forget_memory needed for namespace/headers"
            )
        try:
            return _to_plain_dict(self._sdk.memory.forget(memory_id, purge=False))
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def purge(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "purge_memory", None)):
            purge_raw = getattr(raw_mem, "purge_memory_without_preload_content", None)
            if callable(purge_raw) and hasattr(raw_mem, "api_client"):

                def _op_purge_raw(hdrs: dict[str, str]) -> dict[str, Any]:
                    resp = purge_raw(
                        memory_id,
                        x_ebb_namespace=namespace,
                        _headers=_merge_headers_ci(hdrs, headers),
                    )
                    raw_bytes = resp.read()
                    status_code = getattr(resp, "status", 200)
                    text = (
                        raw_bytes.decode("utf-8", errors="replace")
                        if isinstance(raw_bytes, bytes)
                        else (raw_bytes or "")
                    )
                    if not 200 <= status_code <= 299:
                        try:
                            from ebbmem_client.generated.exceptions import ApiException

                            raise ApiException.from_response(
                                http_resp=resp,
                                body=text,
                                data=None,
                            )
                        except ImportError:
                            resp_headers = {
                                str(k): str(v) for k, v in getattr(resp, "headers", {}).items()
                            }
                            try:
                                err_body: Any = json.loads(text) if text else None
                            except json.JSONDecodeError:
                                err_body = {"raw": redact_secrets(text)}
                            raise _http_error_from_response(
                                HttpResponse(
                                    status=status_code, headers=resp_headers, body=err_body
                                )
                            ) from None
                    parsed = json.loads(text) if text else {}
                    out = _to_plain_dict(parsed)
                    if "status" not in out and out.get("found") is not False:
                        out["status"] = "purged"
                    if "irreversible" not in out and out.get("status") == "purged":
                        out["irreversible"] = True
                    return out

                return self._invoke("purge", _op_purge_raw)

            result = self._invoke(
                "purge",
                lambda hdrs: raw_mem.purge_memory(
                    memory_id,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
            )
            out = _to_plain_dict(result)
            if "status" not in out and out.get("found") is not False:
                out["status"] = "purged"
            if "irreversible" not in out and out.get("status") == "purged":
                out["irreversible"] = True
            return out
        if raw_mem is not None and callable(getattr(raw_mem, "forget_memory", None)):
            result = self._invoke(
                "purge",
                lambda hdrs: raw_mem.forget_memory(
                    memory_id,
                    purge=True,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
            )
            return _to_plain_dict(result)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped SDK lacks raw.memory.forget_memory needed for namespace/headers"
            )
        try:
            return _to_plain_dict(self._sdk.memory.forget(memory_id, purge=True))
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def status(
        self,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "status", None)):
            result = self._invoke(
                "status",
                lambda hdrs: raw_mem.status(
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
            )
            return _to_plain_dict(result)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped SDK lacks raw.memory.status needed for namespace/headers"
            )
        try:
            return _to_plain_dict(self._sdk.memory.status())
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def health(self) -> dict[str, Any]:
        try:
            return _to_plain_dict(self._sdk.health())
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def get_by_key(self, key: str, *, namespace: str | None = None) -> dict[str, Any]:
        if not self.capabilities.exact_key_get:
            raise UnsupportedCapabilityError(
                "get_by_key requires P0-09 exact-key APIs not present in ebbmem-client 0.2.0"
            )
        fn = getattr(getattr(self._sdk, "memory", None), "get_by_key", None) or getattr(
            getattr(getattr(self._sdk, "raw", None), "memory", None), "get_by_key", None
        )
        if not callable(fn):
            raise UnsupportedCapabilityError(
                "get_by_key requires P0-09 exact-key APIs not present in ebbmem-client 0.2.0"
            )
        try:
            return _to_plain_dict(fn(key, namespace=namespace))
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def delete_by_key(self, key: str, *, namespace: str | None = None) -> dict[str, Any]:
        if not self.capabilities.exact_key_delete:
            raise UnsupportedCapabilityError(
                "delete_by_key requires P0-09 exact-key APIs not present in ebbmem-client 0.2.0"
            )
        fn = getattr(getattr(self._sdk, "memory", None), "delete_by_key", None) or getattr(
            getattr(getattr(self._sdk, "raw", None), "memory", None), "delete_by_key", None
        )
        if not callable(fn):
            raise UnsupportedCapabilityError(
                "delete_by_key requires P0-09 exact-key APIs not present in ebbmem-client 0.2.0"
            )
        try:
            return _to_plain_dict(fn(key, namespace=namespace))
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def list_namespaces(self, *, prefix: str | None = None) -> list[str]:
        if not self.capabilities.namespace_prefix_list:
            raise UnsupportedCapabilityError(
                "list_namespaces requires P0-09 namespace-prefix API not in ebbmem-client 0.2.0"
            )
        fn = getattr(getattr(self._sdk, "memory", None), "list_namespaces", None) or getattr(
            getattr(getattr(self._sdk, "raw", None), "memory", None), "list_namespaces", None
        )
        if not callable(fn):
            raise UnsupportedCapabilityError(
                "list_namespaces requires P0-09 namespace-prefix API not in ebbmem-client 0.2.0"
            )
        try:
            return list(fn(prefix=prefix))
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc


class AsyncEbbmemClientSdkAdapter:
    """Async counterpart to EbbmemClientSdkAdapter wrapping AsyncEbbmemClient (0.2.0+)."""

    def __init__(self, async_sdk_client: Any) -> None:
        _validate_sdk_config(async_sdk_client)
        self._sdk = async_sdk_client
        self._models = _load_sdk_models()
        self.capabilities = _detect_sdk_capabilities(async_sdk_client, self._models)

    def __repr__(self) -> str:
        has_inv = callable(getattr(self._sdk, "_invoke", None))
        return f"AsyncEbbmemClientSdkAdapter(has_invoke={has_inv})"

    async def _invoke_rest(
        self,
        name: str,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        invoke_fn = getattr(self._sdk, "_invoke", None)
        if not callable(invoke_fn):
            raise UnsupportedCapabilityError(
                "Async SDK adapter requires AsyncEbbmemClient._invoke or compatible async memory"
            )
        extra_kwargs: dict[str, Any] = {}
        sig = inspect.signature(invoke_fn)
        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if headers:
            if "headers" in sig.parameters or accepts_var_kw:
                extra_kwargs["headers"] = dict(headers)
            else:
                raise UnsupportedCapabilityError(
                    "Wrapped AsyncEbbmemClient._invoke does not support custom headers"
                )
        if timeout_s is not None:
            if "timeout_s" in sig.parameters or accepts_var_kw:
                extra_kwargs["timeout_s"] = timeout_s
            elif "timeout" in sig.parameters:
                extra_kwargs["timeout"] = timeout_s
            else:
                raise UnsupportedCapabilityError(
                    "Wrapped AsyncEbbmemClient._invoke does not support per-call timeout_s"
                )
        try:
            return await invoke_fn(
                name,
                method,
                path,
                json_body=json_body,
                params=params,
                idempotency_key=idempotency_key,
                **extra_kwargs,
            )
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

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
        if idempotency_key is not None:
            validate_idempotency_key(idempotency_key)
        header_idem_key = _get_header_ci(headers, "idempotency-key")
        effective_idempotency_key = (
            idempotency_key
            if idempotency_key is not None
            else (header_idem_key if isinstance(header_idem_key, str) and header_idem_key else None)
        )
        if callable(getattr(self._sdk, "_invoke", None)):
            body: dict[str, Any] = {"content": content, "tier": tier}
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
            if namespace is not None:
                body["namespace"] = namespace
            if metadata is not None:
                body["metadata"] = dict(metadata)
            if idempotency_key is not None:
                body["idempotency_key"] = idempotency_key
            res = await self._invoke_rest(
                "remember",
                "POST",
                "/api/v1/memory/remember",
                json_body=body,
                headers=headers,
                idempotency_key=effective_idempotency_key,
            )
            return _to_plain_dict(res)

        if (
            any(
                x is not None
                for x in (valence, arousal, session_id, timestamp_ms, metadata, namespace)
            )
            or tier != "semantic"
            or bool(headers)
        ):
            raise UnsupportedCapabilityError(
                "Wrapped async SDK lacks _invoke needed for IC-1 remember fields/headers"
            )
        try:
            res = await self._sdk.memory.remember(
                content,
                tags=list(tags) if tags is not None else None,
                importance=importance,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
        return _to_plain_dict(res)

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
        if (tier is not None or tags or session_id is not None) and (
            not self.capabilities.recall_filters
        ):
            raise UnsupportedCapabilityError(
                "Released ebbmem-client 0.2.0 RecallRequestModel lacks tier/tags/session_id filters"
            )
        if callable(getattr(self._sdk, "_invoke", None)):
            body: dict[str, Any] = {"query": query, "limit": limit, "reinforce": False}
            if profile is not None:
                body["profile"] = profile
            if min_score is not None:
                body["min_score"] = min_score
            if domain is not None:
                body["domain"] = domain
            if namespace is not None:
                body["namespace"] = namespace
            if self.capabilities.recall_filters:
                if tier is not None:
                    body["tier"] = tier
                if tags:
                    body["tags"] = list(tags)
                if session_id is not None:
                    body["session_id"] = session_id
            res = await self._invoke_rest(
                "recall",
                "POST",
                "/api/v1/memory/recall",
                json_body=body,
                headers=headers,
                timeout_s=timeout_s,
            )
            return _to_plain_dict(res)

        if (
            any(
                x is not None
                for x in (namespace, profile, min_score, domain, tier, session_id, timeout_s)
            )
            or bool(tags)
            or bool(headers)
        ):
            raise UnsupportedCapabilityError(
                "Wrapped async SDK lacks _invoke needed for IC-1 recall fields/headers"
            )
        try:
            res = await self._sdk.memory.recall(query, limit=limit)
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
        return _to_plain_dict(res)

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
        if callable(getattr(self._sdk, "_invoke", None)):
            body: dict[str, Any] = {"memory_id": memory_id, "delta": validated_delta}
            if used_ids:
                body["used_ids"] = list(used_ids)
            if recall_id is not None:
                body["recall_id"] = recall_id
            if namespace is not None:
                body["namespace"] = namespace
            res = await self._invoke_rest(
                "reinforce",
                "POST",
                "/api/v1/memory/reinforce",
                json_body=body,
                headers=headers,
            )
            return _to_plain_dict(res)

        if used_ids or recall_id is not None or namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Released ebbmem-client 0.2.0 facade lacks used_ids/recall_id/namespace/headers "
                "on reinforce"
            )
        try:
            res = await self._sdk.memory.reinforce(memory_id, delta=validated_delta)
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
        return _to_plain_dict(res)

    async def get(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if callable(getattr(self._sdk, "_invoke", None)):
            params = {"namespace": namespace} if namespace is not None else None
            encoded = urllib.parse.quote(memory_id, safe="")
            res = await self._invoke_rest(
                "get",
                "GET",
                f"/api/v1/memory/{encoded}",
                params=params,
                headers=headers,
            )
            return _to_plain_dict(res)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped async SDK lacks _invoke needed for get namespace/headers"
            )
        try:
            return _to_plain_dict(await self._sdk.memory.get(memory_id))
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def forget(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if callable(getattr(self._sdk, "_invoke", None)):
            params = {"namespace": namespace} if namespace is not None else None
            encoded = urllib.parse.quote(memory_id, safe="")
            res = await self._invoke_rest(
                "forget",
                "DELETE",
                f"/api/v1/memory/{encoded}",
                params=params,
                headers=headers,
            )
            return _to_plain_dict(res)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped async SDK lacks _invoke needed for forget namespace/headers"
            )
        try:
            return _to_plain_dict(await self._sdk.memory.forget(memory_id, purge=False))
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def purge(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if callable(getattr(self._sdk, "_invoke", None)):
            params = {"namespace": namespace} if namespace is not None else None
            encoded = urllib.parse.quote(memory_id, safe="")
            res = await self._invoke_rest(
                "purge",
                "DELETE",
                f"/api/v1/memory/{encoded}/purge",
                params=params,
                headers=headers,
            )
            out = _to_plain_dict(res)
            if isinstance(out, dict) and "status" not in out and out.get("found") is not False:
                out["status"] = "purged"
                out.setdefault("irreversible", True)
            elif isinstance(out, dict) and out.get("status") == "purged":
                out.setdefault("irreversible", True)
            return out
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped async SDK lacks _invoke needed for purge namespace/headers"
            )
        try:
            out = _to_plain_dict(await self._sdk.memory.forget(memory_id, purge=True))
            if isinstance(out, dict) and "status" not in out and out.get("found") is not False:
                out["status"] = "purged"
                out.setdefault("irreversible", True)
            elif isinstance(out, dict) and out.get("status") == "purged":
                out.setdefault("irreversible", True)
            return out
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def status(
        self,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if callable(getattr(self._sdk, "_invoke", None)):
            params = {"namespace": namespace} if namespace is not None else None
            res = await self._invoke_rest(
                "status",
                "GET",
                "/api/v1/memory/status",
                params=params,
                headers=headers,
            )
            return _to_plain_dict(res)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped async SDK lacks _invoke needed for status namespace/headers"
            )
        try:
            return _to_plain_dict(await self._sdk.memory.status())
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def health(self) -> dict[str, Any]:
        try:
            return _to_plain_dict(await self._sdk.health())
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def get_by_key(self, key: str, *, namespace: str | None = None) -> dict[str, Any]:
        if not self.capabilities.exact_key_get:
            raise UnsupportedCapabilityError(
                "get_by_key requires P0-09 exact-key APIs not present in ebbmem-client 0.2.0"
            )
        fn = getattr(getattr(self._sdk, "memory", None), "get_by_key", None) or getattr(
            getattr(getattr(self._sdk, "raw", None), "memory", None), "get_by_key", None
        )
        if not callable(fn):
            raise UnsupportedCapabilityError(
                "get_by_key requires P0-09 exact-key APIs not present in ebbmem-client 0.2.0"
            )
        try:
            res = fn(key, namespace=namespace)
            if inspect.isawaitable(res):
                res = await res
            return _to_plain_dict(res)
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def delete_by_key(self, key: str, *, namespace: str | None = None) -> dict[str, Any]:
        if not self.capabilities.exact_key_delete:
            raise UnsupportedCapabilityError(
                "delete_by_key requires P0-09 exact-key APIs not present in ebbmem-client 0.2.0"
            )
        fn = getattr(getattr(self._sdk, "memory", None), "delete_by_key", None) or getattr(
            getattr(getattr(self._sdk, "raw", None), "memory", None), "delete_by_key", None
        )
        if not callable(fn):
            raise UnsupportedCapabilityError(
                "delete_by_key requires P0-09 exact-key APIs not present in ebbmem-client 0.2.0"
            )
        try:
            res = fn(key, namespace=namespace)
            if inspect.isawaitable(res):
                res = await res
            return _to_plain_dict(res)
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def list_namespaces(self, *, prefix: str | None = None) -> list[str]:
        if not self.capabilities.namespace_prefix_list:
            raise UnsupportedCapabilityError(
                "list_namespaces requires P0-09 namespace-prefix API not in ebbmem-client 0.2.0"
            )
        fn = getattr(getattr(self._sdk, "memory", None), "list_namespaces", None) or getattr(
            getattr(getattr(self._sdk, "raw", None), "memory", None), "list_namespaces", None
        )
        if not callable(fn):
            raise UnsupportedCapabilityError(
                "list_namespaces requires P0-09 namespace-prefix API not in ebbmem-client 0.2.0"
            )
        try:
            res = fn(prefix=prefix)
            if inspect.isawaitable(res):
                res = await res
            return list(res)
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
