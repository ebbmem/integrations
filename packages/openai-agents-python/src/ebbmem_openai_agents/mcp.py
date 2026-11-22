"""Helpers for attaching EbbMem MCP servers to OpenAI Agents SDK agents."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Literal

from agents.mcp import (
    MCPServerStdio,
    MCPServerStdioParams,
    MCPServerStreamableHttp,
    MCPServerStreamableHttpParams,
    create_static_tool_filter,
)
from ebbmem_integration_core import (
    ValidationError,
    validate_auth_credentials,
    validate_base_url,
    validate_namespace,
)

MCPProfile = Literal["core", "full", "admin"]


def ebbmem_mcp_server(
    base_url: str,
    *,
    bearer_token: str | None = None,
    api_key: str | None = None,
    profile: MCPProfile = "core",
    namespace: str | None = None,
    name: str | None = None,
    timeout: float | timedelta = 10.0,
    sse_read_timeout: float | timedelta = 300.0,
    client_session_timeout_seconds: float | None = 10.0,
    cache_tools_list: bool = True,
    use_structured_content: bool = False,
    allowed_tools: Sequence[str] | None = None,
    blocked_tools: Sequence[str] | None = None,
    require_approval: Any = None,
    headers: dict[str, str] | None = None,
) -> MCPServerStreamableHttp:
    """Create a configured ``MCPServerStreamableHttp`` pointing at an EbbMem ``/mcp`` endpoint.

    Enforces IC-1 header-only credentials (rejecting URL userinfo or query credentials) and
    never sends both ``bearer_token`` and ``api_key`` simultaneously.
    """
    if profile not in {"core", "full", "admin"}:
        raise ValidationError(f"Unsupported MCP profile: {profile!r}")

    clean_base = validate_base_url(base_url)
    auth_headers = validate_auth_credentials(bearer_token=bearer_token, api_key=api_key)

    req_headers: dict[str, str] = dict(headers or {})
    req_headers.update(auth_headers)
    req_headers.setdefault("X-Ebb-MCP-Profile", profile)
    if namespace is not None:
        req_headers["X-Ebb-Namespace"] = validate_namespace(namespace.strip())

    if clean_base.endswith(("/mcp", "/mcp/core", "/mcp/full", "/mcp/admin")):
        mcp_url = clean_base
    else:
        mcp_url = f"{clean_base}/mcp/{profile}"

    params: MCPServerStreamableHttpParams = {
        "url": mcp_url,
        "headers": req_headers,
        "timeout": timeout,
        "sse_read_timeout": sse_read_timeout,
    }

    tool_filter = None
    if allowed_tools is not None or blocked_tools is not None:
        tool_filter = create_static_tool_filter(
            allowed_tool_names=list(allowed_tools) if allowed_tools is not None else None,
            blocked_tool_names=list(blocked_tools) if blocked_tools is not None else None,
        )

    return MCPServerStreamableHttp(
        params=params,
        name=name or f"ebbmem-mcp-{profile}",
        cache_tools_list=cache_tools_list,
        client_session_timeout_seconds=client_session_timeout_seconds,
        tool_filter=tool_filter,
        use_structured_content=use_structured_content,
        require_approval=require_approval,
    )


def ebbmem_mcp_stdio_server(
    *,
    command: str = "ebb-mcp",
    connect_url: str | None = None,
    profile: MCPProfile = "core",
    namespace: str | None = None,
    bearer_token: str | None = None,
    api_key: str | None = None,
    extra_args: Sequence[str] | None = None,
    env: dict[str, str] | None = None,
    name: str | None = None,
    cache_tools_list: bool = True,
    client_session_timeout_seconds: float | None = 10.0,
) -> MCPServerStdio:
    """Create an ``MCPServerStdio`` wrapper for local ``ebb-mcp`` or ``ebb-mcp --connect <url>``."""
    if profile not in {"core", "full", "admin"}:
        raise ValidationError(f"Unsupported MCP profile: {profile!r}")

    args: list[str] = ["--profile", profile]
    if connect_url is not None:
        args.extend(["--connect", validate_base_url(connect_url)])
    if extra_args:
        args.extend(extra_args)

    proc_env: dict[str, str] = dict(env or {})
    auth_headers = validate_auth_credentials(bearer_token=bearer_token, api_key=api_key)
    if "Authorization" in auth_headers:
        proc_env["EBB_BEARER_TOKEN"] = auth_headers["Authorization"].removeprefix("Bearer ")
    if "X-API-Key" in auth_headers:
        proc_env["EBB_API_KEY"] = auth_headers["X-API-Key"]
    if namespace is not None:
        proc_env["EBB_NAMESPACE"] = validate_namespace(namespace.strip())

    params: MCPServerStdioParams = {
        "command": command,
        "args": args,
        "env": proc_env if proc_env else None,  # type: ignore[typeddict-item]
    }
    if not proc_env:
        del params["env"]

    return MCPServerStdio(
        params=params,
        name=name or f"ebbmem-stdio-{profile}",
        cache_tools_list=cache_tools_list,
        client_session_timeout_seconds=client_session_timeout_seconds,
    )
