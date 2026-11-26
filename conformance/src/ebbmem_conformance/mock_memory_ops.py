"""Namespace-scoped memory operations for MockEbbmemServer."""

from __future__ import annotations

import re
import uuid
from typing import Any

from ebbmem_conformance.mock_state import (
    VALID_TIERS,
    IdempotencyRecord,
    Identity,
    MemoryItem,
    MockServerState,
    _fingerprint_remember,
    _is_vchar_1_256,
    _parse_finite_float,
    _parse_int,
    _reinforce_one,
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
