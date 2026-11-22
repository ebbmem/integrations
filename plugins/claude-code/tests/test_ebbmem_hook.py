"""Tests for EbbMem Claude Code lifecycle hooks."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

HOOK_PATH = Path(__file__).resolve().parents[1] / "hooks" / "ebbmem_hook.py"


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("ebbmem_claude_hook", HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ebbmem_claude_hook"] = module
    spec.loader.exec_module(module)
    return module


class _MockMcpState:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.fail_requests = False
        self.use_sse = False
        self.recall_results: list[dict[str, Any]] = [
            {
                "id": "mem-1",
                "memory_id": "mem-1",
                "tier": "semantic",
                "content": "Project uses ruff for Python linting and pytest for unit tests.",
                "score": 0.94,
            },
            {
                "id": "mem-2",
                "memory_id": "mem-2",
                "tier": "semantic",
                "content": "Deployment runs via Caddy reverse proxy on port 443.",
                "score": 0.81,
            },
        ]
        self.recall_id = "11111111-2222-3333-4444-555555555555"


def _start_mock_mcp_server():
    state = _MockMcpState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            if state.fail_requests:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"error":"unavailable"}')
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            req = json.loads(raw)
            self.headers_captured = {k.lower(): v for k, v in self.headers.items()}
            state.headers.append(self.headers_captured)
            params = req.get("params") or {}
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            state.calls.append({"name": tool_name, "arguments": arguments})

            if tool_name == "memory_recall":
                structured = {
                    "query": arguments.get("query", ""),
                    "count": len(state.recall_results),
                    "recall_id": state.recall_id,
                    "results": state.recall_results,
                }
            elif tool_name == "memory_remember":
                structured = {
                    "id": "mem-new",
                    "memory_id": "mem-new",
                    "namespace": arguments.get("namespace", "acme:u:alice"),
                    "tier": arguments.get("tier", "episodic"),
                    "storage_strength": 1.0,
                }
            elif tool_name == "memory_reinforce":
                structured = {
                    "status": "ok",
                    "outcome": "counted",
                    "memory_id": arguments.get("memory_id", ""),
                    "delta": arguments.get("delta", 0.1),
                    "message": "reinforced",
                }
            else:
                structured = {}

            rpc_resp = {
                "jsonrpc": "2.0",
                "id": req.get("id", 1),
                "result": {"structuredContent": structured, "content": []},
            }
            if state.use_sse:
                body = f"event: message\ndata: {json.dumps(rpc_resp)}\n\n".encode()
                ctype = "text/event-stream"
            else:
                body = json.dumps(rpc_resp).encode("utf-8")
                ctype = "application/json"

            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, state


def test_validate_endpoint_url_rejects_query_credentials_and_userinfo() -> None:
    mod = _load_hook_module()
    assert mod.validate_endpoint_url("https://ebbmem.example.com/mcp/core") == (
        "https://ebbmem.example.com/mcp/core"
    )
    for bad in [
        "https://ebbmem.example.com/mcp/core?token=abc",
        "https://ebbmem.example.com/mcp/core?api_key=abc",
        "https://user:pass@ebbmem.example.com/mcp/core",
        "ftp://ebbmem.example.com/mcp/core",
    ]:
        with pytest.raises(ValueError):
            mod.validate_endpoint_url(bad)


def test_redact_secrets_masks_tokens_and_keys() -> None:
    mod = _load_hook_module()
    text = (
        "Configured Authorization: Bearer supersecret_token_12345 and "
        "ebb_secret_key_abcdef123456 with api_key=my_raw_secret"
    )
    redacted = mod.redact_secrets(text)
    assert "supersecret_token_12345" not in redacted
    assert "ebb_secret_key_abcdef123456" not in redacted
    assert "my_raw_secret" not in redacted


def test_session_start_and_stop_recall_capture_and_selective_reinforce(
    tmp_path: Path,
) -> None:
    mod = _load_hook_module()
    server, state = _start_mock_mcp_server()
    try:
        port = server.server_address[1]
        env = {
            "EBBMEM_MCP_URL": f"http://127.0.0.1:{port}/mcp/core",
            "EBBMEM_API_KEY": "ebb_test_key_only",
            "EBBMEM_NAMESPACE": "acme:u:alice",
        }
        start_out = mod.handle_session_start(
            {"session_id": "sess-1", "cwd": "/tmp/demo-project"},
            env=env,
            state_dir=tmp_path,
        )
        assert start_out is not None
        hook_out = start_out["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "SessionStart"
        ctx = hook_out["additionalContext"]
        assert ctx.startswith("<ebbmem_untrusted_memory_context>")
        assert ctx.endswith("</ebbmem_untrusted_memory_context>")
        assert "ruff" in ctx
        assert state.calls[0]["name"] == "memory_recall"
        assert state.headers[0]["authorization"] == "Bearer ebb_test_key_only"
        assert state.headers[0]["x-ebb-namespace"] == "acme:u:alice"

        # Stop uses mem-1 (ruff & pytest) ONLY, not mem-2 (Caddy), and includes a raw secret to redact
        stop_res = mod.handle_stop(
            {
                "session_id": "sess-1",
                "stop_hook_active": False,
                "last_assistant_message": (
                    "I ran ruff and pytest checks cleanly. "
                    "Do not persist sk-live-1234567890abcdef."
                ),
            },
            env=env,
            state_dir=tmp_path,
        )
        assert stop_res["captured"] is True
        assert stop_res["reinforced_ids"] == ["mem-1"]

        remember_call = next(c for c in state.calls if c["name"] == "memory_remember")
        assert "sk-live-1234567890abcdef" not in remember_call["arguments"]["content"]
        assert remember_call["arguments"]["tier"] == "episodic"
        assert remember_call["arguments"]["session_id"] == "sess-1"
        assert remember_call["arguments"]["idempotency_key"].startswith("claude-hook-")

        reinforce_call = next(c for c in state.calls if c["name"] == "memory_reinforce")
        assert reinforce_call["arguments"]["memory_id"] == "mem-1"
        assert "used_ids" not in reinforce_call["arguments"]
        assert reinforce_call["arguments"]["recall_id"] == state.recall_id
    finally:
        server.shutdown()
        server.server_close()


def test_stop_skips_when_stop_hook_active_and_handles_sse(tmp_path: Path) -> None:
    mod = _load_hook_module()
    server, state = _start_mock_mcp_server()
    state.use_sse = True
    try:
        port = server.server_address[1]
        env = {
            "EBBMEM_MCP_URL": f"http://127.0.0.1:{port}/mcp/core",
            "EBBMEM_API_KEY": "ebb_test_key_only",
        }
        skipped = mod.handle_stop(
            {
                "session_id": "sess-2",
                "stop_hook_active": True,
                "last_assistant_message": "done",
            },
            env=env,
            state_dir=tmp_path,
        )
        assert skipped == {"skipped": "stop_hook_active"}
        assert state.calls == []

        res = mod.handle_session_start(
            {"session_id": "sess-2", "cwd": "/tmp/app"},
            env=env,
            state_dir=tmp_path,
        )
        assert res is not None
        assert "Caddy" in res["hookSpecificOutput"]["additionalContext"]
    finally:
        server.shutdown()
        server.server_close()


def test_main_fails_open_when_server_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_hook_module()
    server, state = _start_mock_mcp_server()
    state.fail_requests = True
    try:
        port = server.server_address[1]
        monkeypatch.setenv("EBBMEM_MCP_URL", f"http://127.0.0.1:{port}/mcp/core")
        monkeypatch.setenv("EBBMEM_API_KEY", "ebb_test_key_only")
        monkeypatch.setattr(
            sys, "stdin", io.StringIO('{"session_id":"s1","cwd":"/tmp"}')
        )
        stderr_buf = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr_buf)
        assert mod.main(["session-start"]) == 0
        assert "fail-open" in stderr_buf.getvalue()
    finally:
        server.shutdown()
        server.server_close()
