"""Asynchronous AsyncIntegrationClient facade enforcing IC-1 invariants."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ebbmem_integration_core.errors import (
    EbbmemApiError,
    RateLimitError,
    RequestTimeoutError,
    ServerError,
    TransportError,
    UnsupportedCapabilityError,
    ValidationError,
)
from ebbmem_integration_core.reinforce import (
    DEFAULT_REINFORCE_DELTA,
    ExposureBookkeeper,
    ReinforcementTracker,
    ReinforceMode,
    ReinforcePlan,
    ReinforcePolicy,
    validate_delta,
)
from ebbmem_integration_core.retry import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT_S, RetryPolicy
from ebbmem_integration_core.scoping import ScopeResolver, validate_metadata
from ebbmem_integration_core.security import (
    TELEMETRY_ENABLED_DEFAULT,
    frame_recalled_memories,
    redact_payload,
    redact_secrets,
)
from ebbmem_integration_core.transport import AsyncMemoryTransport, AsyncStdlibHttpTransport


def _is_fail_open_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        RequestTimeoutError | TransportError | RateLimitError | ServerError | TimeoutError,
    )


def _degraded_recall(query: str) -> dict[str, Any]:
    return {"query": query, "count": 0, "results": [], "degraded": True}


def _degraded_list() -> dict[str, Any]:
    return {
        "items": [],
        "rows": [],
        "total": 0,
        "total_count": 0,
        "has_more": False,
        "next_cursor": None,
        "degraded": True,
    }


def _degraded_episodic_tail(thread_key: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "thread_key": thread_key,
        "count": 0,
        "turns": [],
        "degraded": True,
    }


class AsyncIntegrationClient:
    """Asynchronous EbbMem integration core client with full IC-1 parity."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        bearer_token: str | None = None,
        api_key: str | None = None,
        scope: ScopeResolver | None = None,
        transport: AsyncMemoryTransport | None = None,
        fail_open: bool = True,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_policy: RetryPolicy | None = None,
        reinforce_policy: ReinforcePolicy | None = None,
        exposure_bookkeeper: ExposureBookkeeper | None = None,
        telemetry_enabled: bool = TELEMETRY_ENABLED_DEFAULT,
        redact_before_remember: bool = True,
        log_sink: list[str] | None = None,
    ) -> None:
        if transport is None:
            if not base_url:
                raise ValueError("Provide either base_url or a custom async transport")
            policy = (
                retry_policy
                if retry_policy is not None
                else RetryPolicy(timeout_s=timeout_s, max_retries=max_retries)
            )
            transport = AsyncStdlibHttpTransport(
                base_url,
                bearer_token=bearer_token,
                api_key=api_key,
                retry_policy=policy,
                telemetry_enabled=telemetry_enabled,
                log_sink=log_sink,
            )
        self.transport = transport
        self.scope = (
            scope if scope is not None else ScopeResolver(app_namespace="default:u:default")
        )
        self.fail_open = fail_open
        self.telemetry_enabled = bool(telemetry_enabled)
        self.redact_before_remember = redact_before_remember
        self.log_sink: list[str] = (
            log_sink if log_sink is not None else getattr(transport, "log_sink", [])
        )
        self.reinforce_policy = (
            reinforce_policy if reinforce_policy is not None else ReinforcePolicy()
        )
        self.exposure_bookkeeper = (
            exposure_bookkeeper if exposure_bookkeeper is not None else ExposureBookkeeper()
        )
        self.tracker = ReinforcementTracker(
            policy=self.reinforce_policy,
            bookkeeper=self.exposure_bookkeeper,
        )

    def __repr__(self) -> str:
        return f"AsyncIntegrationClient(transport={self.transport!r}, fail_open={self.fail_open!r})"

    def __str__(self) -> str:
        return self.__repr__()

    def _log_warning(self, message: str) -> None:
        self.log_sink.append(redact_secrets(message))

    async def remember(
        self,
        content: str,
        *,
        tier: str = "semantic",
        user_id: str | None = None,
        app_namespace: str | None = None,
        header_namespace: str | None = None,
        body_namespace: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        thread_id: str | None = None,
        tuple_segments: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        domain: str | None = None,
        importance: float | None = None,
        valence: float | None = None,
        arousal: float | None = None,
        timestamp_ms: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        resolved = self.scope.resolve(
            user_id=user_id,
            app_namespace=app_namespace,
            header_namespace=header_namespace,
            body_namespace=body_namespace,
            agent_id=agent_id,
            session_id=session_id,
            thread_id=thread_id,
            tuple_segments=tuple_segments,
            tags=tags,
            domain=domain,
        )
        clean_metadata = validate_metadata(metadata)
        effective_content = redact_secrets(content) if self.redact_before_remember else content
        if self.redact_before_remember and clean_metadata:
            clean_metadata = redact_payload(clean_metadata)
        final_tags = resolved.remember_tags(include_domain=True)
        if self.redact_before_remember and final_tags:
            final_tags = [redact_secrets(t) for t in final_tags]

        return await self.transport.remember(
            content=effective_content,
            namespace=resolved.namespace,
            tier=tier,
            tags=final_tags,
            importance=importance,
            valence=valence,
            arousal=arousal,
            session_id=resolved.session_id,
            timestamp_ms=timestamp_ms,
            metadata=clean_metadata or None,
            idempotency_key=idempotency_key,
            headers=headers,
        )

    async def recall(
        self,
        query: str,
        *,
        limit: int = 10,
        tier: str | None = None,
        user_id: str | None = None,
        app_namespace: str | None = None,
        header_namespace: str | None = None,
        body_namespace: str | None = None,
        session_id: str | None = None,
        thread_id: str | None = None,
        tuple_segments: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        domain: str | None = None,
        profile: str | None = None,
        min_score: float | None = None,
        turn_id: str | None = None,
        timeout_s: float | None = None,
        fail_open: bool | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        resolved = self.scope.resolve(
            user_id=user_id,
            app_namespace=app_namespace,
            header_namespace=header_namespace,
            body_namespace=body_namespace,
            session_id=session_id,
            thread_id=thread_id,
            tuple_segments=tuple_segments,
            tags=tags,
            domain=domain,
        )
        effective_fail_open = self.fail_open if fail_open is None else fail_open
        try:
            resp = await self.transport.recall(
                query=query,
                namespace=resolved.namespace,
                limit=limit,
                tier=tier,
                tags=list(resolved.tags) if resolved.tags else None,
                session_id=resolved.session_id,
                profile=profile,
                min_score=min_score,
                domain=resolved.domain,
                timeout_s=timeout_s,
                headers=headers,
            )
        except EbbmemApiError as exc:
            if effective_fail_open and _is_fail_open_error(exc):
                self._log_warning(f"recall degraded error={type(exc).__name__}: {exc}")
                return _degraded_recall(query)
            raise
        except TimeoutError as exc:
            if effective_fail_open:
                self._log_warning(f"recall degraded error={type(exc).__name__}")
                return _degraded_recall(query)
            raise

        self.tracker.record_exposure(resolved.namespace, resp, turn_id=turn_id)
        return resp

    async def list_memories(
        self,
        *,
        tier: str | None = None,
        user_id: str | None = None,
        app_namespace: str | None = None,
        header_namespace: str | None = None,
        body_namespace: str | None = None,
        session_id: str | None = None,
        thread_id: str | None = None,
        tuple_segments: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        domain: str | None = None,
        tombstoned: bool = False,
        search: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
        min_importance: float | None = None,
        min_valence: float | None = None,
        max_valence: float | None = None,
        metadata_filter: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
        fail_open: bool | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        list_fn = getattr(self.transport, "list_memories", None)
        if not callable(list_fn):
            raise UnsupportedCapabilityError(
                "Configured transport does not implement list_memories"
            )
        resolved = self.scope.resolve(
            user_id=user_id,
            app_namespace=app_namespace,
            header_namespace=header_namespace,
            body_namespace=body_namespace,
            session_id=session_id,
            thread_id=thread_id,
            tuple_segments=tuple_segments,
            tags=tags,
            domain=domain,
        )
        effective_fail_open = self.fail_open if fail_open is None else fail_open
        try:
            return await list_fn(
                namespace=resolved.namespace,
                tier=tier,
                tags=list(resolved.tags) if resolved.tags else None,
                tombstoned=tombstoned,
                search=search,
                session_id=resolved.session_id,
                limit=limit,
                cursor=cursor,
                page=page,
                page_size=page_size,
                min_importance=min_importance,
                min_valence=min_valence,
                max_valence=max_valence,
                metadata_filter=metadata_filter,
                timeout_s=timeout_s,
                headers=headers,
            )
        except EbbmemApiError as exc:
            if effective_fail_open and _is_fail_open_error(exc):
                self._log_warning(f"list_memories degraded error={type(exc).__name__}: {exc}")
                return _degraded_list()
            raise
        except TimeoutError as exc:
            if effective_fail_open:
                self._log_warning(f"list_memories degraded error={type(exc).__name__}")
                return _degraded_list()
            raise

    async def episodic_remember(
        self,
        content: str,
        *,
        thread_key: str | None = None,
        role: str = "user",
        user_id: str | None = None,
        app_namespace: str | None = None,
        header_namespace: str | None = None,
        body_namespace: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        thread_id: str | None = None,
        tuple_segments: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        domain: str | None = None,
        idempotency_key: str | None = None,
        mcp_profile: str = "full",
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        ep_fn = getattr(self.transport, "episodic_remember", None)
        if not callable(ep_fn):
            raise UnsupportedCapabilityError(
                "Configured transport does not implement episodic_remember"
            )
        resolved = self.scope.resolve(
            user_id=user_id,
            app_namespace=app_namespace,
            header_namespace=header_namespace,
            body_namespace=body_namespace,
            agent_id=agent_id,
            session_id=session_id,
            thread_id=thread_id,
            tuple_segments=tuple_segments,
            tags=tags,
            domain=domain,
        )
        effective_thread_key = thread_key if thread_key is not None else resolved.thread_key
        if not isinstance(effective_thread_key, str) or not effective_thread_key.strip():
            raise ValidationError("thread_key is required for episodic_remember")
        effective_content = redact_secrets(content) if self.redact_before_remember else content
        final_tags = resolved.remember_tags(include_domain=True)
        if self.redact_before_remember and final_tags:
            final_tags = [redact_secrets(t) for t in final_tags]
        return await ep_fn(
            thread_key=effective_thread_key.strip(),
            session_id=resolved.session_id,
            content=effective_content,
            role=role,
            tags=final_tags,
            namespace=resolved.namespace,
            idempotency_key=idempotency_key,
            mcp_profile=mcp_profile,
            headers=headers,
        )

    async def episodic_tail(
        self,
        *,
        thread_key: str | None = None,
        limit: int = 10,
        user_id: str | None = None,
        app_namespace: str | None = None,
        header_namespace: str | None = None,
        body_namespace: str | None = None,
        session_id: str | None = None,
        thread_id: str | None = None,
        mcp_profile: str = "full",
        timeout_s: float | None = None,
        fail_open: bool | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        tail_fn = getattr(self.transport, "episodic_tail", None)
        if not callable(tail_fn):
            raise UnsupportedCapabilityError(
                "Configured transport does not implement episodic_tail"
            )
        resolved = self.scope.resolve(
            user_id=user_id,
            app_namespace=app_namespace,
            header_namespace=header_namespace,
            body_namespace=body_namespace,
            session_id=session_id,
            thread_id=thread_id,
        )
        effective_thread_key = thread_key if thread_key is not None else resolved.thread_key
        if not isinstance(effective_thread_key, str) or not effective_thread_key.strip():
            raise ValidationError("thread_key is required for episodic_tail")
        clean_thread_key = effective_thread_key.strip()
        effective_fail_open = self.fail_open if fail_open is None else fail_open
        try:
            return await tail_fn(
                thread_key=clean_thread_key,
                session_id=resolved.session_id,
                limit=limit,
                namespace=resolved.namespace,
                mcp_profile=mcp_profile,
                timeout_s=timeout_s,
                headers=headers,
            )
        except EbbmemApiError as exc:
            if effective_fail_open and _is_fail_open_error(exc):
                self._log_warning(f"episodic_tail degraded error={type(exc).__name__}: {exc}")
                return _degraded_episodic_tail(clean_thread_key)
            raise
        except TimeoutError as exc:
            if effective_fail_open:
                self._log_warning(f"episodic_tail degraded error={type(exc).__name__}")
                return _degraded_episodic_tail(clean_thread_key)
            raise

    def frame_memories(
        self, results_or_response: Sequence[Mapping[str, Any]] | Mapping[str, Any]
    ) -> str:
        if isinstance(results_or_response, Mapping):
            raw = results_or_response.get("results") or []
            return frame_recalled_memories(raw)
        return frame_recalled_memories(results_or_response)

    async def reinforce(
        self,
        memory_id: str,
        *,
        used_ids: Sequence[str] = (),
        recall_id: str | None = None,
        delta: float = DEFAULT_REINFORCE_DELTA,
        clamp_delta: bool = False,
        user_id: str | None = None,
        app_namespace: str | None = None,
        header_namespace: str | None = None,
        body_namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        resolved = self.scope.resolve(
            user_id=user_id,
            app_namespace=app_namespace,
            header_namespace=header_namespace,
            body_namespace=body_namespace,
        )
        validated_delta = validate_delta(delta, clamp=clamp_delta)
        dedup_used = [uid for uid in dict.fromkeys(used_ids) if uid and uid != memory_id]
        return await self.transport.reinforce(
            memory_id=memory_id,
            used_ids=dedup_used,
            recall_id=recall_id,
            delta=validated_delta,
            namespace=resolved.namespace,
            headers=headers,
        )

    async def reinforce_from_exposure(
        self,
        *,
        used_ids: Sequence[str] = (),
        response_text: str | None = None,
        turn_id: str | None = None,
        recall_id: str | None = None,
        delta: float = DEFAULT_REINFORCE_DELTA,
        clamp_delta: bool = False,
        mode: ReinforceMode | None = None,
        injected: bool | None = None,
        user_id: str | None = None,
        app_namespace: str | None = None,
        header_namespace: str | None = None,
        body_namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        resolved = self.scope.resolve(
            user_id=user_id,
            app_namespace=app_namespace,
            header_namespace=header_namespace,
            body_namespace=body_namespace,
        )
        plan: ReinforcePlan | None = self.tracker.plan_reinforcement(
            resolved.namespace,
            used_ids=used_ids,
            response_text=response_text,
            turn_id=turn_id,
            recall_id=recall_id,
            delta=delta,
            clamp_delta=clamp_delta,
            mode=mode,
            injected=injected,
        )
        if plan is None:
            return None
        return await self.transport.reinforce(
            memory_id=plan.memory_id,
            used_ids=plan.used_ids,
            recall_id=plan.recall_id,
            delta=plan.delta,
            namespace=plan.namespace,
            headers=headers,
        )

    async def get(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        app_namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        ns = self.scope.resolve_namespace(user_id=user_id, app_namespace=app_namespace)
        return await self.transport.get(memory_id, namespace=ns, headers=headers)

    async def forget(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        app_namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        ns = self.scope.resolve_namespace(user_id=user_id, app_namespace=app_namespace)
        return await self.transport.forget(memory_id, namespace=ns, headers=headers)

    async def delete(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        app_namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Framework delete always maps strictly to soft-tombstone forget, never purge."""
        return await self.forget(
            memory_id,
            user_id=user_id,
            app_namespace=app_namespace,
            headers=headers,
        )

    async def purge(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        app_namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Explicit admin-only physical erasure."""
        ns = self.scope.resolve_namespace(user_id=user_id, app_namespace=app_namespace)
        return await self.transport.purge(memory_id, namespace=ns, headers=headers)

    async def status(
        self,
        *,
        user_id: str | None = None,
        app_namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        ns = self.scope.resolve_namespace(user_id=user_id, app_namespace=app_namespace)
        return await self.transport.status(namespace=ns, headers=headers)

    async def health(self) -> dict[str, Any]:
        return await self.transport.health()
