"""Unit and E2E tests for list_memories, episodic_remember, and episodic_tail (sync & async)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from ebbmem_integration_core import (
    ApiValidationError,
    AsyncEbbmemClientSdkAdapter,
    AsyncIntegrationClient,
    AuthError,
    EbbmemClientSdkAdapter,
    IdempotencyConflictError,
    IntegrationClient,
    NotFoundError,
    RetryPolicy,
    ScopeResolver,
    ServerError,
    UnsupportedCapabilityError,
    ValidationError,
    is_idempotent_request,
)

try:
    from ebbmem_conformance.mock_server import MockEbbmemServer
except ImportError:  # pragma: no cover
    _CONFORMANCE_SRC = Path(__file__).resolve().parents[3] / "conformance" / "src"
    if str(_CONFORMANCE_SRC) not in sys.path:
        sys.path.insert(0, str(_CONFORMANCE_SRC))
    from ebbmem_conformance.mock_server import MockEbbmemServer


class _FakeV020Memory:
    def remember(self, content: str, **kwargs: Any) -> dict[str, Any]:
        return {"id": "m-1", "content": content}

    def recall(self, query: str, **kwargs: Any) -> dict[str, Any]:
        return {"query": query, "count": 0, "results": []}

    def get(self, memory_id: str) -> dict[str, Any]:
        return {"id": memory_id}

    def forget(self, memory_id: str, purge: bool = False) -> dict[str, Any]:
        return {"status": "purged" if purge else "forgotten", "memory_id": memory_id}

    def reinforce(self, memory_id: str, delta: float = 0.1) -> dict[str, Any]:
        return {"status": "reinforced", "memory_id": memory_id, "delta": delta}

    def status(self) -> dict[str, Any]:
        return {"status": "ok"}


class _FakeV020SyncSdk:
    def __init__(self) -> None:
        self.memory = _FakeV020Memory()


class _FakeV030SyncMemory(_FakeV020Memory):
    def list_memories(self, **kwargs: Any) -> dict[str, Any]:
        return {"items": [{"id": "m-1"}], "rows": [{"id": "m-1"}], "total": 1, "total_count": 1}

    def episodic_remember(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "remembered", "sequence_id": 1, "thread_key": kwargs.get("thread_key")}

    def episodic_tail(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "thread_key": kwargs.get("thread_key"), "count": 0, "turns": []}


class _FakeV030SyncSdk:
    def __init__(self) -> None:
        self.memory = _FakeV030SyncMemory()


class _FakeV020AsyncSdk:
    def __init__(self) -> None:
        self.memory = _FakeV020Memory()


class _FakeV030AsyncMemory:
    async def list_memories(self, **kwargs: Any) -> dict[str, Any]:
        return {"items": [], "rows": [], "total": 0, "total_count": 0}

    async def episodic_remember(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "remembered", "sequence_id": 1}

    async def episodic_tail(self, **kwargs: Any) -> dict[str, Any]:
        return {"status": "ok", "count": 0, "turns": []}


class _FakeV030AsyncSdk:
    def __init__(self) -> None:
        self.memory = _FakeV030AsyncMemory()


def test_retry_rules_for_list_and_mcp_episodic() -> None:
    assert is_idempotent_request("GET", "/api/v1/memory") is True
    assert (
        is_idempotent_request(
            "POST",
            "/mcp/full",
            json_body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "memory_episodic_tail", "arguments": {"thread_key": "t1"}},
            },
        )
        is True
    )
    assert (
        is_idempotent_request(
            "POST",
            "/mcp/full",
            json_body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "memory_episodic_remember",
                    "arguments": {"thread_key": "t1", "content": "hi"},
                },
            },
        )
        is False
    )
    assert (
        is_idempotent_request(
            "POST",
            "/mcp/full",
            headers={"Idempotency-Key": "ep-key-1"},
            json_body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "memory_episodic_remember",
                    "arguments": {"thread_key": "t1", "content": "hi"},
                },
            },
        )
        is True
    )
    assert (
        is_idempotent_request(
            "POST",
            "/mcp/full",
            json_body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "memory_episodic_remember",
                    "arguments": {
                        "thread_key": "t1",
                        "content": "hi",
                        "idempotency_key": "ep-key-2",
                    },
                },
            },
        )
        is True
    )


def test_sync_client_list_and_episodic_e2e_against_mock() -> None:
    alice_scope = ScopeResolver(tenant="acme", user_id="alice")
    with MockEbbmemServer() as srv:
        client = IntegrationClient(
            srv.base_url,
            api_key="ebb_test_alice_key",
            scope=alice_scope,
            retry_policy=RetryPolicy(max_retries=2, backoff_base_s=0.001, backoff_cap_s=0.005),
        )
        assert client.transport.capabilities.list_memories is True
        assert client.transport.capabilities.episodic_remember is True
        assert client.transport.capabilities.episodic_tail is True

        # Remember two memories + list pagination + filters
        r1 = client.remember(
            "First note",
            tier="semantic",
            tags=["alpha"],
            importance=0.9,
            timestamp_ms=1000,
            metadata={"kind": "note"},
        )
        r2 = client.remember(
            "Second note",
            tier="episodic",
            tags=["beta"],
            importance=0.3,
            session_id="sess-1",
            timestamp_ms=2000,
        )

        page1 = client.list_memories(limit=1)
        assert page1["total"] == 2
        assert page1["has_more"] is True
        assert [row["memory_id"] for row in page1["items"]] == [r2["memory_id"]]

        page2 = client.list_memories(limit=1, cursor=page1["next_cursor"])
        assert page2["has_more"] is False
        assert [row["memory_id"] for row in page2["items"]] == [r1["memory_id"]]

        filtered = client.list_memories(
            tier="semantic",
            min_importance=0.8,
            metadata_filter={"kind": "note"},
        )
        assert [row["memory_id"] for row in filtered["items"]] == [r1["memory_id"]]

        # Cross-namespace isolation -> NotFoundError; conflict -> ApiValidationError
        with pytest.raises(NotFoundError):
            client.list_memories(app_namespace="other:u:bob")
        with pytest.raises(ApiValidationError):
            client.list_memories(headers={"X-Ebb-Namespace": "other:u:bob"})

        # fail_open=True on 503 for list_memories & episodic_tail, never on episodic_remember
        degraded_list = client.list_memories(headers={"X-Mock-Behavior": "status_503"})
        assert degraded_list["degraded"] is True
        assert degraded_list["items"] == []

        with pytest.raises(ServerError):
            client.list_memories(fail_open=False, headers={"X-Mock-Behavior": "status_503"})

        with pytest.raises(ApiValidationError):
            client.list_memories(limit=0)

        # Episodic remember with secret redaction & scoping via thread_id
        secret_msg = "Bearer ebb_super_secret_token_999"
        ep1 = client.episodic_remember(
            secret_msg,
            thread_id="thread-A",
            role="user",
            idempotency_key="ep-idem-1",
        )
        assert ep1["status"] == "remembered"
        assert ep1["sequence_id"] == 1
        assert ep1["thread_key"] == "thread-A"

        # Mismatch idempotency key raises IdempotencyConflictError
        with pytest.raises(IdempotencyConflictError):
            client.episodic_remember(
                "different content",
                thread_key="thread-A",
                idempotency_key="ep-idem-1",
            )

        # Unkeyed episodic_remember does NOT retry on 503 (only 1 request logged)
        before_count = len(srv.logged_requests)
        with pytest.raises(ServerError):
            client.episodic_remember(
                "no retry",
                thread_key="thread-A",
                headers={"X-Mock-Behavior": "status_503"},
            )
        after_unkeyed = len(srv.logged_requests)
        assert after_unkeyed - before_count == 1

        # Keyed episodic_remember DOES retry (1 + 2 = 3 attempts on 503)
        with pytest.raises(ServerError):
            client.episodic_remember(
                "with retry",
                thread_key="thread-A",
                idempotency_key="ep-retry-503",
                headers={"X-Mock-Behavior": "status_503"},
            )
        after_keyed = len(srv.logged_requests)
        assert after_keyed - after_unkeyed == 3

        # episodic_tail reads redacted turn chronologically & retries on 503
        tail = client.episodic_tail(thread_id="thread-A", limit=5)
        assert tail["count"] == 1
        assert tail["turns"][0]["content"] == "Bearer [REDACTED]"
        raw_mem = srv.get_memory_raw("acme:u:alice", ep1["memory_id"])
        assert raw_mem is not None
        assert raw_mem.storage_strength == 1.0

        degraded_tail = client.episodic_tail(
            thread_key="thread-A",
            headers={"X-Mock-Behavior": "status_503"},
        )
        assert degraded_tail["degraded"] is True
        assert degraded_tail["turns"] == []

        # Missing thread_key raises ValidationError
        with pytest.raises(ValidationError):
            client.episodic_remember("hello")
        with pytest.raises(ValidationError):
            client.episodic_tail()

        # Readonly key cannot episodic_remember (AuthError)
        ro_client = IntegrationClient(
            srv.base_url,
            api_key="ebb_test_readonly_key",
            scope=alice_scope,
        )
        with pytest.raises(AuthError):
            ro_client.episodic_remember("ro fail", thread_key="thread-A")
        ro_tail = ro_client.episodic_tail(thread_key="thread-A")
        assert ro_tail["count"] == 1

        # Zero telemetry headers & zero leaked secrets in log_sink
        joined_logs = "\n".join(client.log_sink)
        assert "ebb_test_alice_key" not in joined_logs
        assert "ebb_super_secret_token_999" not in joined_logs
        for req in srv.logged_requests:
            assert not any(h.lower().startswith("x-ebb-telemetry") for h in req["header_names"])


def test_async_client_list_and_episodic_e2e_against_mock() -> None:
    async def _run() -> None:
        alice_scope = ScopeResolver(tenant="acme", user_id="alice")
        with MockEbbmemServer() as srv:
            client = AsyncIntegrationClient(
                srv.base_url,
                api_key="ebb_test_alice_key",
                scope=alice_scope,
                retry_policy=RetryPolicy(max_retries=2, backoff_base_s=0.001, backoff_cap_s=0.005),
            )
            ep1 = await client.episodic_remember(
                "async turn 1",
                thread_key="async-t1",
                role="user",
            )
            ep2 = await client.episodic_remember(
                "async turn 2",
                thread_key="async-t1",
                role="assistant",
            )
            assert ep1["sequence_id"] == 1
            assert ep2["sequence_id"] == 2

            tail = await client.episodic_tail(thread_key="async-t1", limit=1)
            assert tail["count"] == 1
            assert tail["turns"][0]["content"] == "async turn 2"

            listed = await client.list_memories(tier="episodic", session_id="async-t1")
            assert listed["total"] == 2

            degraded = await client.list_memories(headers={"X-Mock-Behavior": "status_503"})
            assert degraded["degraded"] is True

            degraded_tail = await client.episodic_tail(
                thread_key="async-t1",
                headers={"X-Mock-Behavior": "status_503"},
            )
            assert degraded_tail["degraded"] is True

    asyncio.run(_run())


def test_sdk_adapters_capabilities_and_guards() -> None:
    sync_v020 = EbbmemClientSdkAdapter(_FakeV020SyncSdk())
    assert sync_v020.capabilities.list_memories is False
    assert sync_v020.capabilities.episodic_remember is False
    assert sync_v020.capabilities.episodic_tail is False

    with pytest.raises(UnsupportedCapabilityError):
        sync_v020.list_memories(namespace="acme:u:alice")
    with pytest.raises(UnsupportedCapabilityError):
        sync_v020.episodic_remember(thread_key="t1", content="hi", namespace="acme:u:alice")
    with pytest.raises(UnsupportedCapabilityError):
        sync_v020.episodic_tail(thread_key="t1", namespace="acme:u:alice")

    sync_v030 = EbbmemClientSdkAdapter(_FakeV030SyncSdk())
    assert sync_v030.capabilities.list_memories is True
    assert sync_v030.capabilities.episodic_remember is True
    assert sync_v030.capabilities.episodic_tail is True
    assert sync_v030.list_memories(namespace="acme:u:alice")["total"] == 1
    assert sync_v030.episodic_remember(thread_key="t1", content="hi")["sequence_id"] == 1
    assert sync_v030.episodic_tail(thread_key="t1")["status"] == "ok"

    async def _run_async() -> None:
        async_v020 = AsyncEbbmemClientSdkAdapter(_FakeV020AsyncSdk())
        assert async_v020.capabilities.list_memories is False
        with pytest.raises(UnsupportedCapabilityError):
            await async_v020.list_memories(namespace="acme:u:alice")
        with pytest.raises(UnsupportedCapabilityError):
            await async_v020.episodic_remember(thread_key="t1", content="hi")
        with pytest.raises(UnsupportedCapabilityError):
            await async_v020.episodic_tail(thread_key="t1")

        async_v030 = AsyncEbbmemClientSdkAdapter(_FakeV030AsyncSdk())
        assert async_v030.capabilities.list_memories is True
        assert (await async_v030.list_memories())["total"] == 0
        assert (await async_v030.episodic_remember(thread_key="t1", content="hi"))[
            "sequence_id"
        ] == 1
        assert (await async_v030.episodic_tail(thread_key="t1"))["status"] == "ok"

    asyncio.run(_run_async())
