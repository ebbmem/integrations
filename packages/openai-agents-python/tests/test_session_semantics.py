"""Session semantics tests for EbbmemSession mirroring the OpenAI Agents SDK Session tests."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from agents import Agent, Model, ModelProvider, ModelResponse, RunConfig, Runner, Usage
from agents.items import ResponseOutputMessage, ResponseOutputText, TResponseInputItem
from agents.memory import Session, SessionSettings
from agents.models.interface import ModelTracing

try:
    from ebbmem_conformance.mock_server import MockEbbmemServer
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "conformance" / "src"))
    from ebbmem_conformance.mock_server import MockEbbmemServer

from ebbmem_integration_core import (
    AsyncIntegrationClient,
    IntegrationClient,
    RetryPolicy,
    ScopeResolver,
    ValidationError,
)

from ebbmem_openai_agents import EbbmemSession, McpEpisodicBackend


def _fast_retry_policy() -> RetryPolicy:
    return RetryPolicy(
        timeout_s=2.0,
        max_retries=2,
        backoff_base_s=0.001,
        backoff_cap_s=0.005,
        jitter_fn=lambda _a, _b: 0.0,
    )


class _InMemoryMcpEpisodicStub:
    """Faithful mock of EbbMem's memory_episodic_remember & memory_episodic_tail MCP tools."""

    def __init__(self) -> None:
        self.threads: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.seq = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool_name, dict(arguments)))
        ns = str(arguments.get("namespace") or "default")
        thread_key = str(arguments.get("thread_key") or "")
        if not thread_key:
            raise ValueError("thread_key required")

        if tool_name == "memory_episodic_remember":
            role = str(arguments["role"])
            content = str(arguments["content"])
            self.seq += 1
            turn = {
                "role": role,
                "content": content,
                "sequence_id": self.seq,
                "thread_key": thread_key,
            }
            self.threads.setdefault((ns, thread_key), []).append(turn)
            return {
                "status": "remembered",
                "sequence_id": self.seq,
                "thread_key": thread_key,
                "role": role,
                "message_count": len(self.threads[(ns, thread_key)]),
            }

        if tool_name == "memory_episodic_tail":
            limit = int(arguments.get("limit", 10))
            turns = self.threads.get((ns, thread_key), [])
            sliced = turns[-limit:] if limit > 0 else []
            return {
                "status": "ok",
                "thread_key": thread_key,
                "count": len(sliced),
                "turns": [dict(t) for t in sliced],
            }

        raise ValueError(f"Unknown tool: {tool_name}")


class _MockHttpMcpAndTableServer:
    """HTTP server exposing /mcp/full (episodic tools) and /api/v1/memory (cursor pagination)."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.mcp_stub = _InMemoryMcpEpisodicStub()
        self.rest_records: list[dict[str, Any]] = []
        self.seq = 0
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self.httpd is not None
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> _MockHttpMcpAndTableServer:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _send(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                if parsed.path != "/api/v1/memory":
                    self._send(404, {"detail": "Not Found"})
                    return
                qs = parse_qs(parsed.query)
                ns = qs.get("namespace", ["default"])[0]
                tag = qs.get("tags", [None])[0]
                cursor = int(qs.get("cursor", ["0"])[0])
                with parent.lock:
                    matching = [
                        r
                        for r in parent.rest_records
                        if not r["tombstoned"]
                        and r["namespace"] == ns
                        and (tag is None or tag in r["tags"])
                    ]
                # Page size of 2 exercises multi-page cursor pagination.
                page = matching[cursor : cursor + 2]
                next_cur = str(cursor + 2) if cursor + 2 < len(matching) else None
                self._send(200, {"items": page, "next_cursor": next_cur})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                parsed = urlsplit(self.path)
                if parsed.path == "/api/v1/memory/remember":
                    with parent.lock:
                        parent.seq += 1
                        mem_id = f"mem-{parent.seq}"
                        rec = {
                            "id": mem_id,
                            "memory_id": mem_id,
                            "namespace": body.get("namespace", "default"),
                            "content": body["content"],
                            "tier": body.get("tier", "episodic"),
                            "tags": body.get("tags", []),
                            "session_id": body.get("session_id"),
                            "tombstoned": False,
                            "storage_strength": 1.0,
                            "replayed": False,
                        }
                        parent.rest_records.append(rec)
                    self._send(201, rec)
                    return

                if parsed.path == "/mcp/full":
                    params = body.get("params", {})
                    tool_name = params["name"]
                    args = params.get("arguments", {})
                    with parent.lock:
                        result = asyncio.run(parent.mcp_stub.call_tool(tool_name, args))
                    self._send(
                        200,
                        {
                            "jsonrpc": "2.0",
                            "id": body.get("id"),
                            "result": {
                                "content": [{"type": "text", "text": json.dumps(result)}],
                                "structuredContent": result,
                            },
                        },
                    )
                    return

                self._send(404, {"detail": "Not Found"})

            def do_DELETE(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                if not parsed.path.startswith("/api/v1/memory/"):
                    self._send(404, {"detail": "Not Found"})
                    return
                mem_id = parsed.path[len("/api/v1/memory/") :]
                with parent.lock:
                    for rec in parent.rest_records:
                        if rec["id"] == mem_id:
                            rec["tombstoned"] = True
                            self._send(200, {"status": "forgotten", "memory_id": mem_id})
                            return
                self._send(404, {"detail": "Not Found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5.0)


class _RecordingEchoModel(Model):
    """Deterministic local fake Model that records received inputs without OpenAI API calls."""

    def __init__(self) -> None:
        self.received_inputs: list[list[Any]] = []

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any,
    ) -> ModelResponse:
        del system_instructions, model_settings, tools, output_schema, handoffs, tracing
        del previous_response_id, conversation_id, prompt
        items = list(input) if isinstance(input, list) else [{"role": "user", "content": input}]
        self.received_inputs.append(items)
        last_text = ""
        for entry in reversed(items):
            if isinstance(entry, dict) and entry.get("role") == "user":
                last_text = str(entry.get("content", ""))
                break
        msg = ResponseOutputMessage(
            id=f"msg-{len(self.received_inputs)}",
            type="message",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText(
                    type="output_text",
                    text=f"seen_items={len(items)} reply_to={last_text}",
                    annotations=[],
                )
            ],
        )
        return ModelResponse(output=[msg], usage=Usage(), response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class _StaticProvider(ModelProvider):
    def __init__(self, model: Model) -> None:
        self._model = model

    def get_model(self, model_name: str | None) -> Model:
        del model_name
        return self._model


def _make_rest_session(
    server: MockEbbmemServer,
    session_id: str,
    *,
    session_settings: SessionSettings | dict[str, Any] | None = None,
) -> EbbmemSession:
    client = AsyncIntegrationClient(
        server.base_url,
        api_key="ebb_test_alice_key",
        scope=ScopeResolver(tenant="acme", user_id="alice"),
        retry_policy=_fast_retry_policy(),
        fail_open=False,
    )
    return EbbmemSession(session_id, client=client, session_settings=session_settings)


async def _run_core_session_semantics_suite(
    factory: Any,
) -> None:
    """Mirror OpenAI Agents SDK Session semantics suite against any EbbmemSession factory."""
    s1 = factory("sess-alpha")
    assert isinstance(s1, Session)
    assert s1.session_id == "sess-alpha"

    # Empty session behavior
    assert await s1.get_items() == []
    assert await s1.pop_item() is None
    await s1.add_items([])
    assert await s1.get_items() == []

    # Chronological add_items + get_items
    items: list[TResponseInputItem] = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "Remember my token=sk-secret-value-123456"},
        {"role": "assistant", "content": "Noted safely."},
    ]
    await s1.add_items(items[:2])
    await s1.add_items(items[2:])

    got_all = await s1.get_items()
    assert len(got_all) == 4
    assert got_all[0]["content"] == "Hello"
    assert got_all[1]["content"] == "Hi there!"
    assert "sk-secret-value-123456" not in str(got_all[2]["content"])
    assert "[REDACTED]" in str(got_all[2]["content"])
    assert got_all[3]["content"] == "Noted safely."

    # Limit semantics: positive limit returns latest N in chronological order; <=0 returns []
    assert [it["content"] for it in await s1.get_items(limit=2)] == [
        got_all[2]["content"],
        "Noted safely.",
    ]
    assert await s1.get_items(limit=0) == []
    assert await s1.get_items(limit=-3) == []

    # Defensive copy: mutating returned items does not mutate stored history
    got_all[0]["content"] = "MUTATED"
    refetched = await s1.get_items()
    assert refetched[0]["content"] == "Hello"

    # Cross-instance persistence for the same session_id
    s1_reopened = factory("sess-alpha")
    assert len(await s1_reopened.get_items()) == 4

    # Session isolation across different session_ids
    s_other = factory("sess-beta")
    await s_other.add_items([{"role": "user", "content": "Other session"}])
    assert len(await s_other.get_items()) == 1
    assert len(await s1.get_items()) == 4

    # pop_item removes and returns most recent item
    popped = await s1_reopened.pop_item()
    assert popped is not None and popped["content"] == "Noted safely."
    remaining = await s1.get_items()
    assert len(remaining) == 3
    assert remaining[-1]["content"] == got_all[2]["content"]

    # clear_session clears only target session
    await s1.clear_session()
    assert await s1.get_items() == []
    assert await s1.pop_item() is None
    assert len(await s_other.get_items()) == 1


def test_session_semantics_rest_against_mock_ebbmem_server() -> None:
    async def run() -> None:
        with MockEbbmemServer() as server:
            await _run_core_session_semantics_suite(
                lambda sid: _make_rest_session(server, sid),
            )

    asyncio.run(run())


def test_session_semantics_mcp_backend_and_http_mcp() -> None:
    async def run() -> None:
        stub = _InMemoryMcpEpisodicStub()
        await _run_core_session_semantics_suite(
            lambda sid: EbbmemSession(
                sid,
                backend=McpEpisodicBackend(call_tool=stub.call_tool, namespace="acme:u:alice"),
            )
        )
        # Verify MCP episodic tool names were actually invoked.
        called_tools = {name for name, _ in stub.calls}
        assert called_tools == {"memory_episodic_remember", "memory_episodic_tail"}

        # Also test EbbmemSession.from_mcp over Streamable HTTP against a local HTTP MCP server.
        with _MockHttpMcpAndTableServer() as http_server:
            s_http = EbbmemSession.from_mcp(
                "http-thread-1",
                base_url=http_server.base_url,
                api_key="ebb_test_alice_key",
                namespace="acme:u:alice",
                profile="full",
            )
            await s_http.add_items(
                [
                    {"role": "user", "content": "First turn"},
                    {"role": "assistant", "content": "Second turn"},
                ]
            )
            reopened = EbbmemSession.from_mcp(
                "http-thread-1",
                base_url=http_server.base_url,
                api_key="ebb_test_alice_key",
                namespace="acme:u:alice",
                profile="full",
            )
            loaded = await reopened.get_items()
            assert [it["content"] for it in loaded] == ["First turn", "Second turn"]
            popped = await reopened.pop_item()
            assert popped == {"role": "assistant", "content": "Second turn"}
            await reopened.clear_session()
            assert await reopened.get_items() == []

    asyncio.run(run())


def test_rest_session_cursor_pagination_and_session_settings() -> None:
    async def run() -> None:
        with _MockHttpMcpAndTableServer() as srv:
            client = AsyncIntegrationClient(
                srv.base_url,
                api_key="ebb_test_alice_key",
                scope=ScopeResolver(tenant="acme", user_id="alice"),
                retry_policy=_fast_retry_policy(),
                fail_open=False,
            )
            # Default SessionSettings(limit=2) returns latest 2 unless overridden.
            session = EbbmemSession(
                "paginated-sess",
                client=client,
                session_settings=SessionSettings(limit=2),
            )
            await session.add_items([{"role": "user", "content": f"msg-{i}"} for i in range(5)])
            default_limited = await session.get_items()
            assert [m["content"] for m in default_limited] == ["msg-3", "msg-4"]
            explicit_more = await session.get_items(limit=4)
            assert [m["content"] for m in explicit_more] == ["msg-1", "msg-2", "msg-3", "msg-4"]
            assert await session.get_items(limit=0) == []

    asyncio.run(run())


def test_runner_multi_turn_with_ebbmem_session_and_mocked_model() -> None:
    async def run() -> None:
        with MockEbbmemServer() as server:
            session = _make_rest_session(server, "runner-thread-1")
            model = _RecordingEchoModel()
            agent = Agent(name="EchoAgent", instructions="Be helpful.")
            run_config = RunConfig(model_provider=_StaticProvider(model), tracing_disabled=True)

            r1 = await Runner.run(agent, "First question", session=session, run_config=run_config)
            assert "seen_items=1" in str(r1.final_output)

            r2 = await Runner.run(agent, "Second question", session=session, run_config=run_config)
            # Second run must include user1 + assistant1 + user2 = 3 items from EbbmemSession.
            assert "seen_items=3" in str(r2.final_output)

            history = await session.get_items()
            assert len(history) == 4
            assert history[0]["content"] == "First question"
            assert history[2]["content"] == "Second question"

    asyncio.run(run())


def test_session_sync_integration_client_wrapper() -> None:
    async def run() -> None:
        with MockEbbmemServer() as server:
            sync_client = IntegrationClient(
                server.base_url,
                api_key="ebb_test_alice_key",
                scope=ScopeResolver(tenant="acme", user_id="alice"),
                retry_policy=_fast_retry_policy(),
                fail_open=False,
            )
            session = EbbmemSession("sync-client-sess", client=sync_client)
            await session.add_items([{"role": "user", "content": "sync hello"}])
            items = await session.get_items()
            assert items == [{"role": "user", "content": "sync hello"}]

    asyncio.run(run())


def test_session_validation_errors() -> None:
    with pytest.raises(ValidationError):
        EbbmemSession("   ", base_url="http://127.0.0.1:8320", api_key="ebb_test_alice_key")
    with pytest.raises(ValidationError):
        McpEpisodicBackend(base_url="http://127.0.0.1:8320", api_key="ebb_k", tail_limit=101)
