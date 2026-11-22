"""LangGraph custom-store test pattern comparing EbbmemStore against InMemoryStore."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest
from ebbmem_integration_core import RetryPolicy, ScopeResolver
from langgraph.store.base import (
    GetOp,
    InvalidNamespaceError,
    Item,
    ListNamespacesOp,
    MatchCondition,
    PutOp,
    SearchItem,
    SearchOp,
)
from langgraph.store.memory import InMemoryStore

from ebbmem_langgraph import EbbmemStore


def _make_store(server: Any, retry_policy: RetryPolicy) -> EbbmemStore:
    return EbbmemStore(
        server.base_url,
        api_key="ebb_test_alice_key",
        scope=ScopeResolver(tenant="acme", user_id="alice"),
        retry_policy=retry_policy,
    )


def _as_keyed_values(items: list[SearchItem]) -> dict[tuple[tuple[str, ...], str], dict]:
    return {(it.namespace, it.key): it.value for it in items}


def test_crud_hierarchy_and_overwrite_parity_with_inmemory(
    mock_server: Any,
    fast_retry_policy: RetryPolicy,
) -> None:
    ref = InMemoryStore()
    ebb = _make_store(mock_server, fast_retry_policy)

    ops = [
        (("users", "alice"), "prefs", {"theme": "dark", "limit": 10, "tags": ["a", "b"]}),
        (("users", "alice", "nested"), "prefs", {"theme": "light", "limit": 20}),
        (("users", "bob"), "prefs", {"theme": "system", "limit": 5}),
        (("docs", "guides"), "intro", {"text": "Python memory guide", "meta": {"v": 1}}),
    ]
    for ns, key, val in ops:
        ref.put(ns, key, val)
        ebb.put(ns, key, val)

    # Exact-key get parity: ("users", "alice") != ("users", "alice", "nested")
    for ns, key, expected in ops:
        ref_item = ref.get(ns, key)
        ebb_item = ebb.get(ns, key)
        assert isinstance(ref_item, Item)
        assert isinstance(ebb_item, Item)
        assert ebb_item.namespace == ref_item.namespace == ns
        assert ebb_item.key == ref_item.key == key
        assert ebb_item.value == ref_item.value == expected

    assert ref.get(("users", "alice"), "missing") is None
    assert ebb.get(("users", "alice"), "missing") is None

    # Overwrite updates value without duplicating search results
    updated = {"theme": "contrast", "limit": 42, "tags": ["a"]}
    ref.put(("users", "alice"), "prefs", updated)
    ebb.put(("users", "alice"), "prefs", updated)
    assert (
        ebb.get(("users", "alice"), "prefs").value
        == ref.get(  # type: ignore[union-attr]
            ("users", "alice"), "prefs"
        ).value
    )

    # Prefix search parity
    ref_users = ref.search(("users",))
    ebb_users = ebb.search(("users",))
    assert _as_keyed_values(ebb_users) == _as_keyed_values(ref_users)
    assert all(it.score is None for it in ebb_users)

    # Delete parity: deletes only exact (ns, key)
    ref.delete(("users", "alice"), "prefs")
    ebb.delete(("users", "alice"), "prefs")
    assert ebb.get(("users", "alice"), "prefs") is None
    assert ref.get(("users", "alice"), "prefs") is None
    assert ebb.get(("users", "alice", "nested"), "prefs") is not None
    assert _as_keyed_values(ebb.search(("users",))) == _as_keyed_values(ref.search(("users",)))


def test_filter_operators_pagination_and_semantic_query(
    mock_server: Any,
    fast_retry_policy: RetryPolicy,
) -> None:
    ref = InMemoryStore()
    ebb = _make_store(mock_server, fast_retry_policy)

    records = [
        (
            ("items",),
            "i1",
            {
                "text": "Python vector tutorial",
                "score": 4.5,
                "meta": {"env": "prod"},
                "arr": [1, 2],
            },
        ),
        (
            ("items",),
            "i2",
            {
                "text": "TypeScript SDK guide",
                "score": 3.0,
                "meta": {"env": "dev"},
                "arr": [3, 4],
            },
        ),
        (
            ("items",),
            "i3",
            {
                "text": "Python agent memory",
                "score": 5.0,
                "meta": {"env": "prod"},
                "arr": [1, 2],
            },
        ),
        (
            ("items",),
            "i4",
            {"text": "Rust core notes", "score": 2.0, "meta": {"env": "prod"}, "arr": [5]},
        ),
    ]
    for ns, key, val in records:
        ref.put(ns, key, val)
        ebb.put(ns, key, val)

    filters = [
        {"meta": {"env": "prod"}},
        {"score": {"$gte": 3.0}, "meta": {"env": "prod"}},
        {"score": {"$gt": 2.0, "$lt": 5.0}},
        {"score": {"$lte": 3.0}},
        {"score": {"$eq": 4.5}},
        {"score": {"$ne": 3.0}, "arr": [1, 2]},
    ]
    for flt in filters:
        assert _as_keyed_values(ebb.search(("items",), filter=flt)) == _as_keyed_values(
            ref.search(("items",), filter=flt)
        )

    # Pagination limit / offset / limit=0
    all_items = ebb.search(("items",), limit=10)
    assert len(all_items) == 4
    page1 = ebb.search(("items",), limit=2, offset=0)
    page2 = ebb.search(("items",), limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {(i.key) for i in page1}.isdisjoint({i.key for i in page2})
    assert ebb.search(("items",), limit=0) == []

    # Semantic query search returns ranked SearchItems with float scores
    q_hits = ebb.search(("items",), query="Python memory", filter={"meta": {"env": "prod"}})
    assert [h.key for h in q_hits] == ["i3", "i1"]
    assert all(isinstance(h.score, float) for h in q_hits)

    # index=False excludes item from semantic query while keeping get() & query=None search
    ebb.put(("items",), "secret_unindexed", {"text": "Python hidden note"}, index=False)
    assert ebb.get(("items",), "secret_unindexed") is not None
    assert "secret_unindexed" not in [
        h.key for h in ebb.search(("items",), query="Python hidden note")
    ]
    assert "secret_unindexed" in [h.key for h in ebb.search(("items",), query=None)]


def test_list_namespaces_parity_with_inmemory(
    mock_server: Any,
    fast_retry_policy: RetryPolicy,
) -> None:
    ref = InMemoryStore()
    ebb = _make_store(mock_server, fast_retry_policy)

    namespaces = [
        ("a", "b", "c"),
        ("a", "b", "d", "e"),
        ("a", "b", "d", "f"),
        ("a", "c", "f"),
        ("x", "b", "c"),
    ]
    for idx, ns in enumerate(namespaces):
        val = {"idx": idx}
        ref.put(ns, f"k{idx}", val)
        ebb.put(ns, f"k{idx}", val)

    assert ebb.list_namespaces() == ref.list_namespaces()
    assert ebb.list_namespaces(prefix=("a", "b")) == ref.list_namespaces(prefix=("a", "b"))
    assert ebb.list_namespaces(prefix=("a", "b"), max_depth=3) == ref.list_namespaces(
        prefix=("a", "b"), max_depth=3
    )
    assert ebb.list_namespaces(suffix=("f",)) == ref.list_namespaces(suffix=("f",))
    assert ebb.list_namespaces(prefix=("*", "b"), suffix=("c",)) == ref.list_namespaces(
        prefix=("*", "b"), suffix=("c",)
    )
    assert ebb.list_namespaces(limit=2, offset=1) == ref.list_namespaces(limit=2, offset=1)

    # Deleting the sole item removes empty namespace from persistent EbbmemStore
    # (InMemoryStore retains an empty dict key in self._data).
    ebb.delete(("x", "b", "c"), "k4")
    assert ("x", "b", "c") not in ebb.list_namespaces()
    assert ebb.list_namespaces(prefix=("a",)) == ref.list_namespaces(prefix=("a",))


def test_batch_ordering_validation_and_zero_process_local_dict(
    mock_server: Any,
    fast_retry_policy: RetryPolicy,
) -> None:
    ref = InMemoryStore()
    ebb = _make_store(mock_server, fast_retry_policy)

    ref.put(("batch",), "k1", {"v": 1})
    ebb.put(("batch",), "k1", {"v": 1})

    # GetOp runs before PutOp in the same batch; last PutOp wins per (ns, key)
    ops = [
        GetOp(("batch",), "k1"),
        PutOp(("batch",), "k1", {"v": 2}),
        PutOp(("batch",), "k1", {"v": 3}),
        PutOp(("batch",), "k2", MappingProxyType({"v": 99})),
        SearchOp(("batch",)),
        ListNamespacesOp(match_conditions=(MatchCondition(match_type="prefix", path=("batch",)),)),
    ]
    ref_res = ref.batch(ops)
    ebb_res = ebb.batch(ops)
    assert ref_res[0].value == ebb_res[0].value == {"v": 1}  # type: ignore[union-attr]
    assert ebb_res[1] is None and ebb_res[2] is None and ebb_res[3] is None
    assert len(ebb_res[4]) == 1  # type: ignore[arg-type]
    assert ebb_res[5] == ref_res[5] == [("batch",)]

    # Fresh EbbmemStore instance sees identical persisted state (no process-local item dict)
    fresh_ebb = _make_store(mock_server, fast_retry_policy)
    assert fresh_ebb.get(("batch",), "k1").value == {"v": 3}  # type: ignore[union-attr]
    assert fresh_ebb.get(("batch",), "k2").value == {"v": 99}  # type: ignore[union-attr]

    # Validation parity with BaseStore / InMemoryStore
    for bad_ns in [(), ("langgraph", "sub"), ("bad.dot",), ("",), ("null\x00byte",)]:
        with pytest.raises(InvalidNamespaceError):
            ebb.put(bad_ns, "k", {"a": 1})
        with pytest.raises(InvalidNamespaceError):
            ebb.batch([PutOp(bad_ns, "k", {"a": 1})])

    with pytest.raises(NotImplementedError):
        ebb.put(("batch",), "ttl_k", {"a": 1}, ttl=5.0)

    with pytest.raises(ValueError, match="Unknown operation type"):
        ebb.batch(["not-an-op"])  # type: ignore[list-item]


@pytest.mark.asyncio
async def test_async_abatch_aput_aget_asearch_adelete_alist_namespaces_parity(
    mock_server: Any,
    fast_retry_policy: RetryPolicy,
) -> None:
    ref = InMemoryStore()
    ebb = _make_store(mock_server, fast_retry_policy)

    await ref.aput(("async", "ns1"), "k1", {"text": "Alpha async fact", "n": 1})
    await ebb.aput(("async", "ns1"), "k1", {"text": "Alpha async fact", "n": 1})
    await ref.aput(("async", "ns2"), "k2", {"text": "Beta async fact", "n": 2})
    await ebb.aput(("async", "ns2"), "k2", {"text": "Beta async fact", "n": 2})

    g_ref = await ref.aget(("async", "ns1"), "k1")
    g_ebb = await ebb.aget(("async", "ns1"), "k1")
    assert g_ebb is not None and g_ref is not None
    assert g_ebb.value == g_ref.value

    s_ref = await ref.asearch(("async",), filter={"n": {"$gte": 1}})
    s_ebb = await ebb.asearch(("async",), filter={"n": {"$gte": 1}})
    assert _as_keyed_values(s_ebb) == _as_keyed_values(s_ref)

    q_ebb = await ebb.asearch(("async",), query="Beta fact")
    assert len(q_ebb) == 2 and q_ebb[0].key == "k2"
    assert q_ebb[0].score is not None and q_ebb[1].score is not None
    assert q_ebb[0].score > q_ebb[1].score

    ns_ref = await ref.alist_namespaces(
        prefix=("async",),
        suffix=("ns2",),
    )
    ns_ebb = await ebb.alist_namespaces(
        prefix=("async",),
        suffix=("ns2",),
    )
    assert ns_ebb == ns_ref == [("async", "ns2")]

    await ebb.abatch(
        [
            PutOp(("async", "ns1"), "k1", None),
            ListNamespacesOp(match_conditions=(MatchCondition("prefix", ("async",)),)),
        ]
    )
    assert await ebb.aget(("async", "ns1"), "k1") is None
    assert await ebb.alist_namespaces(prefix=("async",)) == [("async", "ns2")]
