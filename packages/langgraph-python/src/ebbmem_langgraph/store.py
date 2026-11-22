"""EbbmemStore: LangGraph BaseStore backed by EbbMem via ebbmem-integration-core."""

from __future__ import annotations

import asyncio
import concurrent.futures as cf
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ebbmem_integration_core import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_REINFORCE_DELTA,
    DEFAULT_TIMEOUT_S,
    AsyncEbbmemClientSdkAdapter,
    AsyncIntegrationClient,
    AsyncMemoryTransport,
    EbbmemClientSdkAdapter,
    ExposureBookkeeper,
    IntegrationClient,
    MemoryTransport,
    ReinforceMode,
    ReinforcePolicy,
    RetryPolicy,
    ScopeResolver,
    SdkCapabilities,
    UnsupportedCapabilityError,
    make_idempotency_key,
)
from langgraph.store.base import (
    NOT_PROVIDED,
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
    _validate_namespace,
    _validate_namespace_labels,
    validate_op_namespace,
)

from ebbmem_langgraph._encoding import (
    META_CREATED_AT,
    META_INDEXED,
    META_KEY,
    META_NAMESPACE,
    META_UPDATED_AT,
    META_VALUE,
    STORE_TAG,
    build_store_tags,
    composite_external_key,
    decode_stored_record,
    extract_searchable_content,
    item_tag,
    normalize_value,
    prefix_tag,
)
from ebbmem_langgraph._filters import filter_and_sort_namespaces, matches_filter

DEFAULT_SCAN_LIMIT = 100


def _run_coro_sync(coro: Any) -> Any:
    """Run an async coroutine from sync code, even inside a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with cf.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class EbbmemStore(BaseStore):
    """LangGraph `BaseStore` backed by EbbMem with IC-1 scoping, security, and reinforcement.

    No process-local item dictionary is kept: all items, overwrites, tombstones,
    and namespace metadata live in EbbMem.

    Namespace mapping (IC-1):
    - EbbMem's top-level `namespace` is resolved via `ScopeResolver` (`{tenant}:u:{user_id}`
      or explicit `app_namespace`, or optional `user_id_from_namespace` extractor), preventing
      per-tuple namespace explosion.
    - LangGraph `namespace` tuples are mapped into normalized IC-1 `tags` (with `session:<id>`
      extracted into `session_id`) plus deterministic hierarchy tags and `metadata` fields so
      exact `get`, `search(namespace_prefix=...)`, and `list_namespaces(...)` preserve full
      LangGraph hierarchy semantics.
    """

    supports_ttl: bool = False

    def __init__(
        self,
        base_url: str | None = None,
        *,
        bearer_token: str | None = None,
        api_key: str | None = None,
        scope: ScopeResolver | None = None,
        tenant: str = "default",
        user_id: str | None = None,
        app_namespace: str | None = None,
        agent_id: str | None = None,
        default_session_id: str | None = None,
        user_id_from_namespace: Callable[[tuple[str, ...]], str | None] | None = None,
        client: IntegrationClient | None = None,
        async_client: AsyncIntegrationClient | None = None,
        transport: MemoryTransport | None = None,
        async_transport: AsyncMemoryTransport | None = None,
        sdk_client: Any | None = None,
        async_sdk_client: Any | None = None,
        tier: str = "semantic",
        fail_open: bool = True,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_policy: RetryPolicy | None = None,
        reinforce_policy: ReinforcePolicy | None = None,
        exposure_bookkeeper: ExposureBookkeeper | None = None,
        telemetry_enabled: bool = False,
        redact_before_remember: bool = True,
        scan_limit: int = DEFAULT_SCAN_LIMIT,
        index_fields: Sequence[str] | None = None,
        strict_sdk_capabilities: bool = False,
        log_sink: list[str] | None = None,
    ) -> None:
        if scan_limit < 1:
            raise ValueError("scan_limit must be >= 1")
        if scope is None:
            if app_namespace is None and user_id is None and user_id_from_namespace is None:
                scope = ScopeResolver(
                    tenant=tenant,
                    app_namespace=f"{tenant}:u:default",
                    agent_id=agent_id,
                    default_session_id=default_session_id,
                )
            else:
                scope = ScopeResolver(
                    tenant=tenant,
                    user_id=user_id,
                    app_namespace=app_namespace,
                    agent_id=agent_id,
                    default_session_id=default_session_id,
                )
        self.scope = scope
        self.user_id_from_namespace = user_id_from_namespace
        self.tier = tier
        self.fail_open = fail_open
        self.scan_limit = scan_limit
        self.index_fields = list(index_fields) if index_fields is not None else None
        self.strict_sdk_capabilities = strict_sdk_capabilities
        self.log_sink: list[str] = log_sink if log_sink is not None else []
        self.reinforce_policy = (
            reinforce_policy if reinforce_policy is not None else ReinforcePolicy()
        )
        self.exposure_bookkeeper = (
            exposure_bookkeeper if exposure_bookkeeper is not None else ExposureBookkeeper()
        )

        effective_retry = (
            retry_policy
            if retry_policy is not None
            else RetryPolicy(timeout_s=timeout_s, max_retries=max_retries)
        )

        if transport is None and sdk_client is not None:
            transport = EbbmemClientSdkAdapter(sdk_client)
        if async_transport is None and async_sdk_client is not None:
            async_transport = AsyncEbbmemClientSdkAdapter(async_sdk_client)

        self._sync_client: IntegrationClient | None = client
        if self._sync_client is None and (transport is not None or base_url is not None):
            self._sync_client = IntegrationClient(
                base_url,
                bearer_token=bearer_token,
                api_key=api_key,
                scope=self.scope,
                transport=transport,
                fail_open=fail_open,
                retry_policy=effective_retry,
                reinforce_policy=self.reinforce_policy,
                exposure_bookkeeper=self.exposure_bookkeeper,
                telemetry_enabled=telemetry_enabled,
                redact_before_remember=redact_before_remember,
                log_sink=self.log_sink,
            )

        self._async_client: AsyncIntegrationClient | None = async_client
        if self._async_client is None and (async_transport is not None or base_url is not None):
            self._async_client = AsyncIntegrationClient(
                base_url,
                bearer_token=bearer_token,
                api_key=api_key,
                scope=self.scope,
                transport=async_transport,
                fail_open=fail_open,
                retry_policy=effective_retry,
                reinforce_policy=self.reinforce_policy,
                exposure_bookkeeper=self.exposure_bookkeeper,
                telemetry_enabled=telemetry_enabled,
                redact_before_remember=redact_before_remember,
                log_sink=self.log_sink,
            )

        if self._sync_client is None and self._async_client is None:
            raise ValueError(
                "Provide base_url, client/async_client, transport/async_transport, or sdk_client"
            )

    @property
    def sync_client(self) -> IntegrationClient | None:
        return self._sync_client

    @property
    def async_client(self) -> AsyncIntegrationClient | None:
        return self._async_client

    @property
    def capabilities(self) -> SdkCapabilities:
        if self._async_client is not None:
            return self._async_client.transport.capabilities
        assert self._sync_client is not None
        return self._sync_client.transport.capabilities

    def _resolve_scope_args(self, namespace: tuple[str, ...]) -> dict[str, Any]:
        extracted_uid = (
            self.user_id_from_namespace(namespace)
            if self.user_id_from_namespace is not None
            else None
        )
        if extracted_uid:
            return {"user_id": extracted_uid}
        if self.scope.app_namespace is None and self.scope.user_id is None:
            return {"user_id": "default"}
        return {}

    def _validate_op(self, op: Op) -> None:
        validate_op_namespace(op)
        if isinstance(op, PutOp):
            if op.value is not None:
                _validate_namespace(op.namespace)
                if op.ttl not in (NOT_PROVIDED, None) and not self.supports_ttl:
                    raise NotImplementedError(f"TTL is not supported by {self.__class__.__name__}.")
            else:
                _validate_namespace_labels(op.namespace)

    # ------------------------------------------------------------------
    # BaseStore sync / async batch contract
    # ------------------------------------------------------------------

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        if self._sync_client is None:
            return _run_coro_sync(self.abatch(list(ops)))

        ops_list = list(ops)
        for op in ops_list:
            self._validate_op(op)

        results: list[Result] = []
        put_ops: dict[tuple[tuple[str, ...], str], PutOp] = {}
        for op in ops_list:
            if isinstance(op, GetOp):
                results.append(self._sync_get_item(op.namespace, op.key))
            elif isinstance(op, SearchOp):
                results.append(self._sync_search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(self._sync_list_namespaces(op))
            elif isinstance(op, PutOp):
                put_ops[(op.namespace, op.key)] = op
                results.append(None)
            else:
                raise ValueError(f"Unknown operation type: {type(op)}")

        for op in put_ops.values():
            if op.value is None:
                self._sync_delete_item(op.namespace, op.key)
            else:
                self._sync_put_item(op)
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        if self._async_client is None:
            return await asyncio.to_thread(self.batch, list(ops))

        ops_list = list(ops)
        for op in ops_list:
            self._validate_op(op)

        results: list[Result] = []
        put_ops: dict[tuple[tuple[str, ...], str], PutOp] = {}
        for op in ops_list:
            if isinstance(op, GetOp):
                results.append(await self._async_get_item(op.namespace, op.key))
            elif isinstance(op, SearchOp):
                results.append(await self._async_search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(await self._async_list_namespaces(op))
            elif isinstance(op, PutOp):
                put_ops[(op.namespace, op.key)] = op
                results.append(None)
            else:
                raise ValueError(f"Unknown operation type: {type(op)}")

        for op in put_ops.values():
            if op.value is None:
                await self._async_delete_item(op.namespace, op.key)
            else:
                await self._async_put_item(op)
        return results

    # ------------------------------------------------------------------
    # Reinforcement & IC-1 helper methods
    # ------------------------------------------------------------------

    def reinforce(
        self,
        memory_id: str,
        *,
        used_ids: Sequence[str] = (),
        recall_id: str | None = None,
        delta: float = DEFAULT_REINFORCE_DELTA,
        namespace: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        if self._sync_client is None:
            return _run_coro_sync(
                self.areinforce(
                    memory_id,
                    used_ids=used_ids,
                    recall_id=recall_id,
                    delta=delta,
                    namespace=namespace,
                )
            )
        scope_args = self._resolve_scope_args(namespace) if namespace else {}
        return self._sync_client.reinforce(
            memory_id,
            used_ids=used_ids,
            recall_id=recall_id,
            delta=delta,
            **scope_args,
        )

    async def areinforce(
        self,
        memory_id: str,
        *,
        used_ids: Sequence[str] = (),
        recall_id: str | None = None,
        delta: float = DEFAULT_REINFORCE_DELTA,
        namespace: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        if self._async_client is None:
            return await asyncio.to_thread(
                self.reinforce,
                memory_id,
                used_ids=used_ids,
                recall_id=recall_id,
                delta=delta,
                namespace=namespace,
            )
        scope_args = self._resolve_scope_args(namespace) if namespace else {}
        return await self._async_client.reinforce(
            memory_id,
            used_ids=used_ids,
            recall_id=recall_id,
            delta=delta,
            **scope_args,
        )

    def reinforce_from_exposure(
        self,
        *,
        used_ids: Sequence[str] = (),
        response_text: str | None = None,
        turn_id: str | None = None,
        recall_id: str | None = None,
        delta: float = DEFAULT_REINFORCE_DELTA,
        mode: ReinforceMode | None = None,
        injected: bool | None = None,
        namespace: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        if self._sync_client is None:
            return _run_coro_sync(
                self.areinforce_from_exposure(
                    used_ids=used_ids,
                    response_text=response_text,
                    turn_id=turn_id,
                    recall_id=recall_id,
                    delta=delta,
                    mode=mode,
                    injected=injected,
                    namespace=namespace,
                )
            )
        scope_args = self._resolve_scope_args(namespace) if namespace else {}
        return self._sync_client.reinforce_from_exposure(
            used_ids=used_ids,
            response_text=response_text,
            turn_id=turn_id,
            recall_id=recall_id,
            delta=delta,
            mode=mode,
            injected=injected,
            **scope_args,
        )

    async def areinforce_from_exposure(
        self,
        *,
        used_ids: Sequence[str] = (),
        response_text: str | None = None,
        turn_id: str | None = None,
        recall_id: str | None = None,
        delta: float = DEFAULT_REINFORCE_DELTA,
        mode: ReinforceMode | None = None,
        injected: bool | None = None,
        namespace: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        if self._async_client is None:
            return await asyncio.to_thread(
                self.reinforce_from_exposure,
                used_ids=used_ids,
                response_text=response_text,
                turn_id=turn_id,
                recall_id=recall_id,
                delta=delta,
                mode=mode,
                injected=injected,
                namespace=namespace,
            )
        scope_args = self._resolve_scope_args(namespace) if namespace else {}
        return await self._async_client.reinforce_from_exposure(
            used_ids=used_ids,
            response_text=response_text,
            turn_id=turn_id,
            recall_id=recall_id,
            delta=delta,
            mode=mode,
            injected=injected,
            **scope_args,
        )

    def get_memory_id(self, namespace: tuple[str, ...], key: str) -> str | None:
        """Return the underlying EbbMem `memory_id` for `(namespace, key)`, if present."""
        _validate_namespace_labels(namespace)
        if self._sync_client is None:
            return _run_coro_sync(self.aget_memory_id(namespace, key))
        matches = self._sync_find_exact_records(namespace, key, fail_open=self.fail_open)
        return matches[0][0] if matches else None

    async def aget_memory_id(self, namespace: tuple[str, ...], key: str) -> str | None:
        """Async version of `get_memory_id`."""
        _validate_namespace_labels(namespace)
        if self._async_client is None:
            return await asyncio.to_thread(self.get_memory_id, namespace, key)
        matches = await self._async_find_exact_records(namespace, key, fail_open=self.fail_open)
        return matches[0][0] if matches else None

    def frame_memories(self, items: Sequence[Item | SearchItem]) -> str:
        """Format LangGraph `Item`s inside IC-1 untrusted memory XML delimiters."""
        client = self._sync_client or self._async_client
        assert client is not None
        payloads = [
            {
                "memory_id": f"{'/'.join(it.namespace)}/{it.key}",
                "tier": self.tier,
                "content": it.value,
            }
            for it in items
        ]
        return client.frame_memories(payloads)

    # ------------------------------------------------------------------
    # Shared Sync / Async Transformation Helpers
    # ------------------------------------------------------------------

    def _build_remember_kwargs(
        self,
        op: PutOp,
        existing: Sequence[tuple[str, dict[str, Any], Item, int]],
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        assert op.value is not None
        plain_value = normalize_value(op.value)
        content, is_indexed = extract_searchable_content(
            plain_value, index=op.index, default_fields=self.index_fields
        )
        now = datetime.now(UTC)
        created_at = existing[0][2].created_at if existing else now
        scope_args = self._resolve_scope_args(op.namespace)
        resolved = self.scope.resolve(tuple_segments=op.namespace, **scope_args)
        metadata = {
            META_KEY: op.key,
            META_NAMESPACE: list(op.namespace),
            META_VALUE: plain_value,
            META_CREATED_AT: created_at.isoformat(),
            META_UPDATED_AT: now.isoformat(),
            META_INDEXED: is_indexed,
        }
        idem = make_idempotency_key(
            "lg_put", resolved.namespace, op.namespace, op.key, uuid.uuid4().hex
        )
        kwargs: dict[str, Any] = {
            "tier": self.tier,
            "tuple_segments": op.namespace,
            "tags": build_store_tags(op.namespace, op.key),
            "metadata": metadata,
            "idempotency_key": idem,
            **scope_args,
        }
        return content, kwargs, scope_args

    @staticmethod
    def _filter_exact_pairs(
        namespace: tuple[str, ...],
        key: str,
        pairs: Sequence[tuple[Mapping[str, Any], dict[str, Any]]],
    ) -> list[tuple[str, dict[str, Any], Item, int]]:
        matched: list[tuple[str, dict[str, Any], Item, int]] = []
        for _, rec in pairs:
            decoded = decode_stored_record(rec)
            if decoded is None:
                continue
            item, _, ts = decoded
            if item.namespace == namespace and item.key == key:
                mem_id = str(rec.get("memory_id") or rec.get("id") or "")
                matched.append((mem_id, rec, item, ts))
        matched.sort(key=lambda entry: entry[3], reverse=True)
        return matched

    @staticmethod
    def _filter_and_dedup_search(
        op: SearchOp,
        pairs: Sequence[tuple[Mapping[str, Any], dict[str, Any]]],
    ) -> list[SearchItem]:
        dedup: dict[tuple[tuple[str, ...], str], tuple[SearchItem, int]] = {}
        n = len(op.namespace_prefix)
        for hit, rec in pairs:
            raw_score = hit.get("score")
            score = float(raw_score) if op.query and raw_score is not None else None
            decoded = decode_stored_record(rec, score=score, as_search_item=True)
            if decoded is None:
                continue
            item, is_indexed, ts = decoded
            assert isinstance(item, SearchItem)
            if op.query and not is_indexed:
                continue
            if item.namespace[:n] != op.namespace_prefix:
                continue
            if not matches_filter(item.value, op.filter):
                continue
            dedup_key = (item.namespace, item.key)
            if dedup_key not in dedup or ts > dedup[dedup_key][1]:
                dedup[dedup_key] = (item, ts)

        items = [pair[0] for pair in dedup.values()]
        start = max(0, op.offset)
        return items[start : start + op.limit]

    @staticmethod
    def _collect_namespaces(
        op: ListNamespacesOp,
        pairs: Sequence[tuple[Mapping[str, Any], dict[str, Any]]],
    ) -> list[tuple[str, ...]]:
        namespaces: set[tuple[str, ...]] = set()
        for _, rec in pairs:
            decoded = decode_stored_record(rec)
            if decoded is not None:
                namespaces.add(decoded[0].namespace)
        return filter_and_sort_namespaces(namespaces, op)

    def _prepare_recall_args(
        self,
        caps: SdkCapabilities,
        *,
        query: str,
        namespace: tuple[str, ...],
        extra_tags: list[str],
        limit: int,
        fail_open: bool,
    ) -> tuple[str, dict[str, Any]]:
        scope_args = self._resolve_scope_args(namespace)
        if caps.recall_filters:
            return query, {
                "limit": limit,
                "tuple_segments": namespace,
                "tags": extra_tags,
                "fail_open": fail_open,
                **scope_args,
            }
        if self.strict_sdk_capabilities and extra_tags:
            raise UnsupportedCapabilityError(
                "Wrapped 0.2.0 SDK lacks recall tags/tier/session_id filters"
            )
        # ebbmem-client 0.2.0 RecallRequestModel requires query len >= 1 and limit <= 50.
        return query or "*", {
            "limit": min(limit, 50),
            "fail_open": fail_open,
            **scope_args,
        }

    # ------------------------------------------------------------------
    # Internal Sync Helpers
    # ------------------------------------------------------------------

    def _sync_recall_candidates(
        self,
        *,
        query: str,
        namespace: tuple[str, ...],
        extra_tags: list[str],
        limit: int,
        fail_open: bool,
    ) -> list[dict[str, Any]]:
        assert self._sync_client is not None
        effective_query, kwargs = self._prepare_recall_args(
            self._sync_client.transport.capabilities,
            query=query,
            namespace=namespace,
            extra_tags=extra_tags,
            limit=limit,
            fail_open=fail_open,
        )
        return list(self._sync_client.recall(effective_query, **kwargs).get("results") or [])

    def _sync_hydrate_records(
        self,
        hits: Sequence[Mapping[str, Any]],
        namespace_hint: tuple[str, ...],
    ) -> list[tuple[Mapping[str, Any], dict[str, Any]]]:
        assert self._sync_client is not None
        scope_args = self._resolve_scope_args(namespace_hint)
        pairs: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
        for hit in hits:
            if isinstance(hit.get("metadata"), Mapping) and META_VALUE in hit["metadata"]:
                pairs.append((hit, dict(hit)))
                continue
            mem_id = hit.get("memory_id") or hit.get("id")
            if not isinstance(mem_id, str) or not mem_id:
                continue
            rec = self._sync_client.get(mem_id, **scope_args)
            if "score" in hit and "score" not in rec:
                rec["score"] = hit["score"]
            pairs.append((hit, rec))
        return pairs

    def _sync_find_exact_records(
        self,
        namespace: tuple[str, ...],
        key: str,
        *,
        fail_open: bool,
    ) -> list[tuple[str, dict[str, Any], Item, int]]:
        assert self._sync_client is not None
        caps = self._sync_client.transport.capabilities
        scope_args = self._resolve_scope_args(namespace)
        if caps.exact_key_get:
            get_by_key = getattr(self._sync_client.transport, "get_by_key", None)
            if callable(get_by_key):
                resolved_ns = self.scope.resolve_namespace(**scope_args)
                try:
                    rec = get_by_key(composite_external_key(namespace, key), namespace=resolved_ns)
                    return self._filter_exact_pairs(namespace, key, [(rec, rec)])
                except Exception:
                    if not fail_open:
                        raise
                    return []

        hits = self._sync_recall_candidates(
            query="",
            namespace=namespace,
            extra_tags=[item_tag(namespace, key)],
            limit=self.scan_limit,
            fail_open=fail_open,
        )
        return self._filter_exact_pairs(namespace, key, self._sync_hydrate_records(hits, namespace))

    def _sync_get_item(self, namespace: tuple[str, ...], key: str) -> Item | None:
        matches = self._sync_find_exact_records(namespace, key, fail_open=self.fail_open)
        return matches[0][2] if matches else None

    def _sync_put_item(self, op: PutOp) -> None:
        assert self._sync_client is not None
        existing = self._sync_find_exact_records(op.namespace, op.key, fail_open=False)
        content, kwargs, scope_args = self._build_remember_kwargs(op, existing)
        self._sync_client.remember(content, **kwargs)
        for old_id, _, _, _ in existing:
            if old_id:
                self._sync_client.forget(old_id, **scope_args)

    def _sync_delete_item(self, namespace: tuple[str, ...], key: str) -> None:
        assert self._sync_client is not None
        caps = self._sync_client.transport.capabilities
        scope_args = self._resolve_scope_args(namespace)
        if caps.exact_key_delete:
            del_by_key = getattr(self._sync_client.transport, "delete_by_key", None)
            if callable(del_by_key):
                resolved_ns = self.scope.resolve_namespace(**scope_args)
                del_by_key(composite_external_key(namespace, key), namespace=resolved_ns)
                return
        existing = self._sync_find_exact_records(namespace, key, fail_open=False)
        for old_id, _, _, _ in existing:
            if old_id:
                self._sync_client.forget(old_id, **scope_args)

    def _sync_search(self, op: SearchOp) -> list[SearchItem]:
        if op.limit <= 0:
            return []
        prefix = op.namespace_prefix
        extra_tags = [prefix_tag(prefix)] if prefix else [STORE_TAG]
        fetch_limit = min(self.scan_limit, max(op.limit + max(0, op.offset), 50))
        hits = self._sync_recall_candidates(
            query=op.query or "",
            namespace=prefix,
            extra_tags=extra_tags,
            limit=fetch_limit,
            fail_open=self.fail_open,
        )
        return self._filter_and_dedup_search(op, self._sync_hydrate_records(hits, prefix))

    def _sync_list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        if self.strict_sdk_capabilities and not self.capabilities.namespace_prefix_list:
            raise UnsupportedCapabilityError(
                "Wrapped 0.2.0 SDK lacks native list_namespaces capability"
            )
        hits = self._sync_recall_candidates(
            query="",
            namespace=(),
            extra_tags=[STORE_TAG],
            limit=self.scan_limit,
            fail_open=self.fail_open,
        )
        return self._collect_namespaces(op, self._sync_hydrate_records(hits, ()))

    # ------------------------------------------------------------------
    # Internal Async Helpers
    # ------------------------------------------------------------------

    async def _async_recall_candidates(
        self,
        *,
        query: str,
        namespace: tuple[str, ...],
        extra_tags: list[str],
        limit: int,
        fail_open: bool,
    ) -> list[dict[str, Any]]:
        assert self._async_client is not None
        effective_query, kwargs = self._prepare_recall_args(
            self._async_client.transport.capabilities,
            query=query,
            namespace=namespace,
            extra_tags=extra_tags,
            limit=limit,
            fail_open=fail_open,
        )
        resp = await self._async_client.recall(effective_query, **kwargs)
        return list(resp.get("results") or [])

    async def _async_hydrate_records(
        self,
        hits: Sequence[Mapping[str, Any]],
        namespace_hint: tuple[str, ...],
    ) -> list[tuple[Mapping[str, Any], dict[str, Any]]]:
        assert self._async_client is not None
        scope_args = self._resolve_scope_args(namespace_hint)
        sem = asyncio.Semaphore(8)

        async def _one(hit: Mapping[str, Any]) -> tuple[Mapping[str, Any], dict[str, Any]] | None:
            if isinstance(hit.get("metadata"), Mapping) and META_VALUE in hit["metadata"]:
                return hit, dict(hit)
            mem_id = hit.get("memory_id") or hit.get("id")
            if not isinstance(mem_id, str) or not mem_id:
                return None
            async with sem:
                assert self._async_client is not None
                rec = await self._async_client.get(mem_id, **scope_args)
            if "score" in hit and "score" not in rec:
                rec["score"] = hit["score"]
            return hit, rec

        hydrated = await asyncio.gather(*(_one(h) for h in hits))
        return [pair for pair in hydrated if pair is not None]

    async def _async_find_exact_records(
        self,
        namespace: tuple[str, ...],
        key: str,
        *,
        fail_open: bool,
    ) -> list[tuple[str, dict[str, Any], Item, int]]:
        assert self._async_client is not None
        caps = self._async_client.transport.capabilities
        scope_args = self._resolve_scope_args(namespace)
        if caps.exact_key_get:
            get_by_key = getattr(self._async_client.transport, "get_by_key", None)
            if callable(get_by_key):
                resolved_ns = self.scope.resolve_namespace(**scope_args)
                try:
                    rec = await get_by_key(
                        composite_external_key(namespace, key), namespace=resolved_ns
                    )
                    return self._filter_exact_pairs(namespace, key, [(rec, rec)])
                except Exception:
                    if not fail_open:
                        raise
                    return []

        hits = await self._async_recall_candidates(
            query="",
            namespace=namespace,
            extra_tags=[item_tag(namespace, key)],
            limit=self.scan_limit,
            fail_open=fail_open,
        )
        return self._filter_exact_pairs(
            namespace, key, await self._async_hydrate_records(hits, namespace)
        )

    async def _async_get_item(self, namespace: tuple[str, ...], key: str) -> Item | None:
        matches = await self._async_find_exact_records(namespace, key, fail_open=self.fail_open)
        return matches[0][2] if matches else None

    async def _async_put_item(self, op: PutOp) -> None:
        assert self._async_client is not None
        existing = await self._async_find_exact_records(op.namespace, op.key, fail_open=False)
        content, kwargs, scope_args = self._build_remember_kwargs(op, existing)
        await self._async_client.remember(content, **kwargs)
        for old_id, _, _, _ in existing:
            if old_id:
                await self._async_client.forget(old_id, **scope_args)

    async def _async_delete_item(self, namespace: tuple[str, ...], key: str) -> None:
        assert self._async_client is not None
        caps = self._async_client.transport.capabilities
        scope_args = self._resolve_scope_args(namespace)
        if caps.exact_key_delete:
            del_by_key = getattr(self._async_client.transport, "delete_by_key", None)
            if callable(del_by_key):
                resolved_ns = self.scope.resolve_namespace(**scope_args)
                await del_by_key(composite_external_key(namespace, key), namespace=resolved_ns)
                return
        existing = await self._async_find_exact_records(namespace, key, fail_open=False)
        for old_id, _, _, _ in existing:
            if old_id:
                await self._async_client.forget(old_id, **scope_args)

    async def _async_search(self, op: SearchOp) -> list[SearchItem]:
        if op.limit <= 0:
            return []
        prefix = op.namespace_prefix
        extra_tags = [prefix_tag(prefix)] if prefix else [STORE_TAG]
        fetch_limit = min(self.scan_limit, max(op.limit + max(0, op.offset), 50))
        hits = await self._async_recall_candidates(
            query=op.query or "",
            namespace=prefix,
            extra_tags=extra_tags,
            limit=fetch_limit,
            fail_open=self.fail_open,
        )
        return self._filter_and_dedup_search(op, await self._async_hydrate_records(hits, prefix))

    async def _async_list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        if self.strict_sdk_capabilities and not self.capabilities.namespace_prefix_list:
            raise UnsupportedCapabilityError(
                "Wrapped 0.2.0 SDK lacks native list_namespaces capability"
            )
        hits = await self._async_recall_candidates(
            query="",
            namespace=(),
            extra_tags=[STORE_TAG],
            limit=self.scan_limit,
            fail_open=self.fail_open,
        )
        return self._collect_namespaces(op, await self._async_hydrate_records(hits, ()))
