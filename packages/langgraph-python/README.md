# `ebbmem-langgraph`

LangGraph `BaseStore`, LangChain `create_agent` middleware, and LangMem-compatible memory tools backed by [EbbMem](https://ebbmem.ai) via [`ebbmem-integration-core`](../python/README.md).

## Why EbbMem in LangGraph?

Unlike static key-value or vector-only stores:

1. **Memories decay unless reinforced.** `get` and `search` (`recall`) are strictly **read-only** (`reinforce=False`). Call `store.reinforce(...)` / `store.reinforce_from_exposure(...)` or `reinforce_memory` only on memories that were actually used.
2. **Reinforce-all-shown (`injected`) is OFF by default.** Middleware and tools default to explicit usage (`cited` / `tool` mode) rather than reinforcing every retrieved result.
3. **Zero process-local item cache.** `EbbmemStore` stores all items, overwrites, tombstones, and LangGraph hierarchy tags in EbbMem — separate store instances share the same persisted state.
4. **Secret redaction, untrusted framing, and header-only auth.** Inherited from `ebbmem-integration-core` (`Authorization: Bearer` or `X-API-Key`, URL credentials rejected, telemetry OFF by default).

## Install from Source

```bash
pip install -e "./packages/python"
pip install -e "./packages/langgraph-python[langchain,langmem]"
```

For development and tests:

```bash
pip install -e "./conformance[dev]" -e "./packages/python[dev]" -e "./packages/langgraph-python[dev]"
```

## Quickstart: `EbbmemStore`

```python
from ebbmem_integration_core import ScopeResolver
from ebbmem_langgraph import EbbmemStore

store = EbbmemStore(
    "https://api.ebbmem.ai",
    api_key="ebb_your_key",
    scope=ScopeResolver(tenant="acme", user_id="alice"),
)

store.put(("memories", "prefs"), "theme", {"text": "User prefers dark mode", "kind": "ui"})
item = store.get(("memories", "prefs"), "theme")
hits = store.search(("memories",), query="dark mode", filter={"kind": "ui"})
namespaces = store.list_namespaces(prefix=("memories",))

# Soft-tombstone deletion (maps to EbbMem forget, never purge)
store.delete(("memories", "prefs"), "theme")
```

Async parity (`await store.aput(...)`, `aget`, `asearch`, `adelete`, `alist_namespaces`, `abatch`) is natively supported.

## Namespace Tuple Mapping (IC-1)

EbbMem does not create a separate top-level EbbMem namespace per LangGraph tuple, because unbounded per-tuple/per-thread namespaces break cross-session memory continuity and explode store partitions:

- **Top-level EbbMem namespace**: resolved by `ScopeResolver` to `{tenant}:u:{user_id}` (or explicit `app_namespace`). Optionally supply `user_id_from_namespace=lambda ns: ...` if one shared store maps `("users", "<id>", ...)` tuples to per-user namespaces `{tenant}:u:<id>`.
- **Tuple hierarchy & session extraction**: LangGraph tuple segments are forwarded as normalized IC-1 `tags` (any `session:<id>` segment is extracted into `session_id` and stripped from tags), accompanied by deterministic `lg:store`, `lg:ns:*`, `lg:pfx:*`, and `lg:item:*` tags and `metadata` fields (`lg_namespace`, `lg_key`, `lg_value`, `lg_created_at`, `lg_updated_at`).
- **Reserved keys**: `ScopeResolver` rejects top-level `ebb.*` metadata keys; user item values are stored inside `lg_value` with automatic secret redaction.

## LangChain `create_agent` Middleware & LangMem Compatibility

```python
from langchain.agents import create_agent
from ebbmem_langgraph import (
    EbbmemMemoryMiddleware,
    EbbmemStore,
    create_ebbmem_manage_memory_tool,
    create_ebbmem_reinforce_memory_tool,
    create_ebbmem_search_memory_tool,
)

store = EbbmemStore("https://api.ebbmem.ai", api_key="ebb_your_key", tenant="acme", user_id="alice")
middleware = EbbmemMemoryMiddleware(store=store, recall_limit=5, reinforce_mode="cited")
tools = [
    create_ebbmem_search_memory_tool(namespace=("memories", "alice"), store=store),
    create_ebbmem_manage_memory_tool(namespace=("memories", "alice"), store=store),
    create_ebbmem_reinforce_memory_tool(namespace=("memories", "alice"), store=store),
]

# Works with create_agent(..., middleware=[middleware], tools=tools, store=store)
```

`EbbmemStore` also implements the full `BaseStore` contract expected by `langmem.create_memory_store_manager(model, store=store)` and `langmem`'s built-in `create_manage_memory_tool` / `create_search_memory_tool`.

## Limits & Released `ebbmem-client` `0.2.0` Gaps

- **Default HTTP transport vs. `ebbmem-client` `0.2.0` SDK**: By default (`EbbmemStore(base_url, ...)`), `EbbmemStore` uses `ebbmem-integration-core`'s `StdlibHttpTransport` / `AsyncStdlibHttpTransport` so `tags`, `session_id`, `metadata`, and `recall` filters work end-to-end.
- **Released `0.2.0` SDK gaps**: When wrapping `ebbmem_client.EbbmemClient` / `AsyncEbbmemClient` (`0.2.0`) via `EbbmemStore(sdk_client=...)`, `ebbmem-client` `0.2.0` lacks `get_by_key`, `delete_by_key`, server-side `metadata` filters, `list_namespaces`, and `RecallRequestModel` `tags`/`tier`/`session_id` filters, and `RecallRequestModel` validates `len(query) >= 1` and `limit <= 50`. Additionally, sync `EbbmemClient.raw.memory.get_memory` (`0.2.0`) enforces `MemoryRow` (`text: str`, `StrictInt` `valence`/`arousal`), which differs from `content`/`float` responses on `ebbmem-conformance`'s mock server (`AsyncEbbmemClient` uses raw dict responses and works in both modes). Unless `strict_sdk_capabilities=True` is set, `EbbmemStore` falls back to scanning within `min(scan_limit, 50)` and hydrating/filtering client-side; `put`/`get`/`search`/`list_namespaces` scale up to `scan_limit` items per namespace until server-side exact-key and metadata-filter endpoints land.
- **TTL (`ttl`) & Checkpointing**: `EbbmemStore.supports_ttl` is `False` (passing `ttl=...` raises `NotImplementedError` per `BaseStore`). LangGraph checkpoint saving is out of scope for this package.
- **Delete semantics**: `store.delete` / `adelete` always maps to EbbMem soft-tombstone `forget`, never physical `purge`.

## Running Tests

```bash
ruff check packages/langgraph-python
ruff format --check packages/langgraph-python
pytest -v packages/langgraph-python/tests
```
