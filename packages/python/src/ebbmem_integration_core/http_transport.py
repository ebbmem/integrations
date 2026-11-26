"""Zero-dependency Python stdlib sync HTTP transport for IC-1."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

from ebbmem_integration_core.errors import RequestTimeoutError, TransportError, ValidationError
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
    redact_secrets,
    validate_auth_credentials,
    validate_base_url,
    validate_request_target,
)
from ebbmem_integration_core.transport_types import (
    HttpResponse,
    SdkCapabilities,
    _extract_mcp_result_dict,
    _http_error_from_response,
    _mcp_error_from_payload,
    _merge_headers_ci,
    _set_header_ci,
    _setdefault_header_ci,
)


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
            list_memories=True,
            episodic_remember=True,
            episodic_tail=True,
            notes=(
                "Built-in stdlib HTTP transport supports full IC-1 REST verbs, list_memories, "
                "and MCP episodic_remember/episodic_tail; P0-09 exact-key lookup/delete and "
                "namespace listing are not yet enabled on the base transport.",
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

    def _build_list_request(
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
        headers: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        req_headers = dict(headers or {})
        query: dict[str, str] = {
            "limit": str(limit),
            "tombstoned": "true" if tombstoned else "false",
        }
        if namespace is not None:
            _setdefault_header_ci(req_headers, "X-Ebb-Namespace", namespace)
            query["namespace"] = namespace
        if tier is not None:
            query["tier"] = tier
        if tags:
            query["tags"] = ",".join(tags)
        if search is not None:
            query["search"] = search
        if session_id is not None:
            query["session_id"] = session_id
        if cursor is not None:
            query["cursor"] = cursor
        if page is not None:
            query["page"] = str(page)
        if page_size is not None:
            query["page_size"] = str(page_size)
        if min_importance is not None:
            query["min_importance"] = str(min_importance)
        if min_valence is not None:
            query["min_valence"] = str(min_valence)
        if max_valence is not None:
            query["max_valence"] = str(max_valence)
        if metadata_filter is not None:
            query["metadata_filter"] = json.dumps(dict(metadata_filter))
        return req_headers, query

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
    ) -> dict[str, Any]:
        req_headers, query = self._build_list_request(
            namespace=namespace,
            tier=tier,
            tags=tags,
            tombstoned=tombstoned,
            search=search,
            session_id=session_id,
            limit=limit,
            cursor=cursor,
            page=page,
            page_size=page_size,
            min_importance=min_importance,
            min_valence=min_valence,
            max_valence=max_valence,
            metadata_filter=metadata_filter,
            headers=headers,
        )
        return self._call_json(
            "GET",
            "/api/v1/memory",
            headers=req_headers,
            query=query,
            timeout_s=timeout_s,
        )

    def _prepare_mcp_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        namespace: str | None = None,
        mcp_profile: str = "full",
        idempotency_key: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        if mcp_profile not in {"core", "full", "admin"}:
            raise ValidationError(f"Invalid mcp_profile: {mcp_profile!r}")
        req_headers = dict(headers or {})
        _set_header_ci(req_headers, "Accept", "application/json, text/event-stream")
        _setdefault_header_ci(req_headers, "MCP-Protocol-Version", "2026-07-28")
        _set_header_ci(req_headers, "Mcp-Method", "tools/call")
        _set_header_ci(req_headers, "Mcp-Name", tool_name)
        _setdefault_header_ci(req_headers, "X-Ebb-MCP-Profile", mcp_profile)
        if namespace is not None:
            _setdefault_header_ci(req_headers, "X-Ebb-Namespace", namespace)
        if idempotency_key is not None:
            validate_idempotency_key(idempotency_key)
            _setdefault_header_ci(req_headers, "Idempotency-Key", idempotency_key)
        rpc_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        return f"/mcp/{mcp_profile}", rpc_body, req_headers

    def _call_mcp_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        namespace: str | None = None,
        mcp_profile: str = "full",
        idempotency_key: str | None = None,
        timeout_s: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        path, rpc_body, req_headers = self._prepare_mcp_call(
            tool_name,
            arguments,
            namespace=namespace,
            mcp_profile=mcp_profile,
            idempotency_key=idempotency_key,
            headers=headers,
        )
        resp = self.request_with_retry(
            "POST",
            path,
            json_body=rpc_body,
            headers=req_headers,
            timeout_s=timeout_s,
        )
        if resp.status >= 400:
            raise _http_error_from_response(resp)
        if isinstance(resp.body, Mapping) and resp.body.get("error") is not None:
            raise _mcp_error_from_payload(resp.status, resp.headers, resp.body)
        return _extract_mcp_result_dict(resp.body)

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
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"content": content, "role": role}
        if thread_key is not None:
            args["thread_key"] = thread_key
        if session_id is not None:
            args["session_id"] = session_id
        if tags is not None:
            args["tags"] = list(tags)
        if namespace is not None:
            args["namespace"] = namespace
        if idempotency_key is not None:
            validate_idempotency_key(idempotency_key)
            args["idempotency_key"] = idempotency_key
        return self._call_mcp_tool(
            "memory_episodic_remember",
            args,
            namespace=namespace,
            mcp_profile=mcp_profile,
            idempotency_key=idempotency_key,
            headers=headers,
        )

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
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"limit": limit}
        if thread_key is not None:
            args["thread_key"] = thread_key
        if session_id is not None:
            args["session_id"] = session_id
        if namespace is not None:
            args["namespace"] = namespace
        return self._call_mcp_tool(
            "memory_episodic_tail",
            args,
            namespace=namespace,
            mcp_profile=mcp_profile,
            timeout_s=timeout_s,
            headers=headers,
        )
