"""Tests for MockEbbmemServer GET /api/v1/memory and stateful MCP episodic tools."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ebbmem_conformance.mock_server import MockEbbmemServer


def _http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = dict(headers or {})
    if body is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url=url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw) if raw else {}


def _mcp_call(
    base_url: str,
    *,
    path: str = "/mcp/full",
    api_key: str = "ebb_test_alice_key",
    tool_name: str,
    arguments: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    headers = {
        "X-API-Key": api_key,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call",
        "Mcp-Name": tool_name,
        **(extra_headers or {}),
    }
    status, payload = _http_json(
        "POST",
        f"{base_url}{path}",
        headers=headers,
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
    )
    if status >= 400 or "error" in payload:
        return status, payload, {}
    text = payload["result"]["content"][0]["text"]
    return status, payload, json.loads(text)


def test_list_memories_filters_pagination_isolation_and_readonly() -> None:
    alice_headers = {"X-API-Key": "ebb_test_alice_key", "X-Ebb-Namespace": "acme:u:alice"}
    bob_headers = {"X-API-Key": "ebb_test_bob_key", "X-Ebb-Namespace": "other:u:bob"}
    ro_headers = {"X-API-Key": "ebb_test_readonly_key", "X-Ebb-Namespace": "acme:u:alice"}

    with MockEbbmemServer() as srv:
        m1_status, m1 = _http_json(
            "POST",
            f"{srv.base_url}/api/v1/memory/remember",
            headers=alice_headers,
            body={
                "content": "Alice likes espresso",
                "tier": "semantic",
                "tags": ["coffee", "pref"],
                "importance": 0.9,
                "valence": 0.5,
                "session_id": "s-1",
                "timestamp_ms": 1000,
                "metadata": {"source": "chat", "rank": 1},
            },
        )
        assert m1_status == 201

        m2_status, m2 = _http_json(
            "POST",
            f"{srv.base_url}/api/v1/memory/remember",
            headers=alice_headers,
            body={
                "content": "Alice booked flight",
                "tier": "episodic",
                "tags": ["travel"],
                "importance": 0.4,
                "valence": -0.2,
                "session_id": "s-2",
                "timestamp_ms": 2000,
                "metadata": {"source": "calendar", "rank": 2},
            },
        )
        assert m2_status == 201

        m3_status, m3 = _http_json(
            "POST",
            f"{srv.base_url}/api/v1/memory/remember",
            headers=alice_headers,
            body={
                "content": "Alice forgot umbrella",
                "tier": "episodic",
                "tags": ["weather", "travel"],
                "importance": 0.2,
                "valence": -0.8,
                "session_id": "s-2",
                "timestamp_ms": 3000,
            },
        )
        assert m3_status == 201

        # Tombstone m3
        del_status, _ = _http_json(
            "DELETE",
            f"{srv.base_url}/api/v1/memory/{m3['memory_id']}?namespace=acme:u:alice",
            headers=alice_headers,
        )
        assert del_status == 200

        # Bob creates memory in isolated namespace
        bob_status, _ = _http_json(
            "POST",
            f"{srv.base_url}/api/v1/memory/remember",
            headers=bob_headers,
            body={"content": "Bob secret espresso", "tier": "semantic", "tags": ["coffee"]},
        )
        assert bob_status == 201

        # Default tombstoned=false returns m2 then m1 (descending created_at_ms)
        st, page1 = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?limit=1",
            headers=ro_headers,
        )
        assert st == 200
        assert page1["total"] == 2
        assert page1["total_count"] == 2
        assert page1["has_more"] is True
        assert page1["next_cursor"] == "1"
        assert [row["memory_id"] for row in page1["items"]] == [m2["memory_id"]]
        assert page1["rows"] == page1["items"]

        # Cursor pagination to page 2
        st, page2 = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?limit=1&cursor={page1['next_cursor']}",
            headers=ro_headers,
        )
        assert st == 200
        assert page2["has_more"] is False
        assert page2["next_cursor"] is None
        assert [row["memory_id"] for row in page2["items"]] == [m1["memory_id"]]

        # page / page_size pagination
        st, paged = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?page=2&page_size=1",
            headers=alice_headers,
        )
        assert st == 200
        assert [row["memory_id"] for row in paged["items"]] == [m1["memory_id"]]

        # tombstoned=true returns m3 only
        st, tomb = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?tombstoned=true",
            headers=alice_headers,
        )
        assert st == 200
        assert [row["memory_id"] for row in tomb["items"]] == [m3["memory_id"]]
        assert tomb["items"][0]["tombstoned"] is True

        # Filters: tier, tags, search, session_id, min_importance, valence, metadata_filter
        st, f_tier = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?tier=episodic",
            headers=alice_headers,
        )
        assert st == 200 and [r["memory_id"] for r in f_tier["items"]] == [m2["memory_id"]]

        st, f_tags = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?tags=coffee,pref",
            headers=alice_headers,
        )
        assert st == 200 and [r["memory_id"] for r in f_tags["items"]] == [m1["memory_id"]]

        st, f_multi_tags = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?tags=coffee&tags=pref",
            headers=alice_headers,
        )
        assert st == 200 and [r["memory_id"] for r in f_multi_tags["items"]] == [m1["memory_id"]]

        st, f_search = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?search=ESPRESSO",
            headers=alice_headers,
        )
        assert st == 200 and [r["memory_id"] for r in f_search["items"]] == [m1["memory_id"]]

        st, f_sess = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?session_id=s-2",
            headers=alice_headers,
        )
        assert st == 200 and [r["memory_id"] for r in f_sess["items"]] == [m2["memory_id"]]

        st, f_imp = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?min_importance=0.8",
            headers=alice_headers,
        )
        assert st == 200 and [r["memory_id"] for r in f_imp["items"]] == [m1["memory_id"]]

        st, f_val = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?min_valence=-0.5&max_valence=0.0",
            headers=alice_headers,
        )
        assert st == 200 and [r["memory_id"] for r in f_val["items"]] == [m2["memory_id"]]

        mf = urllib.parse.quote(json.dumps({"source": "chat", "rank": 1}))
        st, f_mf = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?metadata_filter={mf}",
            headers=alice_headers,
        )
        assert st == 200 and [r["memory_id"] for r in f_mf["items"]] == [m1["memory_id"]]

        # Validation errors (422)
        for bad_qs in (
            "limit=0",
            "limit=101",
            "limit=abc",
            "page_size=0",
            "cursor=-1",
            "page=0",
            "tier=invalid",
            "tombstoned=maybe",
            "min_importance=nan",
            "metadata_filter=notjson",
            "metadata_filter=%5B1%5D",
        ):
            bad_st, _ = _http_json(
                "GET",
                f"{srv.base_url}/api/v1/memory?{bad_qs}",
                headers=alice_headers,
            )
            assert bad_st == 422, f"Expected 422 for {bad_qs}, got {bad_st}"

        # Namespace conflict (400) & cross-namespace uniform 404
        st_conflict, conflict_body = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?namespace=other:u:bob",
            headers=alice_headers,
        )
        assert st_conflict == 400
        assert conflict_body["detail"] == "namespace_conflict"

        st_cross, _ = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?namespace=other:u:bob",
            headers={"X-API-Key": "ebb_test_alice_key"},
        )
        assert st_cross == 404

        # Strictly read-only: strength & recall_count unchanged
        raw_m1 = srv.get_memory_raw("acme:u:alice", m1["memory_id"])
        assert raw_m1 is not None
        assert raw_m1.storage_strength == 1.0
        assert raw_m1.recall_count == 0
        assert raw_m1.reinforce_count == 0

        # Erased namespace -> uniform 404
        srv.erase_namespace("acme:u:alice")
        st_erased, _ = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory",
            headers=alice_headers,
        )
        assert st_erased == 404


def test_mcp_episodic_remember_and_tail_stateful_and_isolated() -> None:
    with MockEbbmemServer() as srv:
        # /mcp/core rejects episodic tools with -32601
        st_core, err_core, _ = _mcp_call(
            srv.base_url,
            path="/mcp/core",
            tool_name="memory_episodic_remember",
            arguments={"content": "hello"},
        )
        assert st_core == 404
        assert err_core["error"]["code"] == -32601

        # Readonly rejected on remember (-32003), allowed on tail
        st_ro, err_ro, _ = _mcp_call(
            srv.base_url,
            api_key="ebb_test_readonly_key",
            tool_name="memory_episodic_remember",
            arguments={"content": "hello", "thread_key": "t1"},
        )
        assert st_ro == 403
        assert err_ro["error"]["code"] == -32003

        # Remember 3 turns on thread t1 (with default role and explicit roles)
        st1, _, r1 = _mcp_call(
            srv.base_url,
            tool_name="memory_episodic_remember",
            arguments={"thread_key": "t1", "content": "msg 1", "tags": ["a"]},
        )
        assert st1 == 200
        assert r1["status"] == "remembered"
        assert r1["sequence_id"] == 1
        assert r1["message_count"] == 1
        assert r1["role"] == "user"
        assert r1["thread_key"] == "t1"
        assert r1["tier"] == "episodic"
        assert r1["storage_strength"] == 1.0
        assert r1["replayed"] is False

        srv.advance_clock_ms(100)
        st2, _, r2 = _mcp_call(
            srv.base_url,
            tool_name="memory_episodic_remember",
            arguments={"thread_key": "t1", "role": "assistant", "content": "msg 2"},
            extra_headers={"Idempotency-Key": "ep-key-2"},
        )
        assert st2 == 200
        assert r2["sequence_id"] == 2
        assert r2["message_count"] == 2
        assert r2["role"] == "assistant"
        assert r2["replayed"] is False

        # Replay with same idempotency key does NOT increment sequence_id
        st2_rep, _, r2_rep = _mcp_call(
            srv.base_url,
            tool_name="memory_episodic_remember",
            arguments={"thread_key": "t1", "role": "assistant", "content": "msg 2"},
            extra_headers={"Idempotency-Key": "ep-key-2"},
        )
        assert st2_rep == 200
        assert r2_rep["replayed"] is True
        assert r2_rep["sequence_id"] == 2
        assert r2_rep["memory_id"] == r2["memory_id"]

        # Mismatched payload with same idempotency key -> 422 EBB-100-024
        st2_bad, err_bad, _ = _mcp_call(
            srv.base_url,
            tool_name="memory_episodic_remember",
            arguments={"thread_key": "t1", "role": "assistant", "content": "altered"},
            extra_headers={"Idempotency-Key": "ep-key-2"},
        )
        assert st2_bad == 422
        assert "EBB-100-024" in err_bad["error"]["message"]

        srv.advance_clock_ms(100)
        st3, _, r3 = _mcp_call(
            srv.base_url,
            tool_name="memory_episodic_remember",
            arguments={"session_id": "t1", "role": "tool", "content": "msg 3"},
        )
        assert st3 == 200
        assert r3["sequence_id"] == 3
        assert r3["thread_key"] == "t1"
        assert r3["message_count"] == 3

        # Separate thread t2 starts at sequence_id 1
        st_t2, _, rt2 = _mcp_call(
            srv.base_url,
            tool_name="memory_episodic_remember",
            arguments={"thread_key": "t2", "content": "other thread"},
        )
        assert st_t2 == 200
        assert rt2["sequence_id"] == 1

        # Bob's namespace t1 also starts at sequence_id 1
        st_bob, _, rbob = _mcp_call(
            srv.base_url,
            api_key="ebb_test_bob_key",
            tool_name="memory_episodic_remember",
            arguments={"thread_key": "t1", "content": "bob msg"},
        )
        assert st_bob == 200
        assert rbob["sequence_id"] == 1

        # Tail with limit=2 via readonly key returns turns 2 & 3 in chronological order
        st_tail, _, tail = _mcp_call(
            srv.base_url,
            api_key="ebb_test_readonly_key",
            tool_name="memory_episodic_tail",
            arguments={"thread_key": "t1", "limit": 2},
        )
        assert st_tail == 200
        assert tail["status"] == "ok"
        assert tail["thread_key"] == "t1"
        assert tail["count"] == 2
        assert [t["sequence_id"] for t in tail["turns"]] == [2, 3]
        assert [t["content"] for t in tail["turns"]] == ["msg 2", "msg 3"]

        # Tail does not mutate storage_strength or recall_count
        for mem_id in (r1["memory_id"], r2["memory_id"], r3["memory_id"]):
            item = srv.get_memory_raw("acme:u:alice", mem_id)
            assert item is not None
            assert item.storage_strength == 1.0
            assert item.recall_count == 0

        # Invalid role / empty content / bad tail limit rejected
        st_role, _, _ = _mcp_call(
            srv.base_url,
            tool_name="memory_episodic_remember",
            arguments={"thread_key": "t1", "role": "wizard", "content": "x"},
        )
        assert st_role == 422

        st_limit, _, _ = _mcp_call(
            srv.base_url,
            tool_name="memory_episodic_tail",
            arguments={"thread_key": "t1", "limit": 0},
        )
        assert st_limit == 400

        # Also visible via GET /api/v1/memory?tier=episodic&session_id=t1
        st_list, list_body = _http_json(
            "GET",
            f"{srv.base_url}/api/v1/memory?tier=episodic&session_id=t1",
            headers={"X-API-Key": "ebb_test_alice_key"},
        )
        assert st_list == 200
        assert list_body["total"] == 3

        # erase_namespace clears episodic threads and returns 404
        srv.erase_namespace("acme:u:alice")
        assert srv.get_episodic_turns("acme:u:alice", "t1") == []
        st_erased, _, _ = _mcp_call(
            srv.base_url,
            tool_name="memory_episodic_tail",
            arguments={"thread_key": "t1"},
        )
        assert st_erased == 404
