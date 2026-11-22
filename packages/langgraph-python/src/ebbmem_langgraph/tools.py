"""LangMem-style manage, search, and reinforce memory tools backed by EbbmemStore."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any, Literal

from ebbmem_integration_core import DEFAULT_REINFORCE_DELTA, frame_recalled_memories
from langchain_core.tools import StructuredTool
from langgraph.config import get_config, get_store
from langgraph.store.base import BaseStore

from ebbmem_langgraph.store import EbbmemStore

_DEFAULT_MANAGE_INSTRUCTIONS = (
    "Proactively call this tool to record durable user preferences, decisions, and facts, "
    "update outdated memories by id, or soft-forget deleted memories (never stores raw secrets)."
)

_DEFAULT_SEARCH_INSTRUCTIONS = (
    "Search persistent EbbMem memories before answering questions about prior decisions, "
    "preferences, or project context. Remember to reinforce memories you actually use."
)


def _resolve_namespace(template: tuple[str, ...]) -> tuple[str, ...]:
    if not any("{" in seg and "}" in seg for seg in template):
        return template
    try:
        cfg = get_config()
    except RuntimeError:
        cfg = {}
    configurable = cfg.get("configurable", {}) if isinstance(cfg, dict) else {}
    resolved: list[str] = []
    for seg in template:
        if "{" in seg and "}" in seg:
            resolved.append(seg.format(**configurable))
        else:
            resolved.append(seg)
    return tuple(resolved)


def _resolve_store(initial_store: BaseStore | None) -> BaseStore:
    if initial_store is not None:
        return initial_store
    return get_store()


def create_ebbmem_manage_memory_tool(
    namespace: tuple[str, ...] = ("memories", "{langgraph_user_id}"),
    *,
    store: BaseStore | None = None,
    actions_permitted: Sequence[Literal["create", "update", "delete"]] = (
        "create",
        "update",
        "delete",
    ),
    instructions: str = _DEFAULT_MANAGE_INSTRUCTIONS,
    name: str = "manage_memory",
) -> StructuredTool:
    """Create a LangMem-compatible `manage_memory` tool backed by `BaseStore`/`EbbmemStore`.

    `delete` always maps to soft-tombstone `forget` via `store.delete`/`store.adelete`.
    """
    permitted = tuple(actions_permitted)
    if not permitted:
        raise ValueError("actions_permitted cannot be empty")
    default_action: str = "create" if "create" in permitted else permitted[0]

    def _validate_args(action: str, memory_id: str | None) -> None:
        if action not in permitted:
            raise ValueError(f"Invalid action {action!r}; expected one of {permitted}")
        if action == "create" and memory_id is not None:
            raise ValueError("Omit id when creating a new memory")
        if action in {"update", "delete"} and not memory_id:
            raise ValueError(f"id is required for action={action!r}")

    def manage_memory(
        content: str | dict[str, Any] | None = None,
        action: Literal["create", "update", "delete"] = default_action,  # type: ignore[assignment]
        *,
        id: str | None = None,
    ) -> str:
        _validate_args(action, id)
        target_store = _resolve_store(store)
        ns = _resolve_namespace(namespace)
        if action == "delete":
            assert id is not None
            target_store.delete(ns, key=str(id))
            return f"Deleted memory {id}"
        mem_key = str(id) if id is not None else str(uuid.uuid4())
        payload = content if isinstance(content, dict) else {"content": content}
        target_store.put(ns, key=mem_key, value=payload)
        return f"{action}d memory {mem_key}"

    async def amanage_memory(
        content: str | dict[str, Any] | None = None,
        action: Literal["create", "update", "delete"] = default_action,  # type: ignore[assignment]
        *,
        id: str | None = None,
    ) -> str:
        _validate_args(action, id)
        target_store = _resolve_store(store)
        ns = _resolve_namespace(namespace)
        if action == "delete":
            assert id is not None
            await target_store.adelete(ns, key=str(id))
            return f"Deleted memory {id}"
        mem_key = str(id) if id is not None else str(uuid.uuid4())
        payload = content if isinstance(content, dict) else {"content": content}
        await target_store.aput(ns, key=mem_key, value=payload)
        return f"{action}d memory {mem_key}"

    description = (
        f"Create, update, or soft-delete persistent memories ({', '.join(permitted)}). "
        f"{instructions}"
    )
    return StructuredTool.from_function(
        func=manage_memory,
        coroutine=amanage_memory,
        name=name,
        description=description,
    )


def create_ebbmem_search_memory_tool(
    namespace: tuple[str, ...] = ("memories", "{langgraph_user_id}"),
    *,
    store: BaseStore | None = None,
    response_format: Literal["content", "content_and_artifact"] = "content",
    frame_untrusted: bool = True,
    instructions: str = _DEFAULT_SEARCH_INSTRUCTIONS,
    name: str = "search_memory",
) -> StructuredTool:
    """Create a LangMem-compatible `search_memory` tool with untrusted-data framing."""

    def _format_results(items: list[Any]) -> str:
        if frame_untrusted:
            payloads = [
                {
                    "memory_id": it.key,
                    "tier": "semantic",
                    "content": json.dumps(it.value, ensure_ascii=False),
                }
                for it in items
            ]
            return frame_recalled_memories(payloads)
        return json.dumps([it.dict() for it in items], ensure_ascii=False)

    def search_memory(
        query: str,
        *,
        limit: int = 10,
        offset: int = 0,
        filter: dict[str, Any] | None = None,
    ) -> Any:
        target_store = _resolve_store(store)
        ns = _resolve_namespace(namespace)
        items = target_store.search(ns, query=query, filter=filter, limit=limit, offset=offset)
        rendered = _format_results(items)
        if response_format == "content_and_artifact":
            return rendered, items
        return rendered

    async def asearch_memory(
        query: str,
        *,
        limit: int = 10,
        offset: int = 0,
        filter: dict[str, Any] | None = None,
    ) -> Any:
        target_store = _resolve_store(store)
        ns = _resolve_namespace(namespace)
        items = await target_store.asearch(
            ns, query=query, filter=filter, limit=limit, offset=offset
        )
        rendered = _format_results(items)
        if response_format == "content_and_artifact":
            return rendered, items
        return rendered

    return StructuredTool.from_function(
        func=search_memory,
        coroutine=asearch_memory,
        name=name,
        description=f"Search long-term EbbMem memories. {instructions}",
        response_format=response_format,
    )


def create_ebbmem_reinforce_memory_tool(
    *,
    store: EbbmemStore | None = None,
    namespace: tuple[str, ...] = ("memories", "{langgraph_user_id}"),
    default_delta: float = DEFAULT_REINFORCE_DELTA,
    name: str = "reinforce_memory",
) -> StructuredTool:
    """Create an explicit reinforcement tool so agents only reinforce memories they used."""

    def _resolve_ebbmem_store() -> EbbmemStore:
        resolved = _resolve_store(store)
        if not isinstance(resolved, EbbmemStore):
            raise TypeError("reinforce_memory requires an EbbmemStore instance")
        return resolved

    def reinforce_memory(
        memory_id: str,
        *,
        used_ids: list[str] | None = None,
        recall_id: str | None = None,
        delta: float = default_delta,
    ) -> str:
        ebb_store = _resolve_ebbmem_store()
        ns = _resolve_namespace(namespace)
        target_id = ebb_store.get_memory_id(ns, memory_id) or memory_id
        resolved_used: list[str] = []
        for uid in used_ids or []:
            resolved_used.append(ebb_store.get_memory_id(ns, uid) or uid)
        res = ebb_store.reinforce(
            target_id,
            used_ids=resolved_used,
            recall_id=recall_id,
            delta=delta,
            namespace=ns,
        )
        return json.dumps(res, ensure_ascii=False)

    async def areinforce_memory(
        memory_id: str,
        *,
        used_ids: list[str] | None = None,
        recall_id: str | None = None,
        delta: float = default_delta,
    ) -> str:
        ebb_store = _resolve_ebbmem_store()
        ns = _resolve_namespace(namespace)
        target_id = await ebb_store.aget_memory_id(ns, memory_id) or memory_id
        resolved_used: list[str] = []
        for uid in used_ids or []:
            resolved_used.append(await ebb_store.aget_memory_id(ns, uid) or uid)
        res = await ebb_store.areinforce(
            target_id,
            used_ids=resolved_used,
            recall_id=recall_id,
            delta=delta,
            namespace=ns,
        )
        return json.dumps(res, ensure_ascii=False)

    return StructuredTool.from_function(
        func=reinforce_memory,
        coroutine=areinforce_memory,
        name=name,
        description=(
            "Reinforce memories that were actually used to answer or act (memories decay "
            "unless reinforced; never reinforce unused retrieved items)."
        ),
    )
