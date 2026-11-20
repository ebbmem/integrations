# EbbMem TypeScript Adapters (Planned)

This directory is the workspace layout for upcoming [EbbMem](https://ebbmem.ai) TypeScript core and framework adapters.

All future TypeScript adapters in this directory must pass the IC-1 conformance suite in `../../conformance/` and uphold core EbbMem integration invariants:

- **Read-only recall & explicit reinforcement**: Memories decay unless reinforced; adapters must treat `recall` as read-only and emit `reinforce` only for actually-used memories (`injected` / reinforce-all-shown OFF by default).
- **Header-only authentication**: Support `Authorization: Bearer <token>` or `X-API-Key` headers only, refusing credentials in URLs or logs.
- **Fail-open resilience & idempotent retries**: Degrade gracefully on timeouts/5xx during recall and retry only inherently idempotent requests or keyed `remember` calls.
- **Untrusted recall framing**: Delimit recalled memory text as reference data, never executable instructions.
