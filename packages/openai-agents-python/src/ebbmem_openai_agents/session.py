"""OpenAI Agents SDK Session implementation backed by EbbMem's episodic thread log."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from agents.items import TResponseInputItem
from agents.memory.session import await_session_mutation
from agents.memory.session_settings import (
    SessionSettings,
    coerce_session_settings,
    resolve_session_limit,
)
from ebbmem_integration_core import (
    AsyncIntegrationClient,
    EbbmemApiError,
    IntegrationClient,
    ScopeResolver,
    UnsupportedCapabilityError,
    ValidationError,
    make_idempotency_key,
    redact_payload,
    redact_secrets,
    validate_auth_credentials,
    validate_base_url,
)

_ENVELOPE_VERSION = 1
_EPISODIC_ROLES = frozenset({"user", "assistant", "system", "tool"})
_MCP_PROFILES = frozenset({"core", "full", "admin"})


def _thread_tag_for_session(session_id: str) -> str:
    """Build a deterministic non-reserved tag that identifies a session thread."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return f"oai_thread_{digest}"


def _encode_envelope(
    *,
    op: Literal["item", "pop", "clear"],
    seq: int,
    item: Mapping[str, Any] | None = None,
    target_seq: int | None = None,
    redact: bool = True,
) -> str:
    """Encode a session log event so IC-1 text redaction cannot corrupt JSON structure."""
    envelope: dict[str, Any] = {"__oai_v": _ENVELOPE_VERSION, "op": op, "seq": seq}
    if target_seq is not None:
        envelope["target_seq"] = target_seq
    if item is not None:
        safe_item = redact_payload(dict(item)) if redact else dict(item)
        raw_json = json.dumps(safe_item, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        # Hex encoding uses only [0-9a-f], preventing secret-redaction regexes from altering JSON.
        envelope["hex"] = raw_json.hex()
    return json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)


def _decode_envelope(raw_content: str) -> dict[str, Any] | None:
    """Decode an encoded session log event, returning None for unrelated records."""
    if not isinstance(raw_content, str) or not raw_content.strip().startswith("{"):
        return None
    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("__oai_v") != _ENVELOPE_VERSION:
        return None
    op = data.get("op")
    seq = data.get("seq")
    if op not in {"item", "pop", "clear"} or not isinstance(seq, int) or isinstance(seq, bool):
        return None
    if op == "item":
        hex_val = data.get("hex")
        b64_val = data.get("b64")
        try:
            if isinstance(hex_val, str):
                decoded_bytes = bytes.fromhex(hex_val)
            elif isinstance(b64_val, str):
                decoded_bytes = base64.urlsafe_b64decode(b64_val.encode("ascii"))
            else:
                return None
            item_obj = json.loads(decoded_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(item_obj, dict):
            return None
        return {"op": "item", "seq": seq, "item": item_obj}
    if op == "pop":
        target_seq = data.get("target_seq")
        if not isinstance(target_seq, int) or isinstance(target_seq, bool):
            return None
        return {"op": "pop", "seq": seq, "target_seq": target_seq}
    return {"op": "clear", "seq": seq}


def _infer_episodic_role(item: Mapping[str, Any]) -> str:
    """Map an OpenAI Responses input item to an EbbMem episodic conversation role."""
    role = item.get("role")
    if isinstance(role, str) and role.lower() in _EPISODIC_ROLES:
        return role.lower()
    item_type = item.get("type")
    if item_type == "function_call":
        return "assistant"
    if item_type in {"function_call_output", "computer_call_output", "local_shell_call_output"}:
        return "tool"
    return "user"


def _replay_log_events(
    events: Sequence[dict[str, Any]],
) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    """Replay chronological item/pop/clear events into active (seq, item) entries."""
    ordered = sorted(events, key=lambda ev: int(ev["seq"]))
    active: list[tuple[int, dict[str, Any]]] = []
    max_seq = 0
    for ev in ordered:
        seq = int(ev["seq"])
        if seq > max_seq:
            max_seq = seq
        op = ev["op"]
        if op == "clear":
            active.clear()
        elif op == "item":
            active.append((seq, ev["item"]))
        elif op == "pop":
            target_seq = int(ev["target_seq"])
            for idx in range(len(active) - 1, -1, -1):
                if active[idx][0] == target_seq:
                    active.pop(idx)
                    break
    return active, max_seq


@dataclass
class _StoredEntry:
    seq: int
    memory_id: str | None
    item: dict[str, Any]


@runtime_checkable
class EpisodicSessionBackend(Protocol):
    """Pluggable episodic backend used by EbbmemSession."""

    async def load_entries(self, session_id: str) -> tuple[list[_StoredEntry], int]:
        """Return active entries in chronological order and the highest sequence number seen."""
        ...

    async def append_items(
        self,
        session_id: str,
        items: Sequence[tuple[int, dict[str, Any]]],
        *,
        redact_items: bool,
    ) -> None:
        """Persist new sequenced items to the episodic thread log."""
        ...

    async def pop_entry(self, session_id: str, entry: _StoredEntry, next_seq: int) -> None:
        """Remove a popped entry from the active session log."""
        ...

    async def clear_entries(
        self,
        session_id: str,
        entries: Sequence[_StoredEntry],
        next_seq: int,
    ) -> None:
        """Clear all active entries for the given session."""
        ...


class RestEpisodicBackend:
    """Episodic backend over EbbMem REST endpoints via AsyncIntegrationClient."""

    def __init__(
        self,
        client: AsyncIntegrationClient | IntegrationClient,
        *,
        user_id: str | None = None,
        app_namespace: str | None = None,
    ) -> None:
        self._client = client
        self._user_id = user_id
        self._app_namespace = app_namespace
        self._max_seq_by_session: dict[str, int] = {}

    def _resolve(self, session_id: str) -> tuple[str, str]:
        resolved = self._client.scope.resolve(
            user_id=self._user_id,
            app_namespace=self._app_namespace,
            session_id=session_id,
            tags=[_thread_tag_for_session(session_id)],
        )
        return resolved.namespace, resolved.tags[0]

    async def _call_async_or_thread(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if isinstance(self._client, AsyncIntegrationClient):
            return await fn(*args, **kwargs)
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _list_via_rest(self, namespace: str, thread_tag: str) -> list[dict[str, Any]] | None:
        transport = self._client.transport
        request_fn = getattr(transport, "request_with_retry", None)
        if request_fn is None:
            return None

        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            query: dict[str, str] = {
                "tier": "episodic",
                "tags": thread_tag,
                "tombstoned": "false",
                "limit": "100",
                "namespace": namespace,
            }
            if cursor is not None:
                query["cursor"] = cursor
            resp = await self._call_async_or_thread(
                request_fn,
                "GET",
                "/api/v1/memory",
                query=query,
                headers={"X-Ebb-Namespace": namespace},
            )
            if resp.status == 404:
                return None
            if resp.status >= 400:
                raise EbbmemApiError(
                    f"EbbMem HTTP {resp.status} while listing episodic session items",
                    status=resp.status,
                    body=resp.body,
                )
            body = resp.body if isinstance(resp.body, Mapping) else {}
            page_items = body.get("items") or body.get("rows") or body.get("results") or []
            if isinstance(page_items, list):
                for row in page_items:
                    if isinstance(row, Mapping):
                        rows.append(dict(row))
            next_cursor = body.get("next_cursor") or body.get("cursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return rows

    async def _list_via_recall(
        self,
        session_id: str,
        namespace: str,
        thread_tag: str,
    ) -> list[dict[str, Any]]:
        transport = self._client.transport
        try:
            resp = await self._call_async_or_thread(
                transport.recall,
                query=thread_tag,
                namespace=namespace,
                limit=100,
                tier="episodic",
                tags=[thread_tag],
                session_id=session_id,
            )
        except UnsupportedCapabilityError:
            resp = await self._call_async_or_thread(
                transport.recall,
                query=thread_tag,
                namespace=namespace,
                limit=100,
            )
        results = resp.get("results") if isinstance(resp, Mapping) else None
        if not isinstance(results, list):
            return []
        return [dict(item) for item in results if isinstance(item, Mapping)]

    async def load_entries(self, session_id: str) -> tuple[list[_StoredEntry], int]:
        namespace, thread_tag = self._resolve(session_id)
        raw_rows = await self._list_via_rest(namespace, thread_tag)
        if raw_rows is None:
            raw_rows = await self._list_via_recall(session_id, namespace, thread_tag)

        entries: list[_StoredEntry] = []
        max_seq = self._max_seq_by_session.get(session_id, 0)
        for row in raw_rows:
            content = row.get("content")
            if not isinstance(content, str):
                continue
            decoded = _decode_envelope(content)
            if decoded is None or decoded["op"] != "item":
                continue
            row_session = row.get("session_id")
            if row_session is not None and row_session != session_id:
                continue
            seq = int(decoded["seq"])
            if seq > max_seq:
                max_seq = seq
            mem_id = row.get("memory_id") or row.get("id")
            entries.append(
                _StoredEntry(
                    seq=seq,
                    memory_id=str(mem_id) if mem_id is not None else None,
                    item=decoded["item"],
                )
            )
        entries.sort(key=lambda entry: (entry.seq, entry.memory_id or ""))
        self._max_seq_by_session[session_id] = max_seq
        return entries, max_seq

    async def append_items(
        self,
        session_id: str,
        items: Sequence[tuple[int, dict[str, Any]]],
        *,
        redact_items: bool,
    ) -> None:
        namespace, thread_tag = self._resolve(session_id)
        base_ts_ms = int(time.time() * 1000)
        for seq, item in items:
            envelope = _encode_envelope(op="item", seq=seq, item=item, redact=redact_items)
            idem = make_idempotency_key("oai-session", namespace, session_id, seq, envelope)
            await self._call_async_or_thread(
                self._client.remember,
                envelope,
                tier="episodic",
                user_id=self._user_id,
                app_namespace=self._app_namespace,
                session_id=session_id,
                tags=[thread_tag, f"seq:{seq:012d}"],
                timestamp_ms=base_ts_ms + seq,
                metadata={"ebb_oai_seq": seq, "ebb_oai_session": session_id},
                idempotency_key=idem,
            )
            if seq > self._max_seq_by_session.get(session_id, 0):
                self._max_seq_by_session[session_id] = seq

    async def pop_entry(self, session_id: str, entry: _StoredEntry, next_seq: int) -> None:
        if entry.memory_id:
            await self._call_async_or_thread(
                self._client.forget,
                entry.memory_id,
                user_id=self._user_id,
                app_namespace=self._app_namespace,
            )
        if next_seq > self._max_seq_by_session.get(session_id, 0):
            self._max_seq_by_session[session_id] = next_seq

    async def clear_entries(
        self,
        session_id: str,
        entries: Sequence[_StoredEntry],
        next_seq: int,
    ) -> None:
        for entry in entries:
            if entry.memory_id:
                await self._call_async_or_thread(
                    self._client.forget,
                    entry.memory_id,
                    user_id=self._user_id,
                    app_namespace=self._app_namespace,
                )
        if next_seq > self._max_seq_by_session.get(session_id, 0):
            self._max_seq_by_session[session_id] = next_seq


McpToolCaller = Callable[[str, dict[str, Any]], Awaitable[Mapping[str, Any] | Any]]


class _StdlibMcpToolCaller:
    """Minimal Streamable HTTP JSON-RPC 2.0 caller for EbbMem MCP episodic tools."""

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str | None = None,
        api_key: str | None = None,
        profile: str = "full",
        timeout_s: float = 30.0,
    ) -> None:
        if profile not in _MCP_PROFILES:
            raise ValidationError(f"Unsupported MCP profile: {profile!r}")
        validated_url = validate_base_url(base_url)
        parsed = urllib.parse.urlsplit(validated_url)
        path = parsed.path.rstrip("/")
        if path.endswith(("/mcp", "/mcp/core", "/mcp/full", "/mcp/admin")):
            self._endpoint = validated_url
        else:
            self._endpoint = f"{validated_url}/mcp/{profile}"
        self._auth_headers = validate_auth_credentials(
            bearer_token=bearer_token,
            api_key=api_key,
        )
        self._profile = profile
        self._timeout_s = timeout_s
        self._rpc_seq = 0

    def _call_sync(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._rpc_seq += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_seq,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call",
            "Mcp-Name": tool_name,
            "X-Ebb-MCP-Profile": self._profile,
            **self._auth_headers,
        }
        req = urllib.request.Request(
            url=self._endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = redact_secrets(exc.read().decode("utf-8", errors="replace"))
            raise EbbmemApiError(
                f"EbbMem MCP HTTP {exc.code}: {detail}",
                status=exc.code,
                detail=detail,
            ) from exc
        except (OSError, http.client.HTTPException, TimeoutError) as exc:
            raise EbbmemApiError(redact_secrets(f"EbbMem MCP transport error: {exc}")) from exc

        parsed = json.loads(raw) if raw else {}
        if "error" in parsed and isinstance(parsed["error"], Mapping):
            msg = redact_secrets(str(parsed["error"].get("message") or "MCP error"))
            raise EbbmemApiError(f"EbbMem MCP error: {msg}", detail=msg)
        result = parsed.get("result")
        return dict(result) if isinstance(result, Mapping) else {}

    async def __call__(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._call_sync, tool_name, arguments)


def _extract_mcp_result_dict(raw_result: Any) -> dict[str, Any]:
    """Normalize dict or MCP CallToolResult payloads into a plain dict."""
    if isinstance(raw_result, Mapping):
        structured = raw_result.get("structuredContent") or raw_result.get("structured_content")
        if isinstance(structured, Mapping):
            return dict(structured)
        content = raw_result.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            text = first.get("text") if isinstance(first, Mapping) else getattr(first, "text", None)
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, Mapping):
                        return dict(parsed)
                except json.JSONDecodeError:
                    pass
        return dict(raw_result)

    structured_attr = getattr(raw_result, "structuredContent", None) or getattr(
        raw_result, "structured_content", None
    )
    if isinstance(structured_attr, Mapping):
        return dict(structured_attr)
    content_attr = getattr(raw_result, "content", None)
    if isinstance(content_attr, list) and content_attr:
        text = getattr(content_attr[0], "text", None)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, Mapping):
                    return dict(parsed)
            except json.JSONDecodeError:
                pass
    return {}


class McpEpisodicBackend:
    """Episodic backend over EbbMem's memory_episodic_remember / memory_episodic_tail MCP tools."""

    def __init__(
        self,
        *,
        call_tool: McpToolCaller | None = None,
        base_url: str | None = None,
        bearer_token: str | None = None,
        api_key: str | None = None,
        namespace: str | None = None,
        profile: str = "full",
        tail_limit: int = 100,
    ) -> None:
        if call_tool is None:
            if not base_url:
                raise ValueError("Provide either call_tool or base_url for McpEpisodicBackend")
            call_tool = _StdlibMcpToolCaller(
                base_url,
                bearer_token=bearer_token,
                api_key=api_key,
                profile=profile,
            )
        if tail_limit < 1 or tail_limit > 100:
            raise ValidationError("tail_limit must be between 1 and 100")
        self._call_tool = call_tool
        self._namespace = namespace
        self._tail_limit = tail_limit

    async def load_entries(self, session_id: str) -> tuple[list[_StoredEntry], int]:
        args: dict[str, Any] = {"thread_key": session_id, "limit": self._tail_limit}
        if self._namespace is not None:
            args["namespace"] = self._namespace
        raw = await self._call_tool("memory_episodic_tail", args)
        payload = _extract_mcp_result_dict(raw)
        turns = payload.get("turns") or []
        if not isinstance(turns, list):
            return [], 0

        events: list[dict[str, Any]] = []
        for turn in turns:
            if not isinstance(turn, Mapping):
                continue
            content = turn.get("content")
            if not isinstance(content, str):
                continue
            decoded = _decode_envelope(content)
            if decoded is not None:
                events.append(decoded)

        active, max_seq = _replay_log_events(events)
        return [_StoredEntry(seq=seq, memory_id=None, item=item) for seq, item in active], max_seq

    async def _remember_turn(self, session_id: str, role: str, content: str) -> None:
        args: dict[str, Any] = {
            "thread_key": session_id,
            "role": role,
            "content": content,
            "tags": [_thread_tag_for_session(session_id)],
        }
        if self._namespace is not None:
            args["namespace"] = self._namespace
        await self._call_tool("memory_episodic_remember", args)

    async def append_items(
        self,
        session_id: str,
        items: Sequence[tuple[int, dict[str, Any]]],
        *,
        redact_items: bool,
    ) -> None:
        for seq, item in items:
            envelope = _encode_envelope(op="item", seq=seq, item=item, redact=redact_items)
            await self._remember_turn(session_id, _infer_episodic_role(item), envelope)

    async def pop_entry(self, session_id: str, entry: _StoredEntry, next_seq: int) -> None:
        envelope = _encode_envelope(op="pop", seq=next_seq, target_seq=entry.seq, redact=False)
        await self._remember_turn(session_id, "system", envelope)

    async def clear_entries(
        self,
        session_id: str,
        entries: Sequence[_StoredEntry],
        next_seq: int,
    ) -> None:
        del entries
        envelope = _encode_envelope(op="clear", seq=next_seq, redact=False)
        await self._remember_turn(session_id, "system", envelope)


class EbbmemSession:
    """OpenAI Agents SDK Session implementation backed by EbbMem episodic memory.

    Implements the ``agents.memory.Session`` protocol (``get_items``, ``add_items``,
    ``pop_item``, ``clear_session``) with IC-1 scoping, secret redaction, and
    cancellation-safe mutations.
    """

    session_id: str
    session_settings: SessionSettings | None

    def __init__(
        self,
        session_id: str,
        *,
        client: AsyncIntegrationClient | IntegrationClient | None = None,
        base_url: str | None = None,
        bearer_token: str | None = None,
        api_key: str | None = None,
        scope: ScopeResolver | None = None,
        user_id: str | None = None,
        app_namespace: str | None = None,
        backend: EpisodicSessionBackend | None = None,
        mode: Literal["rest", "mcp"] = "rest",
        mcp_profile: str = "full",
        redact_items: bool = True,
        session_settings: SessionSettings | dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValidationError("session_id must be a non-empty string")
        self.session_id = session_id.strip()
        self.session_settings = (
            coerce_session_settings(session_settings)
            if session_settings is not None
            else SessionSettings()
        )
        self.redact_items = redact_items
        self._lock = asyncio.Lock()

        if backend is not None:
            self._backend = backend
        elif mode == "mcp":
            resolved_ns = (
                scope.resolve_namespace(user_id=user_id, app_namespace=app_namespace)
                if scope is not None
                else app_namespace
            )
            self._backend = McpEpisodicBackend(
                base_url=base_url,
                bearer_token=bearer_token,
                api_key=api_key,
                namespace=resolved_ns,
                profile=mcp_profile,
            )
        elif mode == "rest":
            effective_client = client
            if effective_client is None:
                if not base_url:
                    raise ValueError("Provide backend, client, or base_url for EbbmemSession")
                effective_scope = scope
                if effective_scope is None:
                    if app_namespace or user_id:
                        effective_scope = ScopeResolver(
                            user_id=user_id,
                            app_namespace=app_namespace,
                        )
                    else:
                        effective_scope = ScopeResolver(app_namespace="default:u:default")
                effective_client = AsyncIntegrationClient(
                    base_url,
                    bearer_token=bearer_token,
                    api_key=api_key,
                    scope=effective_scope,
                    fail_open=False,
                )
            self._backend = RestEpisodicBackend(
                effective_client,
                user_id=user_id,
                app_namespace=app_namespace,
            )
        else:
            raise ValidationError(f"Unsupported session mode: {mode!r}")

    @classmethod
    def from_mcp(
        cls,
        session_id: str,
        *,
        call_tool: McpToolCaller | None = None,
        base_url: str | None = None,
        bearer_token: str | None = None,
        api_key: str | None = None,
        namespace: str | None = None,
        profile: str = "full",
        tail_limit: int = 100,
        redact_items: bool = True,
        session_settings: SessionSettings | dict[str, Any] | None = None,
    ) -> EbbmemSession:
        """Create a session backed by MCP memory_episodic_remember / memory_episodic_tail."""
        backend = McpEpisodicBackend(
            call_tool=call_tool,
            base_url=base_url,
            bearer_token=bearer_token,
            api_key=api_key,
            namespace=namespace,
            profile=profile,
            tail_limit=tail_limit,
        )
        return cls(
            session_id,
            backend=backend,
            redact_items=redact_items,
            session_settings=session_settings,
        )

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve conversation items for this session in chronological order."""
        session_limit = resolve_session_limit(limit, self.session_settings)
        if session_limit is not None and session_limit <= 0:
            return []

        async with self._lock:
            entries, _ = await self._backend.load_entries(self.session_id)

        items: list[TResponseInputItem] = [copy.deepcopy(entry.item) for entry in entries]  # type: ignore[misc]
        if session_limit is not None:
            return items[-session_limit:]
        return items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Append new items to the session's episodic thread log."""
        if not items:
            return

        async def _op() -> None:
            async with self._lock:
                _, max_seq = await self._backend.load_entries(self.session_id)
                sequenced: list[tuple[int, dict[str, Any]]] = []
                seq = max_seq
                for raw_item in items:
                    seq += 1
                    sequenced.append((seq, copy.deepcopy(dict(raw_item))))
                await self._backend.append_items(
                    self.session_id,
                    sequenced,
                    redact_items=self.redact_items,
                )

        await await_session_mutation(_op())

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session, or None if empty."""

        async def _op() -> TResponseInputItem | None:
            async with self._lock:
                entries, max_seq = await self._backend.load_entries(self.session_id)
                if not entries:
                    return None
                last = entries[-1]
                await self._backend.pop_entry(self.session_id, last, max_seq + 1)
                return copy.deepcopy(last.item)  # type: ignore[return-value]

        return await await_session_mutation(_op())

    async def clear_session(self) -> None:
        """Clear all items for this session."""

        async def _op() -> None:
            async with self._lock:
                entries, max_seq = await self._backend.load_entries(self.session_id)
                if not entries:
                    return
                await self._backend.clear_entries(self.session_id, entries, max_seq + 1)

        await await_session_mutation(_op())
