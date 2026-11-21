# `ebbmem-integration-core` (TypeScript)

Shared IC-1 integration primitives, scoping, security redaction/framing, resilience, exposure bookkeeping, and reinforcement tracking for [EbbMem](https://ebbmem.ai) TypeScript/Node adapters.

## Features & IC-1 Invariants

- **Zero runtime dependencies**: Uses Node >= 20 built-in `fetch`, `AbortController`, `URL`, and `node:crypto`.
- **Scoping resolver (`ScopingResolver`)**: Resolves `{tenant}:u:{userId}` or explicit `appNamespace` validated against `^[A-Za-z0-9._:/-]{1,256}$`, detects `X-Ebb-Namespace` vs. body conflicts, prevents per-thread/per-tuple namespace explosion, maps `agentId` to soul scope `"agent"`, maps `sessionId`/`threadId` (and `session:<id>` tags) to `sessionId`/`threadKey`, normalizes tags/domain, and rejects reserved `ebb.*` metadata keys.
- **Security, redaction & untrusted framing**: Rejects embedded URL userinfo/query credentials (`CredentialInUrlError`) without echoing raw URLs, rejects dual `bearerToken` + `apiKey` (`CredentialConflictError`), sends credentials strictly via headers, defaults telemetry OFF (`TELEMETRY_ENABLED_DEFAULT = false`), automatically redacts secrets before `remember` and in logs/errors (`Bearer`, `ebb_*`, `sk-*`, `gh[pousr]_*`, `github_pat_*`, JWTs, PEM blocks, key-value/JSON assignments), and wraps recalled content in `<ebbmem_untrusted_memory_context>` delimiters with attribute and closing-tag escaping.
- **Resilience & idempotent-only retry**: Defaults to `timeoutMs = 30_000`, `maxRetries = 2`, and `failOpen = true` (degrading `recall` on timeout/connection/`429`/`5xx` to `{ query, count: 0, results: [], degraded: true }` with a redacted warning). Retries only connection/timeout errors, `429`, and `502`/`503`/`504` on idempotent operations (`GET`/`HEAD`, `recall`, `forget`, `status`, `health`) and keyed `remember` (`Idempotency-Key` header or `idempotency_key` body). Never retries unkeyed `remember` or `reinforce`. Provides `makeIdempotencyKey` (`ebb_<64-hex>`).
- **Read-only recall & explicit reinforcement**: `recall` is strictly read-only (`reinforce: false`). `ReinforcementTracker` + `ExposureBookkeeper` support `tool`, `cited`, and `feedback` modes with `injected: false` by default, validate `delta` in `[-1.0, 1.0]` (default `+0.1`), forward `recall_id` when present (falling back cleanly when absent), and map `delete` strictly to soft-tombstone `forget` (`purge` is explicit admin-only).

## Quickstart

```ts
import {
  EbbmemIntegrationCore,
  ScopingResolver,
  makeIdempotencyKey,
} from "ebbmem-integration-core";

const core = new EbbmemIntegrationCore({
  baseUrl: "https://api.ebbmem.ai",
  apiKey: "ebb_your_key",
  scope: new ScopingResolver({ tenant: "acme", userId: "alice" }),
});

const created = await core.remember("User prefers concise bullet summaries.", {
  tier: "semantic",
  sessionId: "sess-1",
  idempotencyKey: makeIdempotencyKey(["acme:u:alice", "concise-bullets"]),
});

const recalled = await core.recall("concise summaries", { turnId: "turn-1" });
const promptContext = core.frameMemories(recalled);

await core.reinforceFromExposure({
  usedIds: [String(created.id)],
  mode: "tool",
  turnId: "turn-1",
});
```

## Structural `@ebbmem/client` Adapter (`SdkTransportAdapter`)

`SdkTransportAdapter` wraps any `@ebbmem/client` v0.2.0-compatible `SdkClientLike` object without taking a hard runtime dependency.

Known gaps in `@ebbmem/client` v0.2.0 handled by `SdkTransportAdapter`:

- `v0.2.0` facade methods omit `tier`, `session_id`, `metadata`, `valence`, `arousal`, and `timestamp_ms` on `remember`, `profile`/`min_score`/`domain` on `recall`, and `used_ids`/`recall_id` on `reinforce` (where the `v0.2.0` facade also defaults `delta` to `1`). `SdkTransportAdapter` bridges these via `raw.memory` when present and enforces `delta = 0.1`.
- `v0.2.0` `RecallRequestModel` lacks `tier`, `tags`, and `session_id` filters, and `v0.2.0` lacks `getByKey`, `deleteByKey`, metadata filters on `list`/`recall`, and `listNamespaces`. Calling unsupported capabilities throws `UnsupportedCapabilityError` (use `FetchTransport` when full IC-1 REST support is required).

## Running Tests

```bash
npm install --no-package-lock
npm run typecheck
npm run build
npm test
```
