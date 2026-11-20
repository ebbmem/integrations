"""Fresh Apache-2.0 stateful reference mock server for IC-1 and MCP v2 conformance."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

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


class _MockRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ebbmem-mock/0.1.0"
    _MAX_DRAIN_BYTES = 1_048_576

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Suppress default stderr HTTP logging to keep test output clean and credential-free.
        return

    @property
    def state(self) -> MockServerState:
        return self.server.mock_state  # type: ignore[attr-defined]

    def _drain_request_body(self) -> None:
        if getattr(self, "_body_consumed", False):
            return
        self._body_consumed = True
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return
        try:
            length = int(raw_len)
        except ValueError:
            return
        if 0 < length <= self._MAX_DRAIN_BYTES:
            try:
                self.rfile.read(length)
            except OSError:
                return

    def _parse_request_target(self) -> tuple[str, dict[str, list[str]]] | None:
        try:
            parsed = urlsplit(self.path)
        except ValueError:
            safe_path = self.path.split("?", 1)[0]
            self._send_json(400, {"detail": "invalid_url"}, record_path=safe_path)
            return None
        return parsed.path, parse_qs(parsed.query, keep_blank_values=True)

    def _record_request(self, method: str, path: str, status: int) -> None:
        safe_header_names = sorted(
            name
            for name in self.headers.keys()
            if name.lower() not in {"authorization", "x-api-key", "cookie"}
        )
        with self.state.lock:
            self.state.logged_requests.append(
                {
                    "method": method,
                    "path": path,
                    "status": status,
                    "header_names": safe_header_names,
                }
            )

    def _send_json(
        self,
        status: int,
        payload: Any,
        extra_headers: dict[str, str] | None = None,
        record_path: str = "",
    ) -> None:
        if not getattr(self, "_body_consumed", False):
            self._drain_request_body()
        body = json.dumps(payload).encode("utf-8")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Connection", "close")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        if record_path:
            self._record_request(self.command, record_path, status)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def _read_json_body(self) -> tuple[Any, str | None]:
        self._body_consumed = True
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return {}, None
        try:
            length = int(raw_len)
        except ValueError:
            return None, "invalid_content_length"
        if length < 0:
            return None, "invalid_content_length"
        if length == 0:
            return {}, None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "invalid_json"

    def _check_url_credentials(self, query_params: dict[str, list[str]], path: str) -> bool:
        if any(param.lower() in URL_CREDENTIAL_PARAMS for param in query_params):
            self._send_json(400, {"detail": "credentials_in_url"}, record_path=path)
            return False
        return True

    def _check_mock_behavior(self, path: str) -> bool:
        behavior = self.headers.get("X-Mock-Behavior", "").strip()
        if not behavior:
            return True
        if behavior.startswith("delay_ms="):
            try:
                delay_ms = int(behavior.split("=", 1)[1])
                time.sleep(max(0, delay_ms) / 1000.0)
            except ValueError:
                pass
            return True
        if behavior == "status_429":
            self._send_json(
                429,
                {"detail": "rate_limited"},
                extra_headers={"Retry-After": "0.02"},
                record_path=path,
            )
            return False
        if behavior == "status_503":
            self._send_json(503, {"detail": "service_unavailable"}, record_path=path)
            return False
        if behavior == "in_flight":
            self._send_json(
                409,
                {"code": "EBB-700-023", "detail": "idempotency_in_flight"},
                extra_headers={"Retry-After": "1"},
                record_path=path,
            )
            return False
        return True

    def _authenticate(self, path: str) -> Identity | None:
        auth_header = self.headers.get("Authorization")
        api_key_header = self.headers.get("X-API-Key")
        if auth_header and api_key_header:
            self._send_json(400, {"detail": "credentials_conflict"}, record_path=path)
            return None
        token: str | None = None
        if auth_header:
            parts = auth_header.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
                token = parts[1].strip()
        elif api_key_header:
            token = api_key_header.strip()

        if not token or token not in BUILTIN_IDENTITIES:
            self._send_json(401, {"detail": "unauthorized"}, record_path=path)
            return None

        identity = BUILTIN_IDENTITIES[token]
        tenant_header = self.headers.get("X-Tenant-ID")
        if tenant_header and identity.role != "admin" and tenant_header != identity.tenant:
            self._send_json(403, {"detail": "forbidden_tenant"}, record_path=path)
            return None
        return identity

    def _resolve_namespace(
        self,
        identity: Identity,
        body_namespace: Any,
        path: str,
    ) -> str | None:
        header_ns = self.headers.get("X-Ebb-Namespace")
        if body_namespace is not None and not isinstance(body_namespace, str):
            self._send_json(400, {"detail": "namespace_invalid"}, record_path=path)
            return None
        if header_ns is not None and body_namespace is not None and header_ns != body_namespace:
            self._send_json(400, {"detail": "namespace_conflict"}, record_path=path)
            return None

        ns = header_ns if header_ns is not None else body_namespace
        if ns is None:
            default_ns = identity.allowed_namespaces[0]
            ns = "acme:default" if default_ns == "*" else default_ns

        if not NAMESPACE_RE.match(ns):
            self._send_json(400, {"detail": "namespace_invalid"}, record_path=path)
            return None

        if "*" not in identity.allowed_namespaces and ns not in identity.allowed_namespaces:
            self._send_json(404, {"detail": "Not Found"}, record_path=path)
            return None

        with self.state.lock:
            is_erased = ns in self.state.erased_namespaces
        if is_erased:
            self._send_json(404, {"detail": "Not Found"}, record_path=path)
            return None

        return ns

    def do_GET(self) -> None:  # noqa: N802
        self._body_consumed = False
        target = self._parse_request_target()
        if target is None:
            return
        path, query = target

        if not self._check_url_credentials(query, path):
            return
        if not self._check_mock_behavior(path):
            return

        if path in {"/healthz", "/readyz"}:
            self._send_json(200, {"status": "ok"}, record_path=path)
            return

        identity = self._authenticate(path)
        if identity is None:
            return

        if path == "/api/v1/memory/recall":
            ns_param = query.get("namespace", [None])[0]
            ns = self._resolve_namespace(identity, ns_param, path)
            if ns is None:
                return
            q_text = query.get("query", [""])[0]
            limit_raw = query.get("limit", ["10"])[0]
            parsed_limit = _parse_int(limit_raw)
            if parsed_limit is None:
                self._send_json(422, {"detail": "invalid_limit"}, record_path=path)
                return
            limit = max(1, min(100, parsed_limit))
            tier_filter = query.get("tier", [None])[0]
            session_filter = query.get("session_id", [None])[0]
            tags_filter = query.get("tags", [])
            payload = self._execute_recall(
                namespace=ns,
                query_text=q_text,
                limit=limit,
                tier_filter=tier_filter,
                tags_filter=tags_filter or None,
                session_filter=session_filter,
                hex_recall_id=False,
            )
            self._send_json(200, payload, record_path=path)
            return

        if path == "/api/v1/memory/status":
            ns_param = query.get("namespace", [None])[0]
            ns = self._resolve_namespace(identity, ns_param, path)
            if ns is None:
                return
            self._send_json(200, self._get_status(ns), record_path=path)
            return

        if path.startswith("/api/v1/memory/"):
            mem_id = path[len("/api/v1/memory/") :]
            if not mem_id or "/" in mem_id:
                self._send_json(404, {"detail": "Not Found"}, record_path=path)
                return
            ns_param = query.get("namespace", [None])[0]
            ns = self._resolve_namespace(identity, ns_param, path)
            if ns is None:
                return
            with self.state.lock:
                item = self.state.memories.get(ns, {}).get(mem_id)
                payload = (
                    self._serialize_memory(item)
                    if item is not None and not item.tombstoned
                    else None
                )
            if payload is None:
                self._send_json(404, {"detail": "Not Found"}, record_path=path)
                return
            self._send_json(200, payload, record_path=path)
            return

        self._send_json(404, {"detail": "Not Found"}, record_path=path)

    def do_POST(self) -> None:  # noqa: N802
        self._body_consumed = False
        target = self._parse_request_target()
        if target is None:
            return
        path, query = target

        if not self._check_url_credentials(query, path):
            return
        if not self._check_mock_behavior(path):
            return

        if path in {"/mcp", "/mcp/core", "/mcp/full", "/mcp/admin"}:
            self._handle_mcp(path)
            return

        identity = self._authenticate(path)
        if identity is None:
            return

        body, err = self._read_json_body()
        if err or not isinstance(body, dict):
            self._send_json(400, {"detail": err or "invalid_json_object"}, record_path=path)
            return

        if path == "/api/v1/memory/remember":
            self._handle_rest_remember(identity, body, path)
            return
        if path == "/api/v1/memory/recall":
            self._handle_rest_recall(identity, body, path)
            return
        if path == "/api/v1/memory/reinforce":
            self._handle_rest_reinforce(identity, body, path)
            return

        self._send_json(404, {"detail": "Not Found"}, record_path=path)

    def do_DELETE(self) -> None:  # noqa: N802
        self._body_consumed = False
        target = self._parse_request_target()
        if target is None:
            return
        path, query = target

        if not self._check_url_credentials(query, path):
            return
        if not self._check_mock_behavior(path):
            return

        identity = self._authenticate(path)
        if identity is None:
            return

        if not path.startswith("/api/v1/memory/"):
            self._send_json(404, {"detail": "Not Found"}, record_path=path)
            return

        ns_param = query.get("namespace", [None])[0]
        ns = self._resolve_namespace(identity, ns_param, path)
        if ns is None:
            return

        if identity.role == "readonly":
            self._send_json(403, {"detail": "forbidden_key_scope"}, record_path=path)
            return

        rest = path[len("/api/v1/memory/") :]
        if rest.endswith("/purge"):
            mem_id = rest[: -len("/purge")]
            if not mem_id or "/" in mem_id:
                self._send_json(404, {"detail": "Not Found"}, record_path=path)
                return
            if identity.role != "admin":
                self._send_json(403, {"detail": "admin_required"}, record_path=path)
                return
            with self.state.lock:
                ns_store = self.state.memories.get(ns, {})
                item = ns_store.pop(mem_id, None)
                if item is None:
                    response = None
                else:
                    invalidated = 0
                    for (rec_ns, _key), rec in self.state.idempotency.items():
                        if rec_ns == ns and rec.memory_id == mem_id:
                            rec.purged = True
                            invalidated += 1
                    purged_at_ms = self.state.clock_ms
                    response = {
                        "memory_id": mem_id,
                        "namespace": ns,
                        "status": "purged",
                        "memories_removed": 1,
                        "edges_removed": 0,
                        "episodic_removed": 1 if item.tier == "episodic" else 0,
                        "embeddings_removed": 1,
                        "wal_tombstone_written": True,
                        "audit_entry_id": str(uuid.uuid4()),
                        "purged_at_ms": purged_at_ms,
                        "reason": "explicit_admin_purge",
                        "requested_by": identity.role,
                        "irreversible": True,
                        "idempotent_keys_invalidated": invalidated,
                        "tombstone_cleared": True,
                    }
            if response is None:
                self._send_json(404, {"detail": "Not Found"}, record_path=path)
                return
            self._send_json(200, response, record_path=path)
            return

        mem_id = rest
        if not mem_id or "/" in mem_id:
            self._send_json(404, {"detail": "Not Found"}, record_path=path)
            return
        with self.state.lock:
            item = self.state.memories.get(ns, {}).get(mem_id)
            if item is not None:
                item.tombstoned = True
                found = True
            else:
                found = False
        if not found:
            self._send_json(404, {"detail": "Not Found"}, record_path=path)
            return
        self._send_json(
            200,
            {"status": "forgotten", "memory_id": mem_id, "tombstoned": True},
            record_path=path,
        )

    def _serialize_memory(self, item: MemoryItem) -> dict[str, Any]:
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

    def _get_status(self, namespace: str) -> dict[str, Any]:
        with self.state.lock:
            items = list(self.state.memories.get(namespace, {}).values())
            active = sum(1 for item in items if not item.tombstoned)
            tombstoned = sum(1 for item in items if item.tombstoned)
            exposure = namespace in self.state.exposure_log_enabled_namespaces
        return {
            "status": "ok",
            "namespace": namespace,
            "active_count": active,
            "tombstoned_count": tombstoned,
            "exposure_logging_enabled": exposure,
        }

    def _create_memory(
        self,
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

        with self.state.lock:
            if effective_idem_key is not None:
                if effective_idem_key in self.state.in_flight_keys:
                    return (
                        409,
                        {"code": "EBB-700-023", "detail": "idempotency_in_flight"},
                        {"Retry-After": "1"},
                    )
                existing = self.state.idempotency.get((namespace, effective_idem_key))
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
            now_ms = explicit_ts if explicit_ts is not None else self.state.clock_ms
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
            self.state.memories.setdefault(namespace, {})[mem_id] = item
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
                self.state.idempotency[(namespace, effective_idem_key)] = IdempotencyRecord(
                    fingerprint=fingerprint,
                    memory_id=mem_id,
                    response=dict(response),
                )
        return 201, response, {}

    def _execute_recall(
        self,
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
        with self.state.lock:
            candidates = list(self.state.memories.get(namespace, {}).values())
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

                item_terms = {
                    term.lower() for term in re.findall(r"[A-Za-z0-9_]+", item.content)
                } | {t.lower() for t in item.tags}
                if query_terms:
                    overlap = len(query_terms & item_terms) / len(query_terms)
                else:
                    overlap = 0.5
                if (
                    query_terms
                    and overlap == 0.0
                    and query_text.lower() not in item.content.lower()
                ):
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
            if namespace in self.state.exposure_log_enabled_namespaces:
                raw_uuid = uuid.uuid4()
                recall_id = raw_uuid.hex if hex_recall_id else str(raw_uuid)
                self.state.exposure_log[recall_id] = {
                    "namespace": namespace,
                    "memory_ids": [r["memory_id"] for r in results],
                    "created_at_ms": self.state.clock_ms,
                }
                payload["recall_id"] = recall_id

        return payload

    def _execute_reinforce(
        self,
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

        with self.state.lock:
            ns_store = self.state.memories.get(namespace, {})
            resolved_targets: list[tuple[str, MemoryItem]] = []
            for tid in targets:
                target_item = ns_store.get(tid)
                if target_item is None or target_item.tombstoned:
                    return 404, {"detail": "Not Found"}
                resolved_targets.append((tid, target_item))

            primary = resolved_targets[0][1]
            outcomes: dict[str, str] = {}
            for tid, target_item in resolved_targets:
                outcome, _ = _reinforce_one(target_item, delta, self.state.clock_ms)
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

    def _handle_rest_remember(
        self,
        identity: Identity,
        body: dict[str, Any],
        path: str,
    ) -> None:
        ns = self._resolve_namespace(identity, body.get("namespace"), path)
        if ns is None:
            return
        header_idem = self.headers.get("Idempotency-Key")
        status, payload, headers = self._create_memory(identity, ns, body, header_idem)
        self._send_json(status, payload, extra_headers=headers, record_path=path)

    def _handle_rest_recall(
        self,
        identity: Identity,
        body: dict[str, Any],
        path: str,
    ) -> None:
        ns = self._resolve_namespace(identity, body.get("namespace"), path)
        if ns is None:
            return
        query_text = body.get("query", "")
        if not isinstance(query_text, str):
            self._send_json(422, {"detail": "invalid_query"}, record_path=path)
            return
        limit = _parse_int(body.get("limit", 10))
        if limit is None:
            self._send_json(422, {"detail": "invalid_limit"}, record_path=path)
            return
        tier_filter = body.get("tier")
        if tier_filter is not None and not isinstance(tier_filter, str):
            self._send_json(422, {"detail": "invalid_tier"}, record_path=path)
            return
        tags_filter = body.get("tags")
        if tags_filter is not None and (
            not isinstance(tags_filter, list) or not all(isinstance(t, str) for t in tags_filter)
        ):
            self._send_json(422, {"detail": "invalid_tags"}, record_path=path)
            return
        session_filter = body.get("session_id")
        if session_filter is not None and not isinstance(session_filter, str):
            self._send_json(422, {"detail": "invalid_session_id"}, record_path=path)
            return
        payload = self._execute_recall(
            namespace=ns,
            query_text=query_text,
            limit=max(1, min(100, limit)),
            tier_filter=tier_filter,
            tags_filter=tags_filter,
            session_filter=session_filter,
            hex_recall_id=False,
        )
        self._send_json(200, payload, record_path=path)

    def _handle_rest_reinforce(
        self,
        identity: Identity,
        body: dict[str, Any],
        path: str,
    ) -> None:
        ns = self._resolve_namespace(identity, body.get("namespace"), path)
        if ns is None:
            return
        status, payload = self._execute_reinforce(identity, ns, body)
        self._send_json(status, payload, record_path=path)

    def _handle_mcp(self, path: str) -> None:
        origin = self.headers.get("Origin")
        if origin is not None:
            try:
                parsed_origin = urlsplit(origin)
            except ValueError:
                self._send_json(403, {"detail": "untrusted_origin"}, record_path=path)
                return
            host = (parsed_origin.hostname or "").lower()
            if host not in LOCAL_ORIGINS:
                self._send_json(403, {"detail": "untrusted_origin"}, record_path=path)
                return

        identity = self._authenticate(path)
        if identity is None:
            return

        path_profile: str | None = None
        if path == "/mcp/core":
            path_profile = "core"
        elif path == "/mcp/full":
            path_profile = "full"
        elif path == "/mcp/admin":
            path_profile = "admin"

        header_profile = self.headers.get("X-Ebb-MCP-Profile")
        valid_profiles = {"core", "full", "admin"}
        if header_profile is not None and header_profile not in valid_profiles:
            self._send_json(400, {"detail": "invalid_profile"}, record_path=path)
            return
        if (
            path_profile is not None
            and header_profile is not None
            and path_profile != header_profile
        ):
            self._send_json(400, {"detail": "profile_conflict"}, record_path=path)
            return
        profile = path_profile or header_profile or "core"

        req, err = self._read_json_body()
        if err:
            self._send_json(
                400,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
                record_path=path,
            )
            return
        if isinstance(req, list):
            self._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid Request: batching unsupported"},
                },
                record_path=path,
            )
            return
        if not isinstance(req, dict):
            self._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"},
                },
                record_path=path,
            )
            return

        rpc_id = req.get("id")
        method = req.get("method")
        if "params" in req and not isinstance(req["params"], dict):
            self._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32602, "message": "Invalid params"},
                },
                record_path=path,
            )
            return
        params = req.get("params") if "params" in req else {}
        if not isinstance(method, str):
            self._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32600, "message": "Invalid Request"},
                },
                record_path=path,
            )
            return

        header_version = self.headers.get("MCP-Protocol-Version")
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        meta_version = meta.get("io.modelcontextprotocol/protocolVersion")
        init_version = params.get("protocolVersion") if method == "initialize" else None

        if header_version and meta_version and header_version != meta_version:
            self._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32020, "message": "MCP-Protocol-Version mismatch"},
                },
                record_path=path,
            )
            return

        version = header_version or meta_version or init_version or "2026-07-28"
        if version not in SUPPORTED_MCP_VERSIONS:
            self._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {
                        "code": -32022,
                        "message": f"Unsupported protocol version: {version}",
                    },
                },
                record_path=path,
            )
            return

        mcp_method_hdr = self.headers.get("Mcp-Method")
        if mcp_method_hdr is not None and mcp_method_hdr != method:
            self._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32020, "message": "Mcp-Method header mismatch"},
                },
                record_path=path,
            )
            return

        mcp_name_hdr = self.headers.get("Mcp-Name")
        if mcp_name_hdr is not None and method == "tools/call":
            if mcp_name_hdr != params.get("name"):
                self._send_json(
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": -32020, "message": "Mcp-Name header mismatch"},
                    },
                    record_path=path,
                )
                return

        resp_headers = {"MCP-Protocol-Version": version}
        golden = self.state.golden_surface

        if method == "ping":
            self._send_json(
                200,
                {"jsonrpc": "2.0", "id": rpc_id, "result": {}},
                extra_headers=resp_headers,
                record_path=path,
            )
            return

        if method == "initialize":
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "ebbmem-mock", "version": "0.1.0"},
                    },
                },
                extra_headers=resp_headers,
                record_path=path,
            )
            return

        if method == "notifications/initialized":
            self._send_json(
                200,
                {"jsonrpc": "2.0", "id": rpc_id, "result": {"acknowledged": True}},
                extra_headers=resp_headers,
                record_path=path,
            )
            return

        if method == "server/discover":
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "protocolVersion": version,
                        "profile": profile,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "ebbmem-mock", "version": "0.1.0"},
                        "ttlMs": 60_000,
                        "cacheScope": "private",
                    },
                },
                extra_headers=resp_headers,
                record_path=path,
            )
            return

        if method == "tools/list":
            tool_names = list(golden["profiles"][profile])
            admin_tools = set(golden["admin_only_tools"])
            if identity.role != "admin":
                tool_names = [name for name in tool_names if name not in admin_tools]
            tools = [golden["tools"][name] for name in tool_names]
            result: dict[str, Any] = {"tools": tools}
            if version == "2026-07-28":
                result["ttlMs"] = 60_000
                result["cacheScope"] = "private"
            self._send_json(
                200,
                {"jsonrpc": "2.0", "id": rpc_id, "result": result},
                extra_headers=resp_headers,
                record_path=path,
            )
            return

        if method == "tools/call":
            tool_name = params.get("name")
            if "arguments" in params and not isinstance(params["arguments"], dict):
                self._send_json(
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": -32602, "message": "Invalid params"},
                    },
                    extra_headers=resp_headers,
                    record_path=path,
                )
                return
            arguments = params.get("arguments") if "arguments" in params else {}
            if not isinstance(tool_name, str):
                self._send_json(
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": -32602, "message": "Invalid params"},
                    },
                    extra_headers=resp_headers,
                    record_path=path,
                )
                return

            admin_tools = set(golden["admin_only_tools"])
            if tool_name in admin_tools and identity.role != "admin":
                self._send_json(
                    403,
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": -32003, "message": "Admin role required"},
                    },
                    extra_headers=resp_headers,
                    record_path=path,
                )
                return

            allowed_for_profile = set(golden["profiles"][profile]) | set(
                golden["universal_aliases"]
            )
            if tool_name not in golden["tools"] or tool_name not in allowed_for_profile:
                self._send_json(
                    404,
                    {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": -32601, "message": f"Tool not found: {tool_name}"},
                    },
                    extra_headers=resp_headers,
                    record_path=path,
                )
                return

            ns = self._resolve_namespace(identity, arguments.get("namespace"), path)
            if ns is None:
                return

            status, call_result, rpc_err = self._execute_mcp_tool(
                identity=identity,
                namespace=ns,
                tool_name=tool_name,
                arguments=arguments,
                version=version,
            )
            if rpc_err is not None:
                self._send_json(
                    status,
                    {"jsonrpc": "2.0", "id": rpc_id, "error": rpc_err},
                    extra_headers=resp_headers,
                    record_path=path,
                )
                return
            self._send_json(
                200,
                {"jsonrpc": "2.0", "id": rpc_id, "result": call_result},
                extra_headers=resp_headers,
                record_path=path,
            )
            return

        self._send_json(
            404,
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            },
            extra_headers=resp_headers,
            record_path=path,
        )

    def _execute_mcp_tool(
        self,
        identity: Identity,
        namespace: str,
        tool_name: str,
        arguments: dict[str, Any],
        version: str,
    ) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
        structured: dict[str, Any]
        if tool_name in {"memory_remember", "memory_episodic_remember"}:
            args = dict(arguments)
            if tool_name == "memory_episodic_remember":
                args["tier"] = "episodic"
            status, payload, _ = self._create_memory(identity, namespace, args)
            if status >= 400:
                code = -32602 if status == 422 else -32000
                return status, None, {"code": code, "message": str(payload.get("detail"))}
            structured = payload
        elif tool_name in {"memory_recall", "memory_search", "memory_context_pack"}:
            raw_query = arguments.get("query", "")
            if not isinstance(raw_query, str):
                return 400, None, {"code": -32602, "message": "Invalid params: query"}
            limit = _parse_int(arguments.get("limit", 10))
            if limit is None:
                return 400, None, {"code": -32602, "message": "Invalid params: limit"}
            tier_filter = arguments.get("tier")
            if tier_filter is not None and not isinstance(tier_filter, str):
                return 400, None, {"code": -32602, "message": "Invalid params: tier"}
            tags_filter = arguments.get("tags")
            if tags_filter is not None and (
                not isinstance(tags_filter, list)
                or not all(isinstance(t, str) for t in tags_filter)
            ):
                return 400, None, {"code": -32602, "message": "Invalid params: tags"}
            session_filter = arguments.get("session_id")
            if session_filter is not None and not isinstance(session_filter, str):
                return 400, None, {"code": -32602, "message": "Invalid params: session_id"}
            structured = self._execute_recall(
                namespace=namespace,
                query_text=raw_query,
                limit=max(1, min(100, limit)),
                tier_filter=tier_filter,
                tags_filter=tags_filter,
                session_filter=session_filter,
                hex_recall_id=True,
            )
        elif tool_name == "memory_get":
            mem_id = str(arguments.get("memory_id", ""))
            with self.state.lock:
                item = self.state.memories.get(namespace, {}).get(mem_id)
                if item is None or item.tombstoned:
                    return 404, None, {"code": -32004, "message": "Memory not found"}
                structured = self._serialize_memory(item)
        elif tool_name == "memory_reinforce":
            status, payload = self._execute_reinforce(identity, namespace, arguments)
            if status >= 400:
                code = -32602 if status == 422 else -32000
                return status, None, {"code": code, "message": str(payload.get("detail"))}
            structured = payload
        elif tool_name == "memory_forget":
            if identity.role == "readonly":
                return 403, None, {"code": -32003, "message": "Readonly key cannot forget"}
            mem_id = str(arguments.get("memory_id", ""))
            with self.state.lock:
                item = self.state.memories.get(namespace, {}).get(mem_id)
                if item is None:
                    return 404, None, {"code": -32004, "message": "Memory not found"}
                item.tombstoned = True
            structured = {"status": "forgotten", "memory_id": mem_id, "tombstoned": True}
        elif tool_name == "memory_purge":
            if identity.role != "admin":
                return 403, None, {"code": -32003, "message": "Admin role required"}
            mem_id = str(arguments.get("memory_id", ""))
            with self.state.lock:
                item = self.state.memories.get(namespace, {}).pop(mem_id, None)
                if item is None:
                    return 404, None, {"code": -32004, "message": "Memory not found"}
                for (rec_ns, _k), rec in self.state.idempotency.items():
                    if rec_ns == namespace and rec.memory_id == mem_id:
                        rec.purged = True
            structured = {"status": "purged", "memory_id": mem_id, "irreversible": True}
        elif tool_name == "memory_status":
            structured = self._get_status(namespace)
        else:
            structured = {"status": "ok", "tool": tool_name, "namespace": namespace}

        result: dict[str, Any] = {
            "content": [{"type": "text", "text": json.dumps(structured)}],
        }
        if tool_name in self.state.golden_surface["output_schema_tools"]:
            result["structuredContent"] = structured
        if version == "2026-07-28":
            result["resultType"] = "complete"
        return 200, result, None


class MockEbbmemServer:
    """Context-managed stateful mock EbbMem REST + MCP v2 server."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._port = port
        self._state = MockServerState(golden_surface=_load_golden_surface())
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("MockEbbmemServer is not running")
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def logged_requests(self) -> list[dict[str, Any]]:
        with self._state.lock:
            return list(self._state.logged_requests)

    @property
    def erased_namespaces(self) -> set[str]:
        with self._state.lock:
            return set(self._state.erased_namespaces)

    @property
    def exposure_log_enabled_namespaces(self) -> set[str]:
        with self._state.lock:
            return set(self._state.exposure_log_enabled_namespaces)

    def advance_clock_ms(self, delta_ms: int) -> int:
        with self._state.lock:
            self._state.clock_ms += delta_ms
            return self._state.clock_ms

    def set_exposure_logging(self, namespace: str, enabled: bool) -> None:
        with self._state.lock:
            if enabled:
                self._state.exposure_log_enabled_namespaces.add(namespace)
            else:
                self._state.exposure_log_enabled_namespaces.discard(namespace)

    def erase_namespace(self, namespace: str) -> None:
        with self._state.lock:
            self._state.erased_namespaces.add(namespace)
            self._state.memories.pop(namespace, None)

    def set_in_flight(self, idempotency_key: str, active: bool) -> None:
        with self._state.lock:
            if active:
                self._state.in_flight_keys.add(idempotency_key)
            else:
                self._state.in_flight_keys.discard(idempotency_key)

    def get_memory_raw(self, namespace: str, memory_id: str) -> MemoryItem | None:
        with self._state.lock:
            return self._state.memories.get(namespace, {}).get(memory_id)

    def get_exposure_entry(self, recall_id: str) -> dict[str, Any] | None:
        with self._state.lock:
            entry = self._state.exposure_log.get(recall_id)
            return dict(entry) if entry is not None else None

    def start(self) -> MockEbbmemServer:
        if self._httpd is not None:
            return self
        httpd = ThreadingHTTPServer((self._host, self._port), _MockRequestHandler)
        httpd.mock_state = self._state  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def __enter__(self) -> MockEbbmemServer:
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()
