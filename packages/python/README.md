# `ebbmem-integration-core` (Python)

Shared IC-1 integration primitives, scoping, security redaction/framing, resilience, exposure bookkeeping, and reinforcement tracking for [EbbMem](https://ebbmem.ai) Python adapters.

## Features & IC-1 Invariants

- **Zero mandatory runtime dependencies**: Pure Python `>=3.11` standard library (`StdlibHttpTransport` and `AsyncStdlibHttpTransport`) with sync (`IntegrationClient`) and async (`AsyncIntegrationClient`) parity.
- **Scoping resolver (`ScopeResolver`)**: Resolves `{tenant}:u:{user_id}` or explicit `app_namespace` validated against `^[A-Za-z0-9._:/-]{1,256}$`, detects `X-Ebb-Namespace` vs. body conflicts, prevents per-thread/per-tuple namespace explosion, maps `agent_id` to soul scope `"agent"`, maps `session_id`/`thread_id` (and `session:<id>` tags) to `session_id`/`thread_key`, normalizes tags/domain, and rejects reserved `ebb.*` metadata keys.
- **Security, redaction & untrusted framing**: Rejects embedded URL userinfo/query credentials (`CredentialInUrlError`) without echoing raw URLs, rejects dual `bearer_token` + `api_key` (`CredentialConflictError`), sends credentials strictly via headers, defaults telemetry OFF (`telemetry_enabled=False`), automatically redacts secrets before `remember` and in logs/errors (`Bearer`, `ebb_*`, `sk-*`, `gh[pousr]_*`, `github_pat_*`, JWTs, PEM blocks, key-value/JSON assignments), and wraps recalled content in `<ebbmem_untrusted_memory_context>` delimiters with attribute and closing-tag escaping.
- **Resilience & idempotent-only retry**: Defaults to `timeout_s=30.0`, `max_retries=2`, and `fail_open=True` (degrading `recall` on timeout/connection/`429`/`5xx` to `{query, count: 0, results: [], degraded: True}` with a redacted warning). Retries only `ConnectionError`/timeout, `429`, and `502`/`503`/`504` on idempotent operations (`GET`/`HEAD`, `recall`, `forget`, `status`, `health`) and keyed `remember` (`Idempotency-Key` header or `idempotency_key` body). Never retries unkeyed `remember` or `reinforce`. Provides `make_idempotency_key` (`ebb_<64-hex>`).
- **Read-only recall & explicit reinforcement**: `recall` is strictly read-only (`reinforce=False`). `ReinforcementTracker` + `ExposureBookkeeper` support `tool`, `cited`, and `feedback` modes with `injected=False` by default, validate `delta` in `[-1.0, 1.0]` (default `+0.1`), forward `recall_id` when present (falling back cleanly when absent), and map `delete` strictly to soft-tombstone `forget` (`purge` is explicit admin-only).

## Quickstart

```python
from ebbmem_integration_core import IntegrationClient, ScopeResolver, make_idempotency_key

client = IntegrationClient(
    "https://api.ebbmem.ai",
    api_key="ebb_your_key",
    scope=ScopeResolver(tenant="acme", user_id="alice"),
)

created = client.remember(
    "User prefers concise bullet summaries.",
    tier="semantic",
    session_id="sess-1",
    idempotency_key=make_idempotency_key("acme:u:alice", "concise-bullets"),
)

recalled = client.recall("concise summaries", turn_id="turn-1")
prompt_context = client.frame_memories(recalled)

# Reinforce only what was actually used (injected=False by default)
client.reinforce_from_exposure(
    used_ids=[created["id"]],
    mode="tool",
    turn_id="turn-1",
)
```

## Optional Released-SDK Adapter (`ebbmem-client` `0.2.0`)

Install with the optional `sdk` extra (`pip install ebbmem-integration-core[sdk]`) and wrap an `EbbmemClient` or `AsyncEbbmemClient` via `EbbmemClientSdkAdapter` / `AsyncEbbmemClientSdkAdapter`.

Known gaps in `ebbmem-client` `0.2.0` handled by the adapter:

- `0.2.0` facade methods omit `tier`, `session_id`, `metadata`, `valence`, `arousal`, and `timestamp_ms` on `remember`, `profile`/`min_score`/`domain` on `recall`, and `used_ids`/`recall_id` on `reinforce` (where the `0.2.0` facade also defaults `delta=1.0`). The adapter bridges these via `raw.memory` generated models and enforces `delta=0.1`.
- `0.2.0` `RememberRequestModel` enforces `StrictInt` bounds on `valence` (`[-128, 127]`) and `arousal` (`[0, 255]`), so passing float `valence`/`arousal` via `EbbmemClientSdkAdapter.remember` raises `UnsupportedCapabilityError`; `RecallRequestModel` lacks `tier`, `tags`, and `session_id` filters, and `0.2.0` lacks exact-key `get_by_key`/`delete_by_key`, `metadata` filters on `list`/`recall`, and `list_namespaces`. Calling unsupported features raises `UnsupportedCapabilityError` (use `StdlibHttpTransport` / `AsyncStdlibHttpTransport` when full IC-1 REST support is required).

## Running Tests

```bash
pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest -v
```
