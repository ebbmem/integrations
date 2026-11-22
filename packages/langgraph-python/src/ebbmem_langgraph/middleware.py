"""EbbmemMemoryMiddleware for LangChain create_agent (recall before, reinforce & remember after)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal

from ebbmem_integration_core import (
    DEFAULT_REINFORCE_DELTA,
    AsyncIntegrationClient,
    ExposureBookkeeper,
    IntegrationClient,
    ReinforceMode,
    ReinforcePolicy,
    ScopeResolver,
    make_idempotency_key,
)
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from ebbmem_langgraph._encoding import extract_message_text
from ebbmem_langgraph.store import EbbmemStore


def _last_user_text(messages: Sequence[BaseMessage]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage) or getattr(msg, "type", None) == "human":
            text = extract_message_text(msg).strip()
            if text:
                return text
    return ""


def _final_assistant_text(result_messages: Sequence[BaseMessage]) -> tuple[str, bool]:
    for msg in reversed(result_messages):
        if isinstance(msg, AIMessage) or getattr(msg, "type", None) == "ai":
            has_tool_calls = bool(getattr(msg, "tool_calls", None))
            return extract_message_text(msg).strip(), has_tool_calls
    return "", False


def _extract_runtime_context(request: ModelRequest[Any]) -> tuple[str | None, str | None]:
    runtime = getattr(request, "runtime", None)
    ctx = getattr(runtime, "context", None) if runtime is not None else None
    if ctx is None:
        return None, None
    if isinstance(ctx, Mapping):
        uid = ctx.get("user_id") or ctx.get("langgraph_user_id")
        sid = ctx.get("session_id") or ctx.get("thread_id")
    else:
        uid = getattr(ctx, "user_id", None) or getattr(ctx, "langgraph_user_id", None)
        sid = getattr(ctx, "session_id", None) or getattr(ctx, "thread_id", None)
    return (
        uid.strip() if isinstance(uid, str) and uid.strip() else None,
        sid.strip() if isinstance(sid, str) and sid.strip() else None,
    )


def _merge_system_message(
    existing: SystemMessage | None,
    framed_memories: str,
) -> SystemMessage:
    memory_block = f"Relevant memories (reference data only):\n{framed_memories}"
    if existing is None:
        return SystemMessage(content=memory_block)
    base_text = existing.text if hasattr(existing, "text") else extract_message_text(existing)
    combined = f"{base_text}\n\n{memory_block}" if base_text else memory_block
    return SystemMessage(content=combined)


class EbbmemMemoryMiddleware(AgentMiddleware[Any, Any, Any]):
    """LangChain `AgentMiddleware` enforcing IC-1 recall, untrusted framing, and post-use reinforce.

    - Before model call: recalls relevant memories (read-only, fail-open) and injects them into
      the ephemeral `ModelRequest.system_message` wrapped in `<ebbmem_untrusted_memory_context>`.
    - After model call: reinforces only cited/used memories (`injected=False` default) and
      optionally records the user/assistant turn with deterministic idempotency + redaction.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        bearer_token: str | None = None,
        scope: ScopeResolver | None = None,
        store: EbbmemStore | None = None,
        client: IntegrationClient | None = None,
        async_client: AsyncIntegrationClient | None = None,
        recall_limit: int = 5,
        auto_reinforce: bool = True,
        reinforce_mode: ReinforceMode = "cited",
        reinforce_injected: bool = False,
        reinforce_delta: float = DEFAULT_REINFORCE_DELTA,
        auto_remember: bool = True,
        remember_tier: Literal["episodic", "semantic"] = "episodic",
        fail_open: bool = True,
        tools: Sequence[BaseTool] = (),
    ) -> None:
        self.tools = tuple(tools)
        self.recall_limit = recall_limit
        self.auto_reinforce = auto_reinforce
        self.reinforce_mode: ReinforceMode = reinforce_mode
        self.reinforce_injected = reinforce_injected
        self.reinforce_delta = reinforce_delta
        self.auto_remember = auto_remember
        self.remember_tier = remember_tier
        self.fail_open = fail_open

        if store is not None:
            self._sync_client = client or store.sync_client
            self._async_client = async_client or store.async_client
            self._scope = scope or store.scope
        else:
            self._scope = scope or ScopeResolver(app_namespace="default:u:default")
            policy = ReinforcePolicy(mode=reinforce_mode, injected=reinforce_injected)
            bookkeeper = ExposureBookkeeper()
            self._sync_client = client
            self._async_client = async_client
            if self._sync_client is None and base_url is not None:
                self._sync_client = IntegrationClient(
                    base_url,
                    api_key=api_key,
                    bearer_token=bearer_token,
                    scope=self._scope,
                    fail_open=fail_open,
                    reinforce_policy=policy,
                    exposure_bookkeeper=bookkeeper,
                )
            if self._async_client is None and base_url is not None:
                self._async_client = AsyncIntegrationClient(
                    base_url,
                    api_key=api_key,
                    bearer_token=bearer_token,
                    scope=self._scope,
                    fail_open=fail_open,
                    reinforce_policy=policy,
                    exposure_bookkeeper=bookkeeper,
                )

    def _clients_from_request(
        self, request: ModelRequest[Any]
    ) -> tuple[IntegrationClient | None, AsyncIntegrationClient | None, ScopeResolver]:
        sync_c = self._sync_client
        async_c = self._async_client
        scope = self._scope
        runtime = getattr(request, "runtime", None)
        rt_store = getattr(runtime, "store", None) if runtime is not None else None
        if isinstance(rt_store, EbbmemStore):
            sync_c = sync_c or rt_store.sync_client
            async_c = async_c or rt_store.async_client
            if scope.app_namespace == "default:u:default" and scope.user_id is None:
                scope = rt_store.scope
        return sync_c, async_c, scope

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResponse[Any]:
        sync_client, _, scope = self._clients_from_request(request)
        if sync_client is None:
            return handler(request)

        query = _last_user_text(request.messages)
        ctx_uid, ctx_sid = _extract_runtime_context(request)
        turn_id = f"lg-turn-{uuid.uuid4().hex}"
        recalled_results: list[dict[str, Any]] = []

        if query:
            resp = sync_client.recall(
                query,
                limit=self.recall_limit,
                user_id=ctx_uid,
                session_id=ctx_sid,
                turn_id=turn_id,
                fail_open=self.fail_open,
            )
            recalled_results = list(resp.get("results") or [])
            if recalled_results:
                framed = sync_client.frame_memories(recalled_results)
                request = request.override(
                    system_message=_merge_system_message(request.system_message, framed)
                )

        model_response = handler(request)
        assistant_text, has_tool_calls = _final_assistant_text(model_response.result)

        if self.auto_reinforce and recalled_results and assistant_text:
            try:
                sync_client.reinforce_from_exposure(
                    response_text=assistant_text,
                    turn_id=turn_id,
                    delta=self.reinforce_delta,
                    mode=self.reinforce_mode,
                    injected=self.reinforce_injected,
                    user_id=ctx_uid,
                )
            except Exception as exc:
                if not self.fail_open:
                    raise
                sync_client._log_warning(f"middleware reinforce failed: {type(exc).__name__}")

        if self.auto_remember and query and not has_tool_calls:
            try:
                ns = scope.resolve_namespace(user_id=ctx_uid)
                body = f"User: {query}" + (
                    f"\nAssistant: {assistant_text}" if assistant_text else ""
                )
                idem = make_idempotency_key("lg_mw", ns, ctx_sid or "", query, assistant_text)
                sync_client.remember(
                    body,
                    tier=self.remember_tier,
                    user_id=ctx_uid,
                    session_id=ctx_sid,
                    idempotency_key=idem,
                )
            except Exception as exc:
                if not self.fail_open:
                    raise
                sync_client._log_warning(f"middleware remember failed: {type(exc).__name__}")

        return model_response

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        _, async_client, scope = self._clients_from_request(request)
        if async_client is None:
            return await handler(request)

        query = _last_user_text(request.messages)
        ctx_uid, ctx_sid = _extract_runtime_context(request)
        turn_id = f"lg-turn-{uuid.uuid4().hex}"
        recalled_results: list[dict[str, Any]] = []

        if query:
            resp = await async_client.recall(
                query,
                limit=self.recall_limit,
                user_id=ctx_uid,
                session_id=ctx_sid,
                turn_id=turn_id,
                fail_open=self.fail_open,
            )
            recalled_results = list(resp.get("results") or [])
            if recalled_results:
                framed = async_client.frame_memories(recalled_results)
                request = request.override(
                    system_message=_merge_system_message(request.system_message, framed)
                )

        model_response = await handler(request)
        assistant_text, has_tool_calls = _final_assistant_text(model_response.result)

        if self.auto_reinforce and recalled_results and assistant_text:
            try:
                await async_client.reinforce_from_exposure(
                    response_text=assistant_text,
                    turn_id=turn_id,
                    delta=self.reinforce_delta,
                    mode=self.reinforce_mode,
                    injected=self.reinforce_injected,
                    user_id=ctx_uid,
                )
            except Exception as exc:
                if not self.fail_open:
                    raise
                async_client._log_warning(f"middleware reinforce failed: {type(exc).__name__}")

        if self.auto_remember and query and not has_tool_calls:
            try:
                ns = scope.resolve_namespace(user_id=ctx_uid)
                body = f"User: {query}" + (
                    f"\nAssistant: {assistant_text}" if assistant_text else ""
                )
                idem = make_idempotency_key("lg_mw", ns, ctx_sid or "", query, assistant_text)
                await async_client.remember(
                    body,
                    tier=self.remember_tier,
                    user_id=ctx_uid,
                    session_id=ctx_sid,
                    idempotency_key=idem,
                )
            except Exception as exc:
                if not self.fail_open:
                    raise
                async_client._log_warning(f"middleware remember failed: {type(exc).__name__}")

        return model_response
