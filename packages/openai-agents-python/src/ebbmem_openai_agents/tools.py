"""OpenAI Agents SDK function tools and post-use reinforcement hooks for EbbMem."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Sequence
from typing import Any, Literal

from agents.agent import Agent
from agents.lifecycle import RunHooksBase
from agents.run_context import AgentHookContext
from agents.tool import FunctionTool, function_tool
from ebbmem_integration_core import (
    DEFAULT_FRAMEWORK_REINFORCE_MODE,
    DEFAULT_REINFORCE_DELTA,
    AsyncIntegrationClient,
    EbbmemApiError,
    IntegrationClient,
    ReinforceMode,
    ReinforcePolicy,
    redact_secrets,
    validate_delta,
)

MemoryTier = Literal["working", "episodic", "semantic", "procedural"]


def _stringify_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, default=str)
    except TypeError:
        return str(output)


class EbbmemToolkit:
    """Coordinates EbbMem function tools and IC-1 post-use reinforcement for OpenAI Agents."""

    def __init__(
        self,
        client: AsyncIntegrationClient | IntegrationClient,
        *,
        user_id: str | None = None,
        app_namespace: str | None = None,
        session_id: str | None = None,
        reinforce_mode: ReinforceMode = DEFAULT_FRAMEWORK_REINFORCE_MODE,
        injected: bool = False,
        default_delta: float = DEFAULT_REINFORCE_DELTA,
        default_limit: int = 5,
        fail_open_reinforce: bool = True,
    ) -> None:
        validate_delta(default_delta)
        # Validate policy upfront so invalid modes fail fast.
        ReinforcePolicy(mode=reinforce_mode, injected=injected)
        self.client = client
        self.user_id = user_id
        self.app_namespace = app_namespace
        self.session_id = session_id
        self.reinforce_mode: ReinforceMode = reinforce_mode
        self.injected = injected
        self.default_delta = default_delta
        self.default_limit = default_limit
        self.fail_open_reinforce = fail_open_reinforce
        self._active_turn_id: str = f"turn-{uuid.uuid4().hex[:12]}"
        self._reinforced_turns: set[str] = set()
        self._warnings: list[str] = []

    @property
    def active_turn_id(self) -> str:
        """Return the current turn identifier used to correlate recall exposure."""
        return self._active_turn_id

    @property
    def warnings(self) -> list[str]:
        """Return redacted warnings emitted during fail-open operations."""
        return list(self._warnings)

    def begin_turn(self, turn_id: str | None = None) -> str:
        """Start a new turn scope for exposure tracking and post-use reinforcement."""
        self._active_turn_id = turn_id or f"turn-{uuid.uuid4().hex[:12]}"
        return self._active_turn_id

    async def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if isinstance(self.client, AsyncIntegrationClient):
            return await fn(*args, **kwargs)
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _recall(
        self,
        *,
        query: str,
        limit: int | None = None,
        tier: MemoryTier | None = None,
        tags: list[str] | None = None,
    ) -> str:
        effective_limit = self.default_limit if limit is None else max(1, min(100, limit))
        turn_id = self._active_turn_id
        resp = await self._call(
            self.client.recall,
            query,
            limit=effective_limit,
            tier=tier,
            tags=tags,
            user_id=self.user_id,
            app_namespace=self.app_namespace,
            session_id=self.session_id,
            turn_id=turn_id,
        )
        if resp.get("degraded"):
            return json.dumps(
                {
                    "status": "degraded",
                    "count": 0,
                    "degraded": True,
                    "context": "No recalled memories available (fail-open degraded recall).",
                }
            )
        framed = self.client.frame_memories(resp)
        recall_id = resp.get("recall_id")
        header = f"count={resp.get('count', 0)}"
        if isinstance(recall_id, str) and recall_id:
            header += f" recall_id={recall_id}"
        return (
            f"{header}\n{framed}\n"
            "Note: Recall is read-only. If you actually use any memory above in your answer, "
            "cite its memory id or call ebbmem_reinforce."
        )

    async def _remember(
        self,
        *,
        content: str,
        tier: MemoryTier = "semantic",
        tags: list[str] | None = None,
        importance: float | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        resp = await self._call(
            self.client.remember,
            content,
            tier=tier,
            tags=tags,
            importance=importance,
            user_id=self.user_id,
            app_namespace=self.app_namespace,
            session_id=self.session_id,
            idempotency_key=idempotency_key,
        )
        return json.dumps(
            {
                "id": resp.get("id"),
                "memory_id": resp.get("memory_id") or resp.get("id"),
                "namespace": resp.get("namespace"),
                "tier": resp.get("tier"),
                "storage_strength": resp.get("storage_strength"),
                "replayed": resp.get("replayed", False),
            }
        )

    async def _reinforce(
        self,
        *,
        memory_id: str,
        used_ids: list[str] | None = None,
        recall_id: str | None = None,
        delta: float | None = None,
    ) -> str:
        effective_delta = self.default_delta if delta is None else validate_delta(delta)
        all_used = [memory_id, *(used_ids or [])]
        turn_id = self._active_turn_id
        ns = self.client.scope.resolve_namespace(
            user_id=self.user_id,
            app_namespace=self.app_namespace,
        )
        has_exposure = self.client.exposure_bookkeeper.get_for_turn(ns, turn_id) is not None
        if not has_exposure and recall_id:
            has_exposure = (
                self.client.exposure_bookkeeper.get_by_recall_id(recall_id, namespace=ns)
                is not None
            )

        if has_exposure:
            resp = await self._call(
                self.client.reinforce_from_exposure,
                used_ids=all_used,
                turn_id=turn_id,
                recall_id=recall_id,
                delta=effective_delta,
                mode="tool",
                injected=self.injected,
                user_id=self.user_id,
                app_namespace=self.app_namespace,
            )
            if resp is None:
                return json.dumps(
                    {
                        "status": "skipped",
                        "reason": "no_matching_recalled_ids",
                        "memory_id": memory_id,
                    }
                )
        else:
            extra_used = [uid for uid in (used_ids or []) if uid != memory_id]
            resp = await self._call(
                self.client.reinforce,
                memory_id,
                used_ids=extra_used,
                recall_id=recall_id,
                delta=effective_delta,
                user_id=self.user_id,
                app_namespace=self.app_namespace,
            )

        self._reinforced_turns.add(turn_id)
        return json.dumps(resp)

    async def _forget(self, *, memory_id: str) -> str:
        resp = await self._call(
            self.client.forget,
            memory_id,
            user_id=self.user_id,
            app_namespace=self.app_namespace,
        )
        return json.dumps(resp)

    async def reinforce_on_use(
        self,
        *,
        used_ids: Sequence[str] = (),
        response_text: str | None = None,
        turn_id: str | None = None,
        recall_id: str | None = None,
        delta: float | None = None,
        mode: ReinforceMode | None = None,
        injected: bool | None = None,
    ) -> dict[str, Any] | None:
        """Reinforce recalled memories that were actually used in a response or tool output."""
        target_turn = turn_id or self._active_turn_id
        if target_turn in self._reinforced_turns and not used_ids:
            return None

        effective_delta = self.default_delta if delta is None else validate_delta(delta)
        effective_mode = mode if mode is not None else self.reinforce_mode
        effective_injected = self.injected if injected is None else injected
        try:
            resp = await self._call(
                self.client.reinforce_from_exposure,
                used_ids=list(used_ids),
                response_text=response_text,
                turn_id=target_turn,
                recall_id=recall_id,
                delta=effective_delta,
                mode=effective_mode,
                injected=effective_injected,
                user_id=self.user_id,
                app_namespace=self.app_namespace,
            )
        except (EbbmemApiError, TimeoutError) as exc:
            if not self.fail_open_reinforce:
                raise
            self._warnings.append(redact_secrets(f"reinforce failed-open: {type(exc).__name__}"))
            return None

        if resp is not None:
            self._reinforced_turns.add(target_turn)
        return resp

    def tools(self, *, include_forget: bool = False) -> list[FunctionTool]:
        """Build OpenAI Agents SDK FunctionTool wrappers bound to this toolkit."""

        @function_tool
        async def ebbmem_recall(
            query: str,
            limit: int | None = None,
            tier: MemoryTier | None = None,
            tags: list[str] | None = None,
        ) -> str:
            """Search EbbMem memories without modifying their strength (read-only).

            Args:
                query: Natural-language search query for relevant memories.
                limit: Maximum number of memories to return (1-100).
                tier: Optional memory tier filter ('working', 'episodic', 'semantic', 'procedural').
                tags: Optional list of tag filters.
            """
            return await self._recall(query=query, limit=limit, tier=tier, tags=tags)

        @function_tool
        async def ebbmem_remember(
            content: str,
            tier: MemoryTier = "semantic",
            tags: list[str] | None = None,
            importance: float | None = None,
            idempotency_key: str | None = None,
        ) -> str:
            """Persist a durable fact, preference, or event into EbbMem (secrets are redacted).

            Args:
                content: Memory text to persist.
                tier: Target tier ('working', 'episodic', 'semantic', 'procedural').
                tags: Optional contextual tags.
                importance: Optional importance weight in [0.0, 1.0].
                idempotency_key: Optional deterministic key for safe retries.
            """
            return await self._remember(
                content=content,
                tier=tier,
                tags=tags,
                importance=importance,
                idempotency_key=idempotency_key,
            )

        @function_tool
        async def ebbmem_reinforce(
            memory_id: str,
            used_ids: list[str] | None = None,
            recall_id: str | None = None,
            delta: float | None = None,
        ) -> str:
            """Reinforce memories that actually informed your answer (memories decay otherwise).

            Args:
                memory_id: Primary memory ID that was used.
                used_ids: Additional recalled memory IDs that were also used.
                recall_id: Optional recall exposure ID returned by ebbmem_recall.
                delta: Strength delta within [-1.0, 1.0] (defaults to +0.1).
            """
            return await self._reinforce(
                memory_id=memory_id,
                used_ids=used_ids,
                recall_id=recall_id,
                delta=delta,
            )

        built: list[FunctionTool] = [ebbmem_recall, ebbmem_remember, ebbmem_reinforce]

        if include_forget:

            @function_tool
            async def ebbmem_forget(memory_id: str) -> str:
                """Soft-delete (tombstone) a memory by ID while preserving audit lineage.

                Args:
                    memory_id: Memory ID to forget.
                """
                return await self._forget(memory_id=memory_id)

            built.append(ebbmem_forget)

        return built

    def hooks(self) -> EbbmemRunHooks:
        """Return RunHooks that automatically reinforce cited recalled memories on agent end."""
        return EbbmemRunHooks(self)


class EbbmemRunHooks(RunHooksBase[Any, Agent[Any]]):
    """RunHooks subclass that manages turn boundaries and post-use reinforcement."""

    def __init__(self, toolkit: EbbmemToolkit) -> None:
        self.toolkit = toolkit

    async def on_agent_start(self, context: AgentHookContext[Any], agent: Agent[Any]) -> None:
        del context, agent
        self.toolkit.begin_turn()

    async def on_agent_end(
        self,
        context: AgentHookContext[Any],
        agent: Agent[Any],
        output: Any,
    ) -> None:
        del context, agent
        text = _stringify_output(output)
        await self.toolkit.reinforce_on_use(response_text=text)


def ebbmem_tools(
    client: AsyncIntegrationClient | IntegrationClient,
    *,
    user_id: str | None = None,
    app_namespace: str | None = None,
    session_id: str | None = None,
    reinforce_mode: ReinforceMode = DEFAULT_FRAMEWORK_REINFORCE_MODE,
    injected: bool = False,
    default_delta: float = DEFAULT_REINFORCE_DELTA,
    default_limit: int = 5,
    include_forget: bool = False,
) -> tuple[list[FunctionTool], EbbmemToolkit]:
    """Convenience factory returning (tools, toolkit) for an OpenAI Agents SDK Agent."""
    toolkit = EbbmemToolkit(
        client,
        user_id=user_id,
        app_namespace=app_namespace,
        session_id=session_id,
        reinforce_mode=reinforce_mode,
        injected=injected,
        default_delta=default_delta,
        default_limit=default_limit,
    )
    return toolkit.tools(include_forget=include_forget), toolkit
