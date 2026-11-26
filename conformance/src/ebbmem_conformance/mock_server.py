"""Fresh Apache-2.0 stateful reference mock server for IC-1 and MCP v2 conformance."""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ebbmem_conformance.mock_mcp import handle_mcp_request
from ebbmem_conformance.mock_memory_ops import (
    create_memory,
    execute_recall,
    execute_reinforce,
    get_status,
    serialize_memory,
)
from ebbmem_conformance.mock_state import (
    BUILTIN_IDENTITIES,
    LOCAL_ORIGINS,
    NAMESPACE_RE,
    SPACING_WINDOW_MS,
    SUPPORTED_MCP_VERSIONS,
    URL_CREDENTIAL_PARAMS,
    VALID_TIERS,
    IdempotencyRecord,
    Identity,
    MemoryItem,
    MockServerState,
    _load_golden_surface,
    _parse_int,
)

__all__ = [
    "BUILTIN_IDENTITIES",
    "IdempotencyRecord",
    "Identity",
    "LOCAL_ORIGINS",
    "MemoryItem",
    "MockEbbmemServer",
    "MockServerState",
    "NAMESPACE_RE",
    "SPACING_WINDOW_MS",
    "SUPPORTED_MCP_VERSIONS",
    "URL_CREDENTIAL_PARAMS",
    "VALID_TIERS",
]


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
            payload = execute_recall(
                state=self.state,
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
            self._send_json(200, get_status(self.state, ns), record_path=path)
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
                    serialize_memory(item) if item is not None and not item.tombstoned else None
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
            handle_mcp_request(self, path)
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
        status, payload, headers = create_memory(self.state, identity, ns, body, header_idem)
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
        payload = execute_recall(
            state=self.state,
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
        status, payload = execute_reinforce(self.state, identity, ns, body)
        self._send_json(status, payload, record_path=path)


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
