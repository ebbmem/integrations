"""Namespace-scoped memory operations for MockEbbmemServer."""

from __future__ import annotations

import re
import uuid
from typing import Any

from ebbmem_conformance.mock_state import (
    VALID_EPISODIC_ROLES,
    VALID_TIERS,
    EpisodicTurn,
    IdempotencyRecord,
    Identity,
    MemoryItem,
    MockServerState,
    _fingerprint_episodic,
    _fingerprint_remember,
    _is_vchar_1_256,
    _parse_finite_float,
    _parse_int,
    _reinforce_one,
    parse_bool_param,
    parse_metadata_filter,
)


def serialize_memory(item: MemoryItem) -> dict[str, Any]:
    return {
        "id": item.memory_id,
        "memory_id": item.memory_id,
        "namespace": item.namespace,
        "content": item.content,
        "tier": item.tier,
        "tags": list(item.tags),
        "importance": item.importance,
        "valence": item.valence,
        "arousal": item.arousal,
        "session_id": item.session_id,
        "metadata": dict(item.metadata),
        "storage_strength": item.storage_strength,
        "recall_count": item.recall_count,
        "created_at_ms": item.created_at_ms,
    }


def get_status(state: MockServerState, namespace: str) -> dict[str, Any]:
    with state.lock:
        items = list(state.memories.get(namespace, {}).values())
        active = sum(1 for item in items if not item.tombstoned)
        tombstoned = sum(1 for item in items if item.tombstoned)
        exposure = namespace in state.exposure_log_enabled_namespaces
    return {
        "status": "ok",
        "namespace": namespace,
        "active_count": active,
        "tombstoned_count": tombstoned,
        "exposure_logging_enabled": exposure,
    }


def create_memory(
    state: MockServerState,
    identity: Identity,
    namespace: str,
    body: dict[str, Any],
    header_idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    if identity.role == "readonly":
        return 403, {"detail": "forbidden_key_scope"}, {}

    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return 422, {"detail": "content_required"}, {}

    tier = body.get("tier", "semantic")
    if not isinstance(tier, str) or tier not in VALID_TIERS:
        return 422, {"detail": "invalid_tier"}, {}

    tags = body.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return 422, {"detail": "invalid_tags"}, {}

    importance = _parse_finite_float(body.get("importance", 0.5))
    if importance is None:
        return 422, {"detail": "invalid_importance"}, {}
    valence = _parse_finite_float(body.get("valence", 0.0))
    if valence is None:
        return 422, {"detail": "invalid_valence"}, {}
    arousal = _parse_finite_float(body.get("arousal", 0.2))
    if arousal is None:
        return 422, {"detail": "invalid_arousal"}, {}
    session_id = body.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        return 422, {"detail": "invalid_session_id"}, {}

    raw_ts = body.get("timestamp_ms")
    explicit_ts: int | None = None
    if raw_ts is not None:
        explicit_ts = _parse_int(raw_ts)
        if explicit_ts is None:
            return 422, {"detail": "invalid_timestamp_ms"}, {}

    metadata = body.get("metadata", {})
    if not isinstance(metadata, dict):
        return 422, {"detail": "invalid_metadata"}, {}
    if any(str(k).startswith("ebb.") for k in metadata):
        return 400, {"detail": "reserved_metadata_prefix"}, {}

    body_idem_key = body.get("idempotency_key")
    if (
        header_idempotency_key is not None
        and body_idem_key is not None
        and header_idempotency_key != body_idem_key
    ):
        return 400, {"code": "EBB-100-022", "detail": "idempotency_key_mismatch"}, {}

    effective_idem_key = (
        header_idempotency_key if header_idempotency_key is not None else body_idem_key
    )
    if effective_idem_key is not None and not _is_vchar_1_256(effective_idem_key):
        return 400, {"code": "EBB-100-021", "detail": "invalid_idempotency_key"}, {}

    fingerprint = _fingerprint_remember(
        namespace=namespace,
        content=content,
        tier=tier,
        tags=list(tags),
        importance=importance,
        valence=valence,
        arousal=arousal,
        session_id=session_id,
        metadata=dict(metadata),
    )

    with state.lock:
        if effective_idem_key is not None:
            if effective_idem_key in state.in_flight_keys:
                return (
                    409,
                    {"code": "EBB-700-023", "detail": "idempotency_in_flight"},
                    {"Retry-After": "1"},
                )
            existing = state.idempotency.get((namespace, effective_idem_key))
            if existing is not None:
                if existing.purged:
                    return (
                        410,
                        {"code": "EBB-100-025", "detail": "idempotency_target_purged"},
                        {},
                    )
                if existing.fingerprint != fingerprint:
                    return (
                        422,
                        {"code": "EBB-100-024", "detail": "idempotency_payload_mismatch"},
                        {},
                    )
                replay_payload = dict(existing.response)
                replay_payload["replayed"] = True
                return 201, replay_payload, {"Idempotent-Replayed": "true"}

        mem_id = str(uuid.uuid4())
        now_ms = explicit_ts if explicit_ts is not None else state.clock_ms
        item = MemoryItem(
            memory_id=mem_id,
            namespace=namespace,
            content=content,
            tier=tier,
            tags=list(tags),
            importance=importance,
            valence=valence,
            arousal=arousal,
            session_id=session_id,
            metadata=dict(metadata),
            storage_strength=1.0,
            recall_count=0,
            reinforce_count=0,
            created_at_ms=now_ms,
            accessed_at_ms=now_ms,
        )
        state.memories.setdefault(namespace, {})[mem_id] = item
        response = {
            "id": mem_id,
            "memory_id": mem_id,
            "namespace": namespace,
            "tier": tier,
            "storage_strength": 1.0,
            "session_id": session_id,
            "tags": list(tags),
            "created_at_ms": now_ms,
            "replayed": False,
        }
        if effective_idem_key is not None:
            state.idempotency[(namespace, effective_idem_key)] = IdempotencyRecord(
                fingerprint=fingerprint,
                memory_id=mem_id,
                response=dict(response),
            )
    return 201, response, {}


def execute_recall(
    state: MockServerState,
    namespace: str,
    query_text: str,
    limit: int = 10,
    tier_filter: str | None = None,
    tags_filter: list[str] | None = None,
    session_filter: str | None = None,
    hex_recall_id: bool = False,
) -> dict[str, Any]:
    # Strictly read-only: never modifies storage_strength, recall_count, or accessed_at_ms.
    query_terms = {term.lower() for term in re.findall(r"[A-Za-z0-9_]+", query_text)}
    with state.lock:
        candidates = list(state.memories.get(namespace, {}).values())
        scored: list[tuple[float, MemoryItem]] = []
        for item in candidates:
            if item.tombstoned:
                continue
            if tier_filter and item.tier != tier_filter:
                continue
            if session_filter and item.session_id != session_filter:
                continue
            if tags_filter and not set(tags_filter).issubset(set(item.tags)):
                continue

            item_terms = {term.lower() for term in re.findall(r"[A-Za-z0-9_]+", item.content)} | {
                t.lower() for t in item.tags
            }
            if query_terms:
                overlap = len(query_terms & item_terms) / len(query_terms)
            else:
                overlap = 0.5
            if query_terms and overlap == 0.0 and query_text.lower() not in item.content.lower():
                continue
            strength_boost = 0.1 * item.storage_strength
            score = round(overlap + strength_boost + 0.05 * item.importance, 6)
            scored.append((score, item))

        scored.sort(key=lambda pair: (pair[0], pair[1].created_at_ms), reverse=True)
        selected = scored[:limit]

        results: list[dict[str, Any]] = []
        for score, item in selected:
            results.append(
                {
                    "id": item.memory_id,
                    "memory_id": item.memory_id,
                    "tier": item.tier,
                    "content": item.content,
                    "score": score,
                    "tags": list(item.tags),
                    "session_id": item.session_id,
                    "breakdown": {
                        "vector_similarity": round(score * 0.6, 6),
                        "bm25_score": round(score * 0.4, 6),
                        "valence_factor": round(1.0 + abs(item.valence) * 0.1, 6),
                        "decay_factor": 1.0,
                        "cognitive_mass": round(item.storage_strength * item.importance, 6),
                        "storage_strength": item.storage_strength,
                        "hebbian_boost": round(0.1 * item.storage_strength, 6),
                    },
                    "provenance": {
                        "source": "mock",
                        "namespace": item.namespace,
                        "session_id": item.session_id,
                    },
                }
            )

        payload: dict[str, Any] = {
            "query": query_text,
            "count": len(results),
            "results": results,
        }
        if namespace in state.exposure_log_enabled_namespaces:
            raw_uuid = uuid.uuid4()
            recall_id = raw_uuid.hex if hex_recall_id else str(raw_uuid)
            state.exposure_log[recall_id] = {
                "namespace": namespace,
                "memory_ids": [r["memory_id"] for r in results],
                "created_at_ms": state.clock_ms,
            }
            payload["recall_id"] = recall_id

    return payload


def execute_list_memories(
    state: MockServerState,
    namespace: str,
    query: dict[str, list[str]],
) -> tuple[int, dict[str, Any]]:
    """Strictly read-only paginated listing for GET /api/v1/memory."""
    limit_raw = query.get("limit", [None])[0]
    page_size_raw = query.get("page_size", [None])[0]
    parsed_limit: int | None = None
    parsed_page_size: int | None = None

    if limit_raw is not None:
        parsed_limit = _parse_int(limit_raw)
        if parsed_limit is None or parsed_limit < 1 or parsed_limit > 100:
            return 422, {"detail": "invalid_limit"}
    if page_size_raw is not None:
        parsed_page_size = _parse_int(page_size_raw)
        if parsed_page_size is None or parsed_page_size < 1 or parsed_page_size > 100:
            return 422, {"detail": "invalid_page_size"}
    effective_limit = (
        parsed_limit
        if parsed_limit is not None
        else (parsed_page_size if parsed_page_size is not None else 50)
    )

    cursor_raw = query.get("cursor", [None])[0]
    parsed_cursor: int | None = None
    if cursor_raw is not None:
        parsed_cursor = _parse_int(cursor_raw)
        if parsed_cursor is None or parsed_cursor < 0:
            return 422, {"detail": "invalid_cursor"}

    page_raw = query.get("page", [None])[0]
    page = 1
    if page_raw is not None:
        parsed_page = _parse_int(page_raw)
        if parsed_page is None or parsed_page < 1:
            return 422, {"detail": "invalid_page"}
        page = parsed_page

    offset = parsed_cursor if parsed_cursor is not None else (page - 1) * effective_limit

    tier_filter = query.get("tier", [None])[0]
    if tier_filter is not None and tier_filter not in VALID_TIERS:
        return 422, {"detail": "invalid_tier"}

    tags_filter: list[str] = []
    for raw_tag in query.get("tags", []):
        for part in raw_tag.split(","):
            cleaned = part.strip()
            if cleaned:
                tags_filter.append(cleaned)

    tombstoned_raw = query.get("tombstoned", [None])[0]
    tombstoned_bool = False
    if tombstoned_raw is not None:
        parsed_tomb = parse_bool_param(tombstoned_raw)
        if parsed_tomb is None:
            return 422, {"detail": "invalid_tombstoned"}
        tombstoned_bool = parsed_tomb

    search_raw = query.get("search", [None])[0]
    search_lower = search_raw.lower() if search_raw else ""
    session_filter = query.get("session_id", [None])[0]

    min_importance: float | None = None
    min_imp_raw = query.get("min_importance", [None])[0]
    if min_imp_raw is not None:
        min_importance = _parse_finite_float(min_imp_raw)
        if min_importance is None:
            return 422, {"detail": "invalid_min_importance"}

    min_valence: float | None = None
    min_val_raw = query.get("min_valence", [None])[0]
    if min_val_raw is not None:
        min_valence = _parse_finite_float(min_val_raw)
        if min_valence is None:
            return 422, {"detail": "invalid_min_valence"}

    max_valence: float | None = None
    max_val_raw = query.get("max_valence", [None])[0]
    if max_val_raw is not None:
        max_valence = _parse_finite_float(max_val_raw)
        if max_valence is None:
            return 422, {"detail": "invalid_max_valence"}

    mf_raw = query.get("metadata_filter", [None])[0]
    metadata_filter: dict[str, Any] | None = None
    if mf_raw is not None:
        metadata_filter = parse_metadata_filter(mf_raw)
        if metadata_filter is None:
            return 422, {"detail": "invalid_metadata_filter"}

    with state.lock:
        candidates = list(state.memories.get(namespace, {}).values())
        matched: list[MemoryItem] = []
        for item in candidates:
            if item.tombstoned is not tombstoned_bool:
                continue
            if tier_filter is not None and item.tier != tier_filter:
                continue
            if session_filter is not None and item.session_id != session_filter:
                continue
            if tags_filter and not set(tags_filter).issubset(set(item.tags)):
                continue
            if search_lower:
                in_content = search_lower in item.content.lower()
                in_tags = any(search_lower in t.lower() for t in item.tags)
                if not in_content and not in_tags:
                    continue
            if min_importance is not None and item.importance < min_importance:
                continue
            if min_valence is not None and item.valence < min_valence:
                continue
            if max_valence is not None and item.valence > max_valence:
                continue
            if metadata_filter is not None and not all(
                k in item.metadata and item.metadata[k] == v for k, v in metadata_filter.items()
            ):
                continue
            matched.append(item)

        matched.sort(key=lambda item: (item.created_at_ms, item.memory_id), reverse=True)
        total = len(matched)
        page_items = matched[offset : offset + effective_limit]
        items_out = [
            {**serialize_memory(item), "tombstoned": item.tombstoned} for item in page_items
        ]
        rows_out = [dict(row) for row in items_out]
        has_more = (offset + len(items_out)) < total
        next_cursor = str(offset + len(items_out)) if has_more else None

    return 200, {
        "items": items_out,
        "rows": rows_out,
        "total": total,
        "total_count": total,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def _resolve_thread_key(arguments: dict[str, Any]) -> str | None:
    raw_thread_key = arguments.get("thread_key")
    raw_session_id = arguments.get("session_id")
    if raw_thread_key is not None:
        if not isinstance(raw_thread_key, str) or not raw_thread_key.strip():
            return None
        return raw_thread_key
    if raw_session_id is not None:
        if not isinstance(raw_session_id, str) or not raw_session_id.strip():
            return None
        return raw_session_id
    return "default"


def execute_episodic_remember(
    state: MockServerState,
    identity: Identity,
    namespace: str,
    arguments: dict[str, Any],
    header_idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    """Stateful MCP memory_episodic_remember handler."""
    if identity.role == "readonly":
        return 403, None, {"code": -32003, "message": "Readonly key cannot write episodic turns"}

    content = arguments.get("content")
    if not isinstance(content, str) or not content.strip():
        return 422, None, {"code": -32602, "message": "content_required"}

    thread_key = _resolve_thread_key(arguments)
    if thread_key is None:
        return 422, None, {"code": -32602, "message": "invalid_thread_key"}

    raw_role = arguments.get("role")
    role = "user" if raw_role is None else raw_role
    if not isinstance(role, str) or role not in VALID_EPISODIC_ROLES:
        return 422, None, {"code": -32602, "message": "invalid_role"}

    raw_tags = arguments.get("tags")
    tags = [] if raw_tags is None else raw_tags
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return 422, None, {"code": -32602, "message": "invalid_tags"}

    body_idem_key = arguments.get("idempotency_key")
    if (
        header_idempotency_key is not None
        and body_idem_key is not None
        and header_idempotency_key != body_idem_key
    ):
        return (
            400,
            None,
            {
                "code": -32602,
                "message": "EBB-100-022: idempotency_key_mismatch",
                "data": {"code": "EBB-100-022"},
            },
        )

    effective_idem_key = (
        header_idempotency_key if header_idempotency_key is not None else body_idem_key
    )
    if effective_idem_key is not None and not _is_vchar_1_256(effective_idem_key):
        return (
            400,
            None,
            {
                "code": -32602,
                "message": "EBB-100-021: invalid_idempotency_key",
                "data": {"code": "EBB-100-021"},
            },
        )

    fingerprint = _fingerprint_episodic(
        namespace=namespace,
        thread_key=thread_key,
        role=role,
        content=content,
        tags=list(tags),
    )

    with state.lock:
        if effective_idem_key is not None:
            if effective_idem_key in state.in_flight_keys:
                return (
                    409,
                    None,
                    {
                        "code": -32000,
                        "message": "EBB-700-023: idempotency_in_flight",
                        "data": {"code": "EBB-700-023"},
                    },
                )
            existing = state.episodic_idempotency.get((namespace, effective_idem_key))
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    return (
                        422,
                        None,
                        {
                            "code": -32000,
                            "message": "EBB-100-024: idempotency_payload_mismatch",
                            "data": {"code": "EBB-100-024"},
                        },
                    )
                replay_payload = dict(existing.response)
                replay_payload["replayed"] = True
                return 200, replay_payload, None

        turns = state.episodic_threads.setdefault((namespace, thread_key), [])
        sequence_id = len(turns) + 1
        mem_id = str(uuid.uuid4())
        now_ms = state.clock_ms
        turn = EpisodicTurn(
            sequence_id=sequence_id,
            namespace=namespace,
            thread_key=thread_key,
            role=role,
            content=content,
            tags=list(tags),
            memory_id=mem_id,
            created_at_ms=now_ms,
        )
        turns.append(turn)

        item = MemoryItem(
            memory_id=mem_id,
            namespace=namespace,
            content=content,
            tier="episodic",
            tags=list(tags),
            importance=0.5,
            valence=0.0,
            arousal=0.2,
            session_id=thread_key,
            metadata={"role": role, "sequence_id": sequence_id, "thread_key": thread_key},
            storage_strength=1.0,
            recall_count=0,
            reinforce_count=0,
            created_at_ms=now_ms,
            accessed_at_ms=now_ms,
        )
        state.memories.setdefault(namespace, {})[mem_id] = item

        response = {
            "status": "remembered",
            "id": mem_id,
            "memory_id": mem_id,
            "namespace": namespace,
            "thread_key": thread_key,
            "session_id": thread_key,
            "role": role,
            "sequence_id": sequence_id,
            "message_count": len(turns),
            "tags": list(tags),
            "tier": "episodic",
            "storage_strength": 1.0,
            "created_at_ms": now_ms,
            "replayed": False,
        }
        if effective_idem_key is not None:
            state.episodic_idempotency[(namespace, effective_idem_key)] = IdempotencyRecord(
                fingerprint=fingerprint,
                memory_id=mem_id,
                response=dict(response),
            )
    return 200, response, None


def execute_episodic_tail(
    state: MockServerState,
    namespace: str,
    arguments: dict[str, Any],
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    """Strictly read-only MCP memory_episodic_tail handler."""
    thread_key = _resolve_thread_key(arguments)
    if thread_key is None:
        return 400, None, {"code": -32602, "message": "Invalid params: thread_key"}

    raw_limit = arguments.get("limit", 10)
    limit = 10 if raw_limit is None else _parse_int(raw_limit)
    if limit is None or limit < 1 or limit > 100:
        return 400, None, {"code": -32602, "message": "Invalid params: limit"}

    with state.lock:
        turns = state.episodic_threads.get((namespace, thread_key), [])
        selected = turns[-limit:]
        turns_out = [
            {
                "sequence_id": t.sequence_id,
                "thread_key": t.thread_key,
                "role": t.role,
                "content": t.content,
                "tags": list(t.tags),
                "memory_id": t.memory_id,
                "created_at_ms": t.created_at_ms,
            }
            for t in selected
        ]
    return (
        200,
        {
            "status": "ok",
            "namespace": namespace,
            "thread_key": thread_key,
            "count": len(turns_out),
            "turns": turns_out,
        },
        None,
    )


def execute_reinforce(
    state: MockServerState,
    identity: Identity,
    namespace: str,
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    if identity.role == "readonly":
        return 403, {"detail": "forbidden_key_scope"}

    memory_id = body.get("memory_id")
    if not isinstance(memory_id, str) or not memory_id.strip():
        return 422, {"detail": "memory_id_required"}

    raw_delta = body.get("delta", 0.1)
    delta = _parse_finite_float(raw_delta)
    if delta is None:
        return 422, {"detail": "invalid_delta"}
    if delta < -1.0 or delta > 1.0:
        return 422, {"detail": "delta_out_of_bounds"}

    recall_id = body.get("recall_id")
    if recall_id is not None and (not isinstance(recall_id, str) or not recall_id.strip()):
        return 422, {"detail": "invalid_recall_id"}

    used_ids = body.get("used_ids", [])
    if not isinstance(used_ids, list) or not all(isinstance(u, str) and u for u in used_ids):
        return 422, {"detail": "invalid_used_ids"}

    targets = [memory_id] + [uid for uid in used_ids if uid != memory_id]

    with state.lock:
        ns_store = state.memories.get(namespace, {})
        resolved_targets: list[tuple[str, MemoryItem]] = []
        for tid in targets:
            target_item = ns_store.get(tid)
            if target_item is None or target_item.tombstoned:
                return 404, {"detail": "Not Found"}
            resolved_targets.append((tid, target_item))

        primary = resolved_targets[0][1]
        outcomes: dict[str, str] = {}
        for tid, target_item in resolved_targets:
            outcome, _ = _reinforce_one(target_item, delta, state.clock_ms)
            outcomes[tid] = outcome

        primary_outcome = outcomes[memory_id]
        message = "spaced_out" if primary_outcome == "spaced_out" else "reinforced"
        return 200, {
            "status": "reinforced",
            "outcome": primary_outcome,
            "memory_id": memory_id,
            "delta": delta,
            "storage_strength": primary.storage_strength,
            "reinforced_ids": [tid for tid, out in outcomes.items() if out == "counted"],
            "recall_id": recall_id,
            "message": message,
        }
