"""Shared state, identities, constants, and pure helpers for MockEbbmemServer."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NAMESPACE_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")
URL_CREDENTIAL_PARAMS = frozenset({"token", "api_key", "access_token", "key"})
SUPPORTED_MCP_VERSIONS = (
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
SPACING_WINDOW_MS = 1_800_000
VALID_TIERS = frozenset({"working", "episodic", "semantic", "procedural"})
LOCAL_ORIGINS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


@dataclass(frozen=True)
class Identity:
    """Built-in mock API key identity."""

    key: str
    role: str
    tenant: str
    allowed_namespaces: tuple[str, ...]


BUILTIN_IDENTITIES: dict[str, Identity] = {
    "ebb_test_admin_key": Identity(
        key="ebb_test_admin_key",
        role="admin",
        tenant="acme",
        allowed_namespaces=("*",),
    ),
    "ebb_test_alice_key": Identity(
        key="ebb_test_alice_key",
        role="agent",
        tenant="acme",
        allowed_namespaces=("acme:u:alice",),
    ),
    "ebb_test_bob_key": Identity(
        key="ebb_test_bob_key",
        role="agent",
        tenant="other",
        allowed_namespaces=("other:u:bob",),
    ),
    "ebb_test_readonly_key": Identity(
        key="ebb_test_readonly_key",
        role="readonly",
        tenant="acme",
        allowed_namespaces=("acme:u:alice",),
    ),
}


@dataclass
class IdempotencyRecord:
    """Stored idempotency record for remember requests."""

    fingerprint: str
    memory_id: str
    response: dict[str, Any]
    purged: bool = False


@dataclass
class MemoryItem:
    """In-memory cognitive memory record."""

    memory_id: str
    namespace: str
    content: str
    tier: str
    tags: list[str]
    importance: float
    valence: float
    arousal: float
    session_id: str | None
    metadata: dict[str, Any]
    storage_strength: float
    recall_count: int
    reinforce_count: int
    created_at_ms: int
    accessed_at_ms: int
    last_reinforced_ms: int | None = None
    tombstoned: bool = False


@dataclass
class MockServerState:
    """Thread-safe mutable state for MockEbbmemServer."""

    golden_surface: dict[str, Any]
    lock: threading.RLock = field(default_factory=threading.RLock)
    clock_ms: int = 1_700_000_000_000
    memories: dict[str, dict[str, MemoryItem]] = field(default_factory=dict)
    idempotency: dict[tuple[str, str], IdempotencyRecord] = field(default_factory=dict)
    in_flight_keys: set[str] = field(default_factory=set)
    erased_namespaces: set[str] = field(default_factory=set)
    exposure_log_enabled_namespaces: set[str] = field(default_factory=set)
    exposure_log: dict[str, dict[str, Any]] = field(default_factory=dict)
    logged_requests: list[dict[str, Any]] = field(default_factory=list)


def _load_golden_surface() -> dict[str, Any]:
    golden_path = Path(__file__).resolve().parents[2] / "golden" / "mcp_surface_v2.json"
    return json.loads(golden_path.read_text(encoding="utf-8"))


def _is_vchar_1_256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and all(0x21 <= ord(ch) <= 0x7E for ch in value)
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _parse_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fingerprint_remember(
    namespace: str,
    content: str,
    tier: str,
    tags: list[str],
    importance: float,
    valence: float,
    arousal: float,
    session_id: str | None,
    metadata: dict[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "namespace": namespace,
            "content": content,
            "tier": tier,
            "tags": sorted(tags),
            "importance": importance,
            "valence": valence,
            "arousal": arousal,
            "session_id": session_id,
            "metadata": metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reinforce_one(item: MemoryItem, delta: float, clock_ms: int) -> tuple[str, float]:
    """Apply IC-1 tier bounds and spacing-window rules to a single memory."""
    if item.tier == "procedural":
        effective = 0.1 if delta >= 0 else -0.05
        item.storage_strength = round(_clamp(item.storage_strength + effective, 1.0, 5.0), 6)
        item.reinforce_count += 1
        item.last_reinforced_ms = clock_ms
        return "counted", item.storage_strength

    if item.last_reinforced_ms is not None and (clock_ms - item.last_reinforced_ms) < (
        SPACING_WINDOW_MS
    ):
        return "spaced_out", item.storage_strength

    if item.tier == "working":
        # Working tier ignores delta and stays pinned at 1.0
        item.storage_strength = 1.0
    elif item.tier == "episodic":
        item.storage_strength = round(_clamp(item.storage_strength + delta, 1.0, 5.0), 6)
    else:
        # semantic
        item.storage_strength = round(_clamp(item.storage_strength + delta, 1.0, 10.0), 6)

    item.reinforce_count += 1
    item.last_reinforced_ms = clock_ms
    return "counted", item.storage_strength
