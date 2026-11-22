"""Tests for EbbmemMemoryMiddleware, LangMem-style tools, and langmem compatibility."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from ebbmem_integration_core import RetryPolicy, ScopeResolver
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langgraph.func import entrypoint
from langmem import (
    create_manage_memory_tool,
    create_memory_store_manager,
    create_search_memory_tool,
)

from ebbmem_langgraph import (
    EbbmemMemoryMiddleware,
    EbbmemStore,
    create_ebbmem_manage_memory_tool,
    create_ebbmem_reinforce_memory_tool,
    create_ebbmem_search_memory_tool,
)


class _RecordingMockChatModel(BaseChatModel):
    """Deterministic mock chat model recording system messages seen per call."""

    responses: list[AIMessage]
    seen_system_prompts: list[str] = []
    call_index: int = 0

    @property
    def _llm_type(self) -> str:
        return "recording-mock"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        sys_texts = [str(m.content) for m in messages if isinstance(m, SystemMessage)]
        self.seen_system_prompts.append("\n".join(sys_texts))
        msg = self.responses[min(self.call_index, len(self.responses) - 1)]
        self.call_index += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


class _LangmemExtractorMockModel(BaseChatModel):
    """Mock chat model supporting trustcall bind_tools for create_memory_store_manager."""

    planned_tool_calls: list[dict[str, Any]]

    @property
    def _llm_type(self) -> str:
        return "langmem-mock"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        planned = self.planned_tool_calls

        def _run(_input: Any, config: Any = None) -> AIMessage:
            return AIMessage(content="", tool_calls=planned)

        return RunnableLambda(_run)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(
            generations=[
                ChatGeneration(message=AIMessage(content="", tool_calls=self.planned_tool_calls))
            ]
        )


def test_middleware_sync_and_async_with_create_agent_and_fail_open(
    mock_server: Any,
    fast_retry_policy: RetryPolicy,
) -> None:
    server = mock_server
    server.set_exposure_logging("acme:u:alice", True)
    store = EbbmemStore(
        server.base_url,
        api_key="ebb_test_alice_key",
        scope=ScopeResolver(tenant="acme", user_id="alice"),
        retry_policy=fast_retry_policy,
    )
    assert store.sync_client is not None
    m_used = store.sync_client.remember("Alice prefers concise bullet points", tier="semantic")
    m_unused = store.sync_client.remember("Alice likes rainy weather concise", tier="semantic")

    model = _RecordingMockChatModel(
        responses=[AIMessage(content=f"Using concise bullets per memory {m_used['id']}.")]
    )
    mw = EbbmemMemoryMiddleware(
        store=store,
        recall_limit=5,
        auto_reinforce=True,
        reinforce_mode="cited",
        auto_remember=True,
    )
    agent = create_agent(
        model,
        system_prompt="You are a helpful assistant.",
        middleware=[mw],
        store=store,
    )

    res = agent.invoke(
        {"messages": [{"role": "user", "content": "How should I format concise notes?"}]}
    )
    assert "Using concise bullets" in res["messages"][-1].content
    assert "<ebbmem_untrusted_memory_context" in model.seen_system_prompts[-1]
    assert "Alice prefers concise bullet points" in model.seen_system_prompts[-1]

    # Only m_used was cited -> only m_used reinforced (injected=False default)
    assert server.get_memory_raw("acme:u:alice", m_used["id"]).storage_strength == 1.1  # type: ignore[union-attr]
    assert server.get_memory_raw("acme:u:alice", m_unused["id"]).storage_strength == 1.0  # type: ignore[union-attr]

    # auto_remember persisted the user/assistant turn
    recalled_turn = store.sync_client.recall("format concise notes")
    assert any(
        "How should I format concise notes?" in r["content"] for r in recalled_turn["results"]
    )


@pytest.mark.asyncio
async def test_middleware_async_and_fail_open_on_outage(
    mock_server: Any,
) -> None:
    server = mock_server
    server.set_exposure_logging("acme:u:alice", True)
    short_retry = RetryPolicy(
        timeout_s=0.1,
        max_retries=0,
        backoff_base_s=0.001,
        backoff_cap_s=0.005,
        jitter_fn=lambda _a, _b: 0.0,
    )
    store = EbbmemStore(
        server.base_url,
        api_key="ebb_test_alice_key",
        scope=ScopeResolver(tenant="acme", user_id="alice"),
        retry_policy=short_retry,
        fail_open=True,
    )
    assert store.async_client is not None
    m1 = await store.async_client.remember("Async preference: dark theme", tier="semantic")

    model = _RecordingMockChatModel(
        responses=[AIMessage(content=f"Confirmed {m1['id']} dark theme.")]
    )
    mw = EbbmemMemoryMiddleware(store=store, auto_remember=False)
    agent = create_agent(model, middleware=[mw], store=store)
    res = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What dark theme preference?"}]}
    )
    assert m1["id"] in res["messages"][-1].content
    assert "<ebbmem_untrusted_memory_context" in model.seen_system_prompts[-1]
    assert server.get_memory_raw("acme:u:alice", m1["id"]).storage_strength == 1.1  # type: ignore[union-attr]

    # Fail-open when server is unreachable
    down_store = EbbmemStore(
        "http://127.0.0.1:1",
        api_key="ebb_test_alice_key",
        scope=ScopeResolver(tenant="acme", user_id="alice"),
        retry_policy=short_retry,
        fail_open=True,
    )
    down_model = _RecordingMockChatModel(responses=[AIMessage(content="Answered without memory.")])
    down_agent = create_agent(
        down_model,
        middleware=[EbbmemMemoryMiddleware(store=down_store)],
        store=down_store,
    )
    down_res = await down_agent.ainvoke(
        {"messages": [{"role": "user", "content": "Hello without server"}]}
    )
    assert down_res["messages"][-1].content == "Answered without memory."


@pytest.mark.asyncio
async def test_ebbmem_tools_and_langmem_compatibility(
    mock_server: Any,
    fast_retry_policy: RetryPolicy,
) -> None:
    store = EbbmemStore(
        mock_server.base_url,
        api_key="ebb_test_alice_key",
        scope=ScopeResolver(tenant="acme", user_id="alice"),
        retry_policy=fast_retry_policy,
    )

    # 1. EbbMem LangMem-style manage/search/reinforce tools
    manage_tool = create_ebbmem_manage_memory_tool(
        namespace=("memories", "{langgraph_user_id}"), store=store
    )
    search_tool = create_ebbmem_search_memory_tool(
        namespace=("memories", "{langgraph_user_id}"),
        store=store,
        response_format="content_and_artifact",
    )
    reinforce_tool = create_ebbmem_reinforce_memory_tool(
        namespace=("memories", "{langgraph_user_id}"), store=store
    )

    @entrypoint(store=store)
    async def workflow(payload: dict[str, Any]) -> dict[str, Any]:
        created_msg = await manage_tool.ainvoke({"content": payload["content"], "action": "create"})
        mem_key = created_msg.split()[-1]
        tool_msg = await search_tool.ainvoke(
            {
                "name": "search_memory",
                "args": {"query": payload["query"]},
                "id": "tc_search_1",
                "type": "tool_call",
            }
        )
        reinf_str = await reinforce_tool.ainvoke({"memory_id": mem_key, "delta": 0.2})
        return {
            "created": mem_key,
            "search": tool_msg.content,
            "artifacts": tool_msg.artifact,
            "reinf": reinf_str,
        }

    cfg = {"configurable": {"langgraph_user_id": "alice"}}
    out = await workflow.ainvoke(
        {"content": "User loves black coffee", "query": "coffee"},
        config=cfg,
    )
    assert "<ebbmem_untrusted_memory_context" in out["search"]
    assert len(out["artifacts"]) == 1
    assert json.loads(out["reinf"])["outcome"] == "counted"

    # Soft delete via manage_tool -> tombstones in EbbMem
    del_msg = manage_tool.invoke({"action": "delete", "id": out["created"]}, config=cfg)
    assert "Deleted memory" in del_msg
    assert store.get(("memories", "alice"), out["created"]) is None

    # 2. Official langmem manage/search tools backed by EbbmemStore
    lm_manage = create_manage_memory_tool(
        namespace=("langmem_tools", "{langgraph_user_id}"), store=store
    )
    lm_search = create_search_memory_tool(
        namespace=("langmem_tools", "{langgraph_user_id}"), store=store
    )
    lm_created = await lm_manage.ainvoke(
        {"content": "Official LangMem tool works with EbbMem"},
        config=cfg,
    )
    assert "created memory" in lm_created
    lm_found = await lm_search.ainvoke({"query": "Official LangMem"}, config=cfg)
    assert "Official LangMem tool works with EbbMem" in lm_found

    # 3. Official langmem.create_memory_store_manager(store=EbbmemStore(...)) compatibility
    mock_lm_model = _LangmemExtractorMockModel(
        planned_tool_calls=[
            {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "name": "Memory",
                "args": {"content": "User prefers espresso in the morning"},
            }
        ]
    )
    manager = create_memory_store_manager(
        mock_lm_model,
        namespace=("lm_manager", "{langgraph_user_id}"),
        store=store,
    )
    puts = await manager.ainvoke(
        {"messages": [{"role": "user", "content": "I prefer espresso in the morning"}]},
        config=cfg,
    )
    assert len(puts) == 1
    found_lm = await manager.asearch(query="espresso", config=cfg)
    assert len(found_lm) == 1
    assert found_lm[0].value.content == "User prefers espresso in the morning"

    # Sync invoke compatibility on create_memory_store_manager as well
    sync_found = manager.search(query="espresso", config=cfg)
    assert len(sync_found) == 1
