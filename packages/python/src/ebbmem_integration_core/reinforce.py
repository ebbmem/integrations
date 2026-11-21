"""Reinforcement policy, delta validation, cited-ID extraction, and ExposureBookkeeper."""

from __future__ import annotations

import math
import re
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ebbmem_integration_core.errors import ValidationError

ReinforceMode = Literal["tool", "cited", "feedback", "injected"]

DEFAULT_REINFORCE_DELTA: float = 0.1
DEFAULT_FRAMEWORK_REINFORCE_MODE: ReinforceMode = "cited"
DEFAULT_MCP_REINFORCE_MODE: ReinforceMode = "tool"
DEFAULT_EXPOSURE_CAPACITY: int = 512

VALID_REINFORCE_MODES: frozenset[str] = frozenset({"tool", "cited", "feedback", "injected"})


def validate_delta(delta: Any, *, clamp: bool = False) -> float:
    """Validate (or optionally clamp) reinforcement delta within [-1.0, 1.0].

    Rejects booleans, non-numeric values, NaN, and +/-Inf.
    """
    if isinstance(delta, bool):
        raise ValidationError("invalid_delta: boolean values are not permitted")
    try:
        val = float(delta)
    except (TypeError, ValueError) as exc:
        raise ValidationError("invalid_delta: delta must be a finite number") from exc
    if not math.isfinite(val):
        raise ValidationError("invalid_delta: delta must be finite")
    if clamp:
        return max(-1.0, min(1.0, val))
    if val < -1.0 or val > 1.0:
        raise ValidationError("delta_out_of_bounds: delta must be within [-1.0, 1.0]")
    return val


def extract_cited_ids(text: str, candidate_ids: Sequence[str]) -> list[str]:
    """Extract candidate memory IDs explicitly referenced in response text."""
    if not text or not candidate_ids:
        return []
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for mem_id in candidate_ids:
        if not mem_id or mem_id in seen:
            continue
        pattern = re.compile(rf"(?<![A-Za-z0-9_-]){re.escape(mem_id)}(?![A-Za-z0-9_-])")
        match = pattern.search(text)
        if match is not None:
            seen.add(mem_id)
            found.append((match.start(), mem_id))
    found.sort(key=lambda pair: pair[0])
    return [mem_id for _, mem_id in found]


@dataclass(frozen=True)
class ReinforcePolicy:
    """Enforces explicit post-use reinforcement with injected OFF by default."""

    mode: ReinforceMode = DEFAULT_FRAMEWORK_REINFORCE_MODE
    injected: bool = False

    def __post_init__(self) -> None:
        if self.mode not in VALID_REINFORCE_MODES:
            raise ValidationError(f"Unsupported reinforcement mode: {self.mode!r}")

    def select_ids_to_reinforce(
        self,
        recalled_ids: Sequence[str],
        used_ids: Sequence[str],
        *,
        injected: bool | None = None,
    ) -> list[str]:
        """Select deduplicated IDs to reinforce.

        When injected is False (default), returns only used_ids that were recalled,
        preserving used_ids order. When injected is True, returns deduplicated
        used_ids + recalled_ids.
        """
        use_injected = (self.injected or self.mode == "injected") if injected is None else injected
        if use_injected:
            seen: set[str] = set()
            ordered: list[str] = []
            for item_id in [*used_ids, *recalled_ids]:
                if item_id and item_id not in seen:
                    seen.add(item_id)
                    ordered.append(item_id)
            return ordered

        recalled_set = {item_id for item_id in recalled_ids if item_id}
        seen_used: set[str] = set()
        result: list[str] = []
        for item_id in used_ids:
            if item_id and item_id in recalled_set and item_id not in seen_used:
                seen_used.add(item_id)
                result.append(item_id)
        return result


@dataclass(frozen=True)
class ExposureRecord:
    """Recorded recall exposure for one turn/namespace."""

    namespace: str
    turn_id: str | None
    recall_id: str | None
    recalled_ids: tuple[str, ...]
    ranks: dict[str, int]
    scores: dict[str, float]
    timestamp_ms: int


class ExposureBookkeeper:
    """Bounded per-namespace/per-turn recall exposure tracker."""

    def __init__(self, *, max_entries: int = DEFAULT_EXPOSURE_CAPACITY) -> None:
        if max_entries < 1:
            raise ValidationError("max_entries must be >= 1")
        self.max_entries = max_entries
        self._records: OrderedDict[int, ExposureRecord] = OrderedDict()
        self._seq: int = 0
        self._by_turn: dict[tuple[str, str], int] = {}
        self._by_recall_id: dict[tuple[str, str], int] = {}
        self._latest_by_ns: dict[str, int] = {}

    def _evict_key(self, seq_id: int) -> None:
        rec = self._records.pop(seq_id, None)
        if rec is None:
            return
        if rec.turn_id is not None and self._by_turn.get((rec.namespace, rec.turn_id)) == seq_id:
            self._by_turn.pop((rec.namespace, rec.turn_id), None)
        if (
            rec.recall_id is not None
            and self._by_recall_id.get((rec.namespace, rec.recall_id)) == seq_id
        ):
            self._by_recall_id.pop((rec.namespace, rec.recall_id), None)
        if self._latest_by_ns.get(rec.namespace) == seq_id:
            self._latest_by_ns.pop(rec.namespace, None)

    def record_recall(
        self,
        namespace: str,
        recall_response: Mapping[str, Any],
        *,
        turn_id: str | None = None,
        timestamp_ms: int | None = None,
    ) -> ExposureRecord:
        """Record a non-degraded recall response for later post-use reinforcement."""
        raw_results = recall_response.get("results") or []
        recalled_ids: list[str] = []
        ranks: dict[str, int] = {}
        scores: dict[str, float] = {}
        for rank, item in enumerate(raw_results, start=1):
            if not isinstance(item, Mapping):
                continue
            mem_id = item.get("memory_id") or item.get("id")
            if not isinstance(mem_id, str) or not mem_id:
                continue
            if mem_id not in ranks:
                recalled_ids.append(mem_id)
                ranks[mem_id] = rank
                raw_score = item.get("score")
                if isinstance(raw_score, int | float) and not isinstance(raw_score, bool):
                    if math.isfinite(float(raw_score)):
                        scores[mem_id] = float(raw_score)

        raw_recall_id = recall_response.get("recall_id")
        recall_id = (
            raw_recall_id.strip()
            if isinstance(raw_recall_id, str) and raw_recall_id.strip()
            else None
        )
        ts_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)

        record = ExposureRecord(
            namespace=namespace,
            turn_id=turn_id,
            recall_id=recall_id,
            recalled_ids=tuple(recalled_ids),
            ranks=ranks,
            scores=scores,
            timestamp_ms=ts_ms,
        )

        self._seq += 1
        seq_id = self._seq
        self._records[seq_id] = record
        self._latest_by_ns[namespace] = seq_id
        if turn_id is not None:
            self._by_turn[(namespace, turn_id)] = seq_id
        if recall_id is not None:
            self._by_recall_id[(namespace, recall_id)] = seq_id

        while len(self._records) > self.max_entries:
            oldest_seq = next(iter(self._records))
            self._evict_key(oldest_seq)

        return record

    def get_latest(self, namespace: str) -> ExposureRecord | None:
        seq_id = self._latest_by_ns.get(namespace)
        if seq_id is None:
            return None
        rec = self._records.get(seq_id)
        if rec is not None:
            self._records.move_to_end(seq_id)
        return rec

    def get_for_turn(self, namespace: str, turn_id: str) -> ExposureRecord | None:
        seq_id = self._by_turn.get((namespace, turn_id))
        if seq_id is None:
            return None
        rec = self._records.get(seq_id)
        if rec is not None:
            self._records.move_to_end(seq_id)
        return rec

    def get_by_recall_id(
        self,
        recall_id: str,
        *,
        namespace: str | None = None,
    ) -> ExposureRecord | None:
        if namespace is not None:
            seq_id = self._by_recall_id.get((namespace, recall_id))
            if seq_id is None:
                return None
            rec = self._records.get(seq_id)
            if rec is not None:
                self._records.move_to_end(seq_id)
            return rec
        for seq_id, rec in reversed(self._records.items()):
            if rec.recall_id == recall_id:
                self._records.move_to_end(seq_id)
                return rec
        return None

    def consume_turn(self, namespace: str, turn_id: str) -> ExposureRecord | None:
        seq_id = self._by_turn.get((namespace, turn_id))
        if seq_id is None:
            return None
        rec = self._records.get(seq_id)
        self._evict_key(seq_id)
        return rec

    def clear(self, namespace: str | None = None) -> None:
        if namespace is None:
            self._records.clear()
            self._by_turn.clear()
            self._by_recall_id.clear()
            self._latest_by_ns.clear()
            return
        to_remove = [seq_id for seq_id, rec in self._records.items() if rec.namespace == namespace]
        for seq_id in to_remove:
            self._evict_key(seq_id)


@dataclass(frozen=True)
class ReinforcePlan:
    """Resolved reinforcement call parameters ready for transport dispatch."""

    memory_id: str
    used_ids: tuple[str, ...]
    recall_id: str | None
    delta: float
    namespace: str


@dataclass
class ReinforcementTracker:
    """Coordinates ReinforcePolicy, ExposureBookkeeper, and mode-specific ID selection."""

    policy: ReinforcePolicy = field(default_factory=ReinforcePolicy)
    bookkeeper: ExposureBookkeeper = field(default_factory=ExposureBookkeeper)

    def record_exposure(
        self,
        namespace: str,
        recall_response: Mapping[str, Any],
        *,
        turn_id: str | None = None,
        timestamp_ms: int | None = None,
    ) -> ExposureRecord:
        return self.bookkeeper.record_recall(
            namespace,
            recall_response,
            turn_id=turn_id,
            timestamp_ms=timestamp_ms,
        )

    def plan_reinforcement(
        self,
        namespace: str,
        *,
        used_ids: Sequence[str] = (),
        response_text: str | None = None,
        turn_id: str | None = None,
        recall_id: str | None = None,
        delta: float = DEFAULT_REINFORCE_DELTA,
        clamp_delta: bool = False,
        mode: ReinforceMode | None = None,
        injected: bool | None = None,
    ) -> ReinforcePlan | None:
        """Build a ReinforcePlan from explicit used_ids, cited IDs, or feedback.

        Returns None when no memory IDs qualify for reinforcement.
        """
        validated_delta = validate_delta(delta, clamp=clamp_delta)
        effective_mode = mode if mode is not None else self.policy.mode
        if effective_mode not in VALID_REINFORCE_MODES:
            raise ValidationError(f"Unsupported reinforcement mode: {effective_mode!r}")

        explicit_recall_id = (
            recall_id.strip() if isinstance(recall_id, str) and recall_id.strip() else None
        )
        exposure: ExposureRecord | None = None
        if explicit_recall_id is not None:
            exposure = self.bookkeeper.get_by_recall_id(explicit_recall_id, namespace=namespace)
        elif turn_id is not None:
            exposure = self.bookkeeper.get_for_turn(namespace, turn_id)
        else:
            exposure = self.bookkeeper.get_latest(namespace)

        recalled_ids: Sequence[str] = exposure.recalled_ids if exposure is not None else ()
        resolved_recall_id = (
            explicit_recall_id
            if explicit_recall_id is not None
            else (exposure.recall_id if exposure else None)
        )

        candidate_used: list[str] = list(used_ids)
        if not candidate_used and effective_mode == "cited" and response_text and recalled_ids:
            candidate_used = extract_cited_ids(response_text, recalled_ids)

        if effective_mode == "feedback" and candidate_used:
            # Explicit user feedback targets known IDs directly (forwarding recall_id if matched).
            seen_fb: set[str] = set()
            selected = []
            for uid in candidate_used:
                if uid and uid not in seen_fb:
                    seen_fb.add(uid)
                    selected.append(uid)
            if explicit_recall_id is None and turn_id is None:
                resolved_recall_id = None
        else:
            effective_injected = (
                injected
                if injected is not None
                else (effective_mode == "injected" or self.policy.injected)
            )
            selected = self.policy.select_ids_to_reinforce(
                recalled_ids,
                candidate_used,
                injected=effective_injected,
            )

        if not selected:
            return None

        return ReinforcePlan(
            memory_id=selected[0],
            used_ids=tuple(selected[1:]),
            recall_id=resolved_recall_id,
            delta=validated_delta,
            namespace=namespace,
        )
