# EbbMem Integrations — Contributor Agent Rules

This file defines repository-wide contributor guardrails for coding agents working on `ebbmem/integrations` (Apache-2.0). For the end-user agent instruction snippet distributed to EbbMem MCP users, see `instructions/AGENTS.md` and `instructions/CORE.md`.

## Repository Layout

| Path | Role |
| --- | --- |
| `conformance/` | Python IC-1 conformance suite (`ebbmem-conformance`), 21 YAML scenarios, `golden/mcp_surface_v2.json`, stateful `MockEbbmemServer`, `scripts/compat_manifest.json`, `scripts/generate_compat_table.py` |
| `conformance/ts/` | TypeScript IC-1 conformance runner (`ebbmem-conformance-ts`), `MockServerBridge`, deterministic badge evaluator (`src/badges.ts`) |
| `instructions/` | Canonical end-user agent discipline (`CORE.md`) and host wrappers (`SKILL.md`, `AGENTS.md`, Cursor, OpenClaw, Claude Code) |
| `packages/python/` | `ebbmem-integration-core` (Python >=3.11, stdlib HTTP + optional `ebbmem-client` adapter) |
| `packages/typescript/` | `ebbmem-integration-core` (Node >=20, native `fetch` + structural `@ebbmem/client` adapter) |
| `packages/langgraph-python/` | `ebbmem-langgraph` (`BaseStore`, LangChain `create_agent` middleware, LangMem tools) |
| `packages/openai-agents-python/` | `ebbmem-openai-agents` (`Session`, function tools, MCP server helpers) |
| `packages/ai-sdk/` | `@ebbmem/ai-sdk` (Vercel AI SDK v7+ tools and `wrapLanguageModel` middleware) |
| `plugins/{claude-code,cursor,openclaw,open-webui}/` | Host plugin bundles and Open WebUI Filter Function |
| `configs/copilot/`, `registry/`, `docs/` | VS Code / Copilot MCP snippets, Official MCP Registry `server.json`, host/recipe guides |

## Mandatory IC-1 Cognitive & Security Invariants

Every core module, adapter, middleware, plugin, and mock endpoint must preserve these invariants:

1. **Read-only recall & natural decay**: `recall`, `list_memories`, `episodic_tail`, `memory_search`, and `memory_get` must never mutate `storage_strength` or reset spacing state (`reinforce=False`).
2. **Explicit post-use reinforcement (`injected` OFF by default)**: `injected` / reinforce-all-shown defaults to `False` / `false`. Default `reinforce_delta` is `0.1` within `[-1.0, 1.0]`. Forward `recall_id` when available, never silently fall back to `get_latest(namespace)` when an explicit `turn_id` or `recall_id` misses, and clear active turn exposure at turn start (before early returns) and on zero-hit recall.
3. **Soft `forget` vs. admin `purge`**: Adapter `delete` operations always map to soft-tombstone `forget`. Physical `purge` is explicit admin-only and targets `DELETE /api/v1/memory/{id}/purge` (`purge_memory` / `purgeMemory`) whenever available.
4. **Header-only auth & local URL validation**: Send credentials strictly via `Authorization: Bearer <token>` or `X-API-Key` (never both). Reject URL userinfo (`@`), query credentials (`?token=`, `?api_key=`, `?access_token=`, `?key`), base URL `?query`/`#fragment`, and ASCII control characters (`ord < 32` or `127`) locally before any network call and without echoing raw URLs or secrets in exceptions or logs.
5. **Redact before truncating & single-line safety**: `redact_before_remember` defaults to `True`. Always run secret redaction **before** applying character-length truncation so boundary-straddling PEM blocks or JWTs never leak raw suffixes. Quoted key-value secret patterns must stay single-line (`[^\r\n]`) so an unclosed quote never consumes subsequent non-secret lines.
6. **Untrusted recall framing**: Wrap recalled text in `<ebbmem_untrusted_memory_context>` delimiters, HTML-escape element attributes, neutralize closing tags including unclosed EOF closing tags (`(?:>|\Z)`), and preserve falsy non-`None` content (`0`, `False`).
7. **Zero telemetry by default**: `telemetry_enabled` / `telemetryEnabled` defaults to `False` / `false`; never emit telemetry headers or outbound tracking requests by default.
8. **Fail-open & retry boundaries**: `fail_open` defaults to `True` **only** for read-only calls (`recall`, `list_memories`, `episodic_tail`, `health`) on transient failures (timeout, connection error, `429`, `5xx`), never for `remember`/`episodic_remember` or `4xx` auth/validation errors. Retry only idempotent operations and keyed `remember` (`idempotency_key` / `Idempotency-Key`); **never** retry unkeyed `remember` or `reinforce`.

## Public Repository Hygiene & Release Gates

- **Automated compliance scan**: `conformance/tests/test_public_compliance.py` scans **every** file in the repository (including tests, fixtures, docs, and `.agents/`) for blocked strings (private repo URLs, internal design doc or hostname markers, local filesystem paths, unverified compliance claims, and unmeasured benchmarks). Never include any blocked pattern—even in test fixtures or negative prose (for example, use `"private"` instead of blocked classification words). Always run `pytest -v conformance/tests/test_public_compliance.py` before committing.
- **No lockfiles, build outputs, or binaries**: Every TypeScript package sets `package-lock=false` in `.npmrc`; always use `npm install --no-package-lock`. Never commit `package-lock.json`, `node_modules/`, `dist/`, `build/`, `.venv*`, or downloaded CLI binaries (`mcp-publisher`).
- **No live paid APIs or unapproved publishing**: Tests must run deterministically offline using mocked models and local `MockEbbmemServer`. Clearly mark any unverified hosted endpoint or live snippet as untested live. Never run `mcp-publisher login`/`publish`, `npm publish`, `twine upload`, or marketplace submissions without explicit maintainer approval.
- **CI workflows**: Do not modify `.github/workflows/*` unless CI changes are explicitly requested.

## File Size, Matrix Sync & Instruction Pack Rules

- **1,000-line file cap at every commit**: Keep every added or modified source and test file `<= 1,000` lines at **every commit** in `origin/main..HEAD` (not only at `HEAD`). Split large modules cleanly behind backward-compatible re-export facades.
- **Compatibility matrix**: Never hand-edit between `<!-- BEGIN_COMPAT_TABLE -->` and `<!-- END_COMPAT_TABLE -->` in `docs/integrations/README.md`. Update `conformance/scripts/compat_manifest.json`, keep `conformance/ts/src/badges.ts` and `conformance/scripts/generate_compat_table.py` badge rules identical, and verify `python conformance/scripts/generate_compat_table.py --check` exits `0`.
- **Instruction pack parity**: Keep `instructions/` wrappers and bundled plugin skills/rules (`plugins/openclaw/skills/ebbmem-memory/SKILL.md`, `plugins/claude-code/skills/ebbmem-memory/SKILL.md`, `plugins/cursor/rules/ebbmem.mdc`) synchronized and verified with `pytest -v conformance/tests/test_instructions_pack.py`.
- **Commit hygiene**: Use Conventional Commits with DCO sign-off (`git commit -s`).

## Specialized Repo Skills

Load the relevant skill in `.agents/skills/` before editing specialized surfaces:

- `ebbmem-integration-core-and-sdk-adapters` — editing `packages/python/`, `packages/typescript/`, `conformance/src/ebbmem_conformance/mock_*.py`, or SDK adapters (`ebbmem-client` / `@ebbmem/client`).
- `ebbmem-adapter-and-plugin-patterns` — building or fixing framework adapters (`langgraph-python`, `openai-agents-python`, `ai-sdk`), host plugins (`openclaw`, `open-webui`, `claude-code`, `cursor`), Copilot configs, or `registry/server.json`.
- `ebbmem-conformance-and-test-harness` — running multi-package Python/TS tests, `MockServerBridge`, `conformance/ts`, deliberate-break probes, compatibility table checks, or pre-PR gates.
