"""MCP JSON-RPC request handling and tool dispatch for MockEbbmemServer."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from ebbmem_conformance.mock_memory_ops import (
    create_memory,
    execute_recall,
    execute_reinforce,
    get_status,
    serialize_memory,
)
from ebbmem_conformance.mock_state import (
    LOCAL_ORIGINS,
    SUPPORTED_MCP_VERSIONS,
    Identity,
    _parse_int,
)


def handle_mcp_request(handler: Any, path: str) -> None:
    origin = handler.headers.get("Origin")
    if origin is not None:
        try:
            parsed_origin = urlsplit(origin)
        except ValueError:
            handler._send_json(403, {"detail": "untrusted_origin"}, record_path=path)
            return
        host = (parsed_origin.hostname or "").lower()
        if host not in LOCAL_ORIGINS:
            handler._send_json(403, {"detail": "untrusted_origin"}, record_path=path)
            return

    identity = handler._authenticate(path)
    if identity is None:
        return

    path_profile: str | None = None
    if path == "/mcp/core":
        path_profile = "core"
    elif path == "/mcp/full":
        path_profile = "full"
    elif path == "/mcp/admin":
        path_profile = "admin"

    header_profile = handler.headers.get("X-Ebb-MCP-Profile")
    valid_profiles = {"core", "full", "admin"}
    if header_profile is not None and header_profile not in valid_profiles:
        handler._send_json(400, {"detail": "invalid_profile"}, record_path=path)
        return
    if path_profile is not None and header_profile is not None and path_profile != header_profile:
        handler._send_json(400, {"detail": "profile_conflict"}, record_path=path)
        return
    profile = path_profile or header_profile or "core"

    req, err = handler._read_json_body()
    if err:
        handler._send_json(
            400,
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            record_path=path,
        )
        return
    if isinstance(req, list):
        handler._send_json(
            400,
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: batching unsupported"},
            },
            record_path=path,
        )
        return
    if not isinstance(req, dict):
        handler._send_json(
            400,
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            },
            record_path=path,
        )
        return

    rpc_id = req.get("id")
    method = req.get("method")
    if "params" in req and not isinstance(req["params"], dict):
        handler._send_json(
            400,
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32602, "message": "Invalid params"},
            },
            record_path=path,
        )
        return
    params = req.get("params") if "params" in req else {}
    if not isinstance(method, str):
        handler._send_json(
            400,
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32600, "message": "Invalid Request"},
            },
            record_path=path,
        )
        return

    header_version = handler.headers.get("MCP-Protocol-Version")
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    meta_version = meta.get("io.modelcontextprotocol/protocolVersion")
    init_version = params.get("protocolVersion") if method == "initialize" else None

    if header_version and meta_version and header_version != meta_version:
        handler._send_json(
            400,
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32020, "message": "MCP-Protocol-Version mismatch"},
            },
            record_path=path,
        )
        return

    version = header_version or meta_version or init_version or "2026-07-28"
    if version not in SUPPORTED_MCP_VERSIONS:
        handler._send_json(
            400,
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {
                    "code": -32022,
                    "message": f"Unsupported protocol version: {version}",
                },
            },
            record_path=path,
        )
        return

    mcp_method_hdr = handler.headers.get("Mcp-Method")
    if mcp_method_hdr is not None and mcp_method_hdr != method:
        handler._send_json(
            400,
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32020, "message": "Mcp-Method header mismatch"},
            },
            record_path=path,
        )
        return

    mcp_name_hdr = handler.headers.get("Mcp-Name")
    if mcp_name_hdr is not None and method == "tools/call":
        if mcp_name_hdr != params.get("name"):
            handler._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32020, "message": "Mcp-Name header mismatch"},
                },
                record_path=path,
            )
            return

    resp_headers = {"MCP-Protocol-Version": version}
    golden = handler.state.golden_surface

    if method == "ping":
        handler._send_json(
            200,
            {"jsonrpc": "2.0", "id": rpc_id, "result": {}},
            extra_headers=resp_headers,
            record_path=path,
        )
        return

    if method == "initialize":
        handler._send_json(
            200,
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "ebbmem-mock", "version": "0.1.0"},
                },
            },
            extra_headers=resp_headers,
            record_path=path,
        )
        return

    if method == "notifications/initialized":
        handler._send_json(
            200,
            {"jsonrpc": "2.0", "id": rpc_id, "result": {"acknowledged": True}},
            extra_headers=resp_headers,
            record_path=path,
        )
        return

    if method == "server/discover":
        handler._send_json(
            200,
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "protocolVersion": version,
                    "profile": profile,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "ebbmem-mock", "version": "0.1.0"},
                    "ttlMs": 60_000,
                    "cacheScope": "private",
                },
            },
            extra_headers=resp_headers,
            record_path=path,
        )
        return

    if method == "tools/list":
        tool_names = list(golden["profiles"][profile])
        admin_tools = set(golden["admin_only_tools"])
        if identity.role != "admin":
            tool_names = [name for name in tool_names if name not in admin_tools]
        tools = [golden["tools"][name] for name in tool_names]
        result: dict[str, Any] = {"tools": tools}
        if version == "2026-07-28":
            result["ttlMs"] = 60_000
            result["cacheScope"] = "private"
        handler._send_json(
            200,
            {"jsonrpc": "2.0", "id": rpc_id, "result": result},
            extra_headers=resp_headers,
            record_path=path,
        )
        return

    if method == "tools/call":
        tool_name = params.get("name")
        if "arguments" in params and not isinstance(params["arguments"], dict):
            handler._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32602, "message": "Invalid params"},
                },
                extra_headers=resp_headers,
                record_path=path,
            )
            return
        arguments = params.get("arguments") if "arguments" in params else {}
        if not isinstance(tool_name, str):
            handler._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32602, "message": "Invalid params"},
                },
                extra_headers=resp_headers,
                record_path=path,
            )
            return

        admin_tools = set(golden["admin_only_tools"])
        if tool_name in admin_tools and identity.role != "admin":
            handler._send_json(
                403,
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32003, "message": "Admin role required"},
                },
                extra_headers=resp_headers,
                record_path=path,
            )
            return

        allowed_for_profile = set(golden["profiles"][profile]) | set(golden["universal_aliases"])
        if tool_name not in golden["tools"] or tool_name not in allowed_for_profile:
            handler._send_json(
                404,
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32601, "message": f"Tool not found: {tool_name}"},
                },
                extra_headers=resp_headers,
                record_path=path,
            )
            return

        ns = handler._resolve_namespace(identity, arguments.get("namespace"), path)
        if ns is None:
            return

        status, call_result, rpc_err = execute_mcp_tool(
            handler=handler,
            identity=identity,
            namespace=ns,
            tool_name=tool_name,
            arguments=arguments,
            version=version,
        )
        if rpc_err is not None:
            handler._send_json(
                status,
                {"jsonrpc": "2.0", "id": rpc_id, "error": rpc_err},
                extra_headers=resp_headers,
                record_path=path,
            )
            return
        handler._send_json(
            200,
            {"jsonrpc": "2.0", "id": rpc_id, "result": call_result},
            extra_headers=resp_headers,
            record_path=path,
        )
        return

    handler._send_json(
        404,
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        },
        extra_headers=resp_headers,
        record_path=path,
    )


def execute_mcp_tool(
    handler: Any,
    identity: Identity,
    namespace: str,
    tool_name: str,
    arguments: dict[str, Any],
    version: str,
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    state = handler.state
    structured: dict[str, Any]
    if tool_name in {"memory_remember", "memory_episodic_remember"}:
        args = dict(arguments)
        if tool_name == "memory_episodic_remember":
            args["tier"] = "episodic"
        status, payload, _ = create_memory(state, identity, namespace, args)
        if status >= 400:
            code = -32602 if status == 422 else -32000
            return status, None, {"code": code, "message": str(payload.get("detail"))}
        structured = payload
    elif tool_name in {"memory_recall", "memory_search", "memory_context_pack"}:
        raw_query = arguments.get("query", "")
        if not isinstance(raw_query, str):
            return 400, None, {"code": -32602, "message": "Invalid params: query"}
        limit = _parse_int(arguments.get("limit", 10))
        if limit is None:
            return 400, None, {"code": -32602, "message": "Invalid params: limit"}
        tier_filter = arguments.get("tier")
        if tier_filter is not None and not isinstance(tier_filter, str):
            return 400, None, {"code": -32602, "message": "Invalid params: tier"}
        tags_filter = arguments.get("tags")
        if tags_filter is not None and (
            not isinstance(tags_filter, list) or not all(isinstance(t, str) for t in tags_filter)
        ):
            return 400, None, {"code": -32602, "message": "Invalid params: tags"}
        session_filter = arguments.get("session_id")
        if session_filter is not None and not isinstance(session_filter, str):
            return 400, None, {"code": -32602, "message": "Invalid params: session_id"}
        structured = execute_recall(
            state=state,
            namespace=namespace,
            query_text=raw_query,
            limit=max(1, min(100, limit)),
            tier_filter=tier_filter,
            tags_filter=tags_filter,
            session_filter=session_filter,
            hex_recall_id=True,
        )
    elif tool_name == "memory_get":
        mem_id = str(arguments.get("memory_id", ""))
        with state.lock:
            item = state.memories.get(namespace, {}).get(mem_id)
            if item is None or item.tombstoned:
                return 404, None, {"code": -32004, "message": "Memory not found"}
            structured = serialize_memory(item)
    elif tool_name == "memory_reinforce":
        status, payload = execute_reinforce(state, identity, namespace, arguments)
        if status >= 400:
            code = -32602 if status == 422 else -32000
            return status, None, {"code": code, "message": str(payload.get("detail"))}
        structured = payload
    elif tool_name == "memory_forget":
        if identity.role == "readonly":
            return 403, None, {"code": -32003, "message": "Readonly key cannot forget"}
        mem_id = str(arguments.get("memory_id", ""))
        with state.lock:
            item = state.memories.get(namespace, {}).get(mem_id)
            if item is None:
                return 404, None, {"code": -32004, "message": "Memory not found"}
            item.tombstoned = True
        structured = {"status": "forgotten", "memory_id": mem_id, "tombstoned": True}
    elif tool_name == "memory_purge":
        if identity.role != "admin":
            return 403, None, {"code": -32003, "message": "Admin role required"}
        mem_id = str(arguments.get("memory_id", ""))
        with state.lock:
            item = state.memories.get(namespace, {}).pop(mem_id, None)
            if item is None:
                return 404, None, {"code": -32004, "message": "Memory not found"}
            for (rec_ns, _k), rec in state.idempotency.items():
                if rec_ns == namespace and rec.memory_id == mem_id:
                    rec.purged = True
        structured = {"status": "purged", "memory_id": mem_id, "irreversible": True}
    elif tool_name == "memory_status":
        structured = get_status(state, namespace)
    else:
        structured = {"status": "ok", "tool": tool_name, "namespace": namespace}

    result: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(structured)}],
    }
    if tool_name in state.golden_surface["output_schema_tools"]:
        result["structuredContent"] = structured
    if version == "2026-07-28":
        result["resultType"] = "complete"
    return 200, result, None
