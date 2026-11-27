---
name: ebbmem-integration-core-and-sdk-adapters
description: >-
  Empirical bug prevention and contract reference for editing packages/python/,
  packages/typescript/, conformance/src/ebbmem_conformance/mock_*.py, and
  optional released-SDK adapters (ebbmem-client / @ebbmem/client) in
  ebbmem/integrations. Use whenever modifying core transports, scoping,
  redaction, framing, retries, reinforcement bookkeeping, SDK adapters, or the
  IC-1 reference mock server.
---

# EbbMem Integration Core & SDK Adapter Playbook

Use this skill when modifying `ebbmem-integration-core` (Python or TypeScript), its `ebbmem-client` / `@ebbmem/client` adapters, or `MockEbbmemServer`. Every rule below addresses a concrete defect previously reproduced under adversarial testing.

## 1. Released-SDK Adapter Pitfalls (`ebbmem-client` & `@ebbmem/client` `0.2.0`)

1. **Preserve `this` binding on TypeScript `raw.memory.*` calls**:
   - Generated `@ebbmem/client` `raw.memory` is a `MemoryApi` class instance whose prototype methods call `this.rememberRaw(...)`, `this.recallRaw(...)`, etc.
   - **Never** detach methods into standalone variables (`const fn = raw.memory.remember; await fn(...)`), which sets `this === undefined` and crashes with `TypeError` (silently masked by `failOpen: true` on `recall`).
   - Always hold `const rawMem = this.sdk.raw?.memory` and call `rawMem.remember!(...)` (or `.call(rawMem, ...)`). Maintain a `class`-based `raw.memory` double in `packages/typescript/test/unit.test.ts` asserting `this instanceof ClassBasedRawMemoryApi` across every method.
2. **Omit `None` fields before instantiating Python OpenAPI Pydantic models**:
   - Generated `ReinforceRequestModel`, `RememberRequestModel`, and `RecallRequestModel` in `ebbmem-client` `0.2.0` emit `"used_ids": null` (and other nullable fields as `null`) in `to_dict()` whenever a field was passed explicitly as `None` (entering `self.model_fields_set`), causing HTTP `422 invalid_used_ids`.
   - Always build a dict and filter `clean_payload = {k: v for k, v in payload.items() if v is not None}` before calling `req_cls(**clean_payload)`. Wrap `ValueError` into `ValidationError(redact_secrets(str(exc)))`.
3. **Handle `0.2.0` Python generated model constraints explicitly**:
   - `RememberRequestModel`: wrap `session_id` with `SessionId(session_id)` (not raw `str`); reject `bool` for `valence`/`arousal` (`isinstance(v, bool)` before `int` checks) with `ValidationError`; raise `UnsupportedCapabilityError` when float `valence`/`arousal` is passed to a `StrictInt`-only model (`valence` in `[-128, 127]`, `arousal` in `[0, 255]`).
   - `RecallRequestModel`: enforces `len(query) >= 1` and `limit <= 50`, and lacks `tier`, `tags`, and `session_id` filters.
   - Sync `MemoryApi` response decoders (`PurgeResultResponse.from_dict`, `MemoryRow`) fail on `MockEbbmemServer` schema differences (`content`/float `valence`/`arousal`, 15-field purge payload). Prefer `*_without_preload_content` when available (e.g. `purge_memory_without_preload_content`), explicitly raising `ApiException.from_response` on `>= 400` responses so `403`/`404` translate cleanly.
4. **Dispatch `purge` to `purge_memory` / `purgeMemory` first**:
   - Calling `forget_memory(..., purge=True)` / `forgetMemory({ memoryId, purge: true })` hits `DELETE /api/v1/memory/{id}?purge=true`, which `MockEbbmemServer` treats as soft-tombstone `forgotten` and bypasses admin checks.
   - Always invoke `raw_mem.purge_memory` / `rawMem.purgeMemory` (`DELETE /api/v1/memory/{id}/purge`) when present, falling back to `forget(..., purge=True)` only if absent.
   - Normalize `status = "purged"` and `irreversible = True` when `"status"` is missing and `found is not False` — **never** fabricate `"purged"` when `found is False`.

## 2. Error Classification & Exception Translation

1. **Exclude `EBB-100-023` from `IdempotencyConflictError`**:
   - On HTTP `400`/`422`, `EBB-100-021`, `EBB-100-022`, `EBB-100-024`, `EBB-100-025`, and `EBB-100-029` map to `IdempotencyConflictError`.
   - **`EBB-100-023` is reserved `ebb.*` metadata key rejection (`ApiValidationError`)** — check `code.startswith("EBB-100-02") and code != "EBB-100-023"` in both Python and TypeScript HTTP and SDK error mappers.
2. **Deep SDK exception unwrapping & body redaction**:
   - Walk `__cause__` / `__context__` with a bounded hop limit (e.g., 16) and cycle set.
   - Parse `.body` across `Mapping`, `bytes`, JSON `str`, and raw `str`, checking both top-level `code` and nested `error.code`.
   - Normalize non-string `.code` values to `None` (parity with HTTP error parser).
   - Always redact `EbbmemApiError.body` via `redact_payload` / `redactPayload` in constructors.
3. **Catch low-level Python stdlib HTTP exceptions**:
   - Catch `(OSError, http.client.HTTPException)` in addition to `HTTPError`, `TimeoutError`, and `URLError` (including while calling `exc.read()` inside an `HTTPError` handler) and wrap as `TransportError(redact_secrets(...))`.

## 3. Wire Protocol, Pagination, Headers & Cross-Language Parity

1. **Never default `limit=50` when `page_size` / `pageSize` is supported**:
   - Defaulting `limit=50` in `list_memories` / `listMemories` transmits `limit=50` on the wire and silently overrides caller-supplied `page_size=2`. Keep `limit: int | None = None` / `limit?: number`, omit `limit` from query parameters when unset, and let the server default to `50`.
2. **Case-insensitive HTTP header merging & idempotency-key detection**:
   - Always inspect and merge headers case-insensitively (`_merge_headers_ci`, `getHeaderCaseInsensitive`).
   - Preserve conflicting caller `x-ebb-namespace` headers so the server returns `400 namespace_conflict` instead of duplicating or overwriting them.
   - Extract case-insensitive `idempotency-key` from `headers` when the explicit `idempotency_key` parameter is omitted so keyed `remember` / `episodic_remember` retries activate consistently across HTTP and SDK transports.
3. **Canonical UTF-8 `make_idempotency_key` / `makeIdempotencyKey` parity**:
   - Python `json.dumps` must use `ensure_ascii=False, sort_keys=True, separators=(",", ":")` to stay byte-identical with TypeScript canonical JSON SHA-256 (`ebb_<64-hex>`) across Unicode BMP and non-BMP strings.
4. **Python `bool` vs `int` equality trap**:
   - In Python `True == 1` and `False == 0`. In `mock_memory_ops.py` `metadata_filter`, always compare `(type(actual) is bool) == (type(expected) is bool) and actual == expected`. Reject `isinstance(v, bool)` before `isinstance(v, int)` in numeric validators (`page_size`, `limit`, `valence`, `arousal`).
5. **No unexpected mock routes**:
   - Keep `POST /api/v1/memory` unmapped (`404 {"detail": "Not Found"}`); memory creation lives at `POST /api/v1/memory/remember` and listing at `GET /api/v1/memory`.
6. **Backward-compatible TypeScript `EbbmemTransport` extensions**:
   - When adding methods (`listMemories`, `episodicRemember`, `episodicTail`) to `EbbmemTransport` or `SdkCapabilities`, keep new interface properties optional (`?`) so downstream packages and test doubles (`packages/ai-sdk`, `plugins/openclaw`) remain source-compatible.

## 4. Scoping, Bookkeeping, Redaction & Framing Invariants

1. **Scoping & metadata**:
   - `check_namespace_conflict` must reject empty or whitespace-only non-`None` strings with `NamespaceError`.
   - `validate_metadata` must block `key.strip().lower().startswith("ebb.")` (catches `"EBB.internal"` and `" ebb.internal"`).
2. **Exposure bookkeeping & reinforcement**:
   - Key `_by_recall_id` by `(namespace, recall_id)` to prevent cross-namespace collisions.
   - Update LRU order (`move_to_end`) on **every** read (`get_latest`, `get_for_turn`, `get_by_recall_id`).
   - In `plan_reinforcement`, if an explicit `recall_id` or `turn_id` is provided and misses, return `None` — **never** fall back to `get_latest(namespace)`.
3. **Redaction & untrusted framing**:
   - Include `AKIA[0-9A-Z]{16}` / `ASIA[0-9A-Z]{16}` in key prefix redaction and cover expanded token keys (`secret_key`, `api_secret`, `refresh_token`, `bearer_token`, `id_token`, `auth_token`, `access_token`).
   - Use negative lookahead `(?![A-Za-z0-9_-])` (not `\b`) at the end of JWT regexes so unsigned `alg=none` JWTs (`... .`) and hyphenated signatures (`... .---`) redact cleanly.
   - Keep quoted KV secret patterns single-line (`[^\r\n]`) so unclosed quotes redact to end-of-line without swallowing subsequent lines.
   - Guard `redact_payload` with cycle tracking and depth cap (`64` -> `"[CIRCULAR/MAX_DEPTH]"`) while releasing visited nodes on stack unwind so shared non-circular DAG objects are redacted on all branches.
   - Escape closing untrusted context tags with `(?:>|\Z)` and preserve falsy non-`None` content (`"" if raw_val is None else str(raw_val)`).

## 5. Verification Checklist

1. Keep every touched file `<= 1,000` lines (split via re-export facades if growing, e.g., `mock_{state,memory_ops,mcp,server}.py` or `transport_types.py` / `http_transport.py` / `sdk_adapter.py`).
2. Run Python checks: `ruff check conformance packages/python`, `ruff format --check conformance packages/python`, `pytest -v conformance/tests packages/python/tests`, `ebbmem-conformance --mode mock`.
3. Run TypeScript checks: `cd packages/typescript && npm run typecheck && npm run build && npm test`.
4. Verify cross-language `make_idempotency_key` parity and test both sync and async paths against `MockEbbmemServer`.
