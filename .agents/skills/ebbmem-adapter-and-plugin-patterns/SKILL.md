---
name: ebbmem-adapter-and-plugin-patterns
description: >-
  Framework-specific and host-specific implementation patterns, security
  boundaries, and schema validation checks for ebbmem/integrations adapters and
  plugins: LangGraph/LangChain/LangMem, Vercel AI SDK v7, OpenAI Agents SDK,
  OpenClaw, Open WebUI, Claude Code, Cursor, GitHub Copilot, and Official MCP
  Registry. Use when building, modifying, or reviewing any adapter under
  packages/, host plugin under plugins/, configs/copilot/, or registry/.
---

# EbbMem Adapter & Host Plugin Patterns

Use this skill when implementing, modifying, or auditing any framework adapter or host integration in `ebbmem/integrations`.

## 1. LangGraph, LangChain & LangMem (`packages/langgraph-python/`)

- **Prevent namespace explosion**: Map the top-level EbbMem namespace via `ScopeResolver` (`{tenant}:u:{user_id}` or `app_namespace`), and store LangGraph tuple hierarchy as normalized tags and metadata (`lg:store`, `lg:ns:*`, `lg:pfx:*`, `lg:item:*`, `lg_namespace`, `lg_key`, `lg_value`).
- **Watch `ScopeResolver` precedence**: A non-`None` `app_namespace` overrides `user_id`. When using `user_id_from_namespace` on a shared multi-user `EbbmemStore`, initialize `ScopeResolver(tenant=tenant)` without setting a fixed `app_namespace`.
- **Zero process-local item cache**: Never keep an in-memory item dictionary in `EbbmemStore`; separate store instances must observe identical persisted state. Set `supports_ttl = False` (raise `NotImplementedError` when `ttl` is passed per `BaseStore`).
- **Testing pitfalls**:
  - Compare behavior directly against LangGraph's `InMemoryStore` across sync and async `batch`/`abatch`, `put`/`aput`, `get`/`aget`, `search`/`asearch`, `delete`/`adelete`, and `list_namespaces`/`alist_namespaces`.
  - Pass `config=cfg` explicitly when invoking LangMem/LangGraph tools outside a workflow entrypoint (`get_config()` fails without an active runnable config).
  - Never write `from tests.conftest import ...` in test files (breaks when `pytest` runs from repo root vs. package root); use pytest fixtures or package-local helpers.

## 2. Vercel AI SDK (`packages/ai-sdk/`)

- **Runtime & v7 API contracts**: Target Node `>=22.0.0` with ESM (`"type": "module"`). In AI SDK v7 (`ai@^7`), direct `tool.execute(...)` calls in tests require `context` inside `ToolExecutionOptions` (`{ toolCallId, messages, context }`).
- **Middleware (`wrapLanguageModel`) concurrency & streaming safety**:
  - Support `LanguageModelV4` / `LanguageModelV3` / `LanguageModelV2` and both `doGenerate` and `doStream`.
  - Isolate concurrent turns using a `WeakMap` rather than instance-wide mutable turn state.
  - Skip premature capture and reinforcement on intermediate `tool-calls` finish reasons or stream errors.
- **Zero paid API calls**: Verify with `MockLanguageModelV4` / `MockLanguageModelV3`, in-memory `MockEbbmemTransport`, and live loopback `MockEbbmemServer` via `FetchTransport`.

## 3. OpenAI Agents SDK (`packages/openai-agents-python/`)

- **`Session` protocol**: Implement `agents.memory.Session` (`get_items`, `add_items`, `pop_item`, `clear_session`, `SessionSettings`, cancellation-safe mutations) backed by `McpEpisodicBackend` (`memory_episodic_remember` / `memory_episodic_tail`) or `RestEpisodicBackend` (using hex-encoded envelopes that survive secret redaction cleanly).
- **Tools, hooks & MCP helpers**: Correlate `ebbmem_recall` -> `ebbmem_reinforce` / `EbbmemRunHooks` via `recall_id` with `injected=False` default; enforce header-only credential validation in `ebbmem_mcp_server` (`MCPServerStreamableHttp`) and `ebbmem_mcp_stdio_server` (`MCPServerStdio`).
- **Offline tests**: Subclass `agents.models.interface.Model` for multi-turn `Runner.run` tests with zero OpenAI API calls.

## 4. OpenClaw Memory-Slot Plugin (`plugins/openclaw/`)

- **Fail-closed `authorizeSearchHits` across every read path (`search`, `get`, `readFile`)**:
  - Never forward a synthesized fallback `scope.namespace` (such as `"default:u:default"`) into `authorizeSearchHits` when `scope.hasExplicitIdentity === false`; pass `params.namespace ?? (scope.hasExplicitIdentity ? scope.namespace : undefined)` so anonymous callers always fail closed (`[]` / `null`).
  - Populate `hitCandidate` consistently in `get()`, `search()`, and `getMemorySearchManager().readFile()`, copying own `namespace`, `agent_id`, `agentId`, `session_id`, `sessionId`, `metadata`, and `provenance` and forwarding all caller context fields.
  - Defend against spoofing and prototype inheritance: read properties with `Object.hasOwn` and verify uniqueness across top-level, `metadata`, and `provenance` for each identity dimension (`namespace`, `agent_id`/`agentId`, `session_id`/`sessionId`), dropping any hit with conflicting values or inherited properties.
- **Gateway reload rebinding (`WeakMap` + getter closure)**:
  - Never track registered APIs with a `WeakSet` and frozen closures—reloading `register(api1) -> register(api2) -> register(api3) -> register(api1)` leaves `api1` bound to closed `r1` and leaves `r3` open.
  - Use `WeakMap<object, { runtime: EbbmemOpenClawRuntime }>()` and bind tools/hooks/CLI handlers to `() => binding.runtime`. Re-registering an existing `api` closes the superseded active runtime, updates `binding.runtime`, refreshes `registerMemoryCapability`, and avoids duplicate listeners.
- **Cross-turn & cross-session exposure isolation**:
  - Key `turnExposures` with a namespace fallback (`__ns__\u0000${namespace}`) when `agentId` and `sessionId` are omitted.
  - Clear active turn exposure at the top of `before_agent_start` **before** early returns (`autoRecall: false`, setup phrases, empty text), and delete it in `search()` when zero hits remain.
  - If `authorizeSearchHits` drops any hit, overwrite `core.tracker.recordExposure` with the authorized subset only.
  - In `reinforceFromExposure`, consume the turn entry and pass a sentinel `turnId` (`"__no_active_turn__"`) when no active turn exists so `ExposureBookkeeper.getLatest(namespace)` is never used implicitly.
- **CLI, fail-open & compiled test paths**:
  - Register every Commander flag explicitly (including variadic `--agent-namespace <mapping...>`) and make test `MockCommanderNode` throw on unregistered options.
  - Keep dynamic `resolveScopeForContext` inside fail-open `try/catch` blocks while keeping static `parsePluginConfig()` strict at startup.
  - Resolve repo/package fixture paths relative to compiled `build/test/test/*.test.js` depth (or walk up to `package.json`).

## 5. Open WebUI Filter Function (`plugins/open-webui/`)

- **Standalone + `ebbmem_integration_core` dual-mode parity**:
  - Because `ebbmem-integration-core` is not yet on PyPI, `ebbmem_filter.py` must run standalone with `requirements: pydantic` and stdlib `urllib.request` (via `asyncio.to_thread`), while optionally delegating when `ebbmem_integration_core` is importable.
  - Always run `pytest -v plugins/open-webui/tests` **twice**: once standalone (core absent/shadowed) and once with `PYTHONPATH=packages/python/src:conformance/src` (`;` on Windows) to catch standalone vs. `with_core` divergence.
- **Redaction & URL hardening**:
  - In `outlet()`, always run `redact_secrets(raw_turn)` **before** slicing `[:_MAX_CAPTURE_CHARS]` so PEM blocks or JWTs spanning char 4,000 never leak raw suffixes.
  - Run a unified single-line redaction pipeline in both standalone and `with_core` modes (unclosed quotes must stop at `\r`/`\n`; `_URL_USERINFO_RE` must match `https://:pw@host` and `https://u:p@ss@host` without matching `https://host:8080/path user@example.com`).
  - In `validate_base_url()`, reject ASCII control chars (`ord < 32` or `127`) **before** `.strip()`, then reject `?query` and `#fragment` without echoing the URL.
- **Per-user isolation, `UserValves` & fault domains**:
  - Derive `<tenant>:u:<sanitized_id>` from raw (unstripped) `__user__["id"]` (rejecting empty/whitespace-only) and append an 8-hex SHA-256 suffix whenever `sanitized != raw_id` so `"alice"`, `"alice\n"`, `" alice "`, and `"other:u:alice"` map to distinct namespaces.
  - Restrict `UserValves` strictly to `enabled`, clamped `recall_limit`, and opt-out-only `auto_remember`. Coerce string booleans (`"false"`, `"0"`, `"no"`, `"off"`) in raw dict `UserValves` and ignore `bool` for `recall_limit`.
  - Isolate `auto_reinforce` and `auto_remember` in separate `try/except` blocks so an HTTP 503 on `/reinforce` does not abort `auto_remember`.
  - Handle multimodal content items with `"type": None` identically to omitted `"type"` without mutating caller `body` dicts/lists.

## 6. Host Bundles, Copilot Configs & Official MCP Registry

- **Validate every JSON manifest against its published Draft-07 schema**:
  - Claude Code (`plugins/claude-code/`): `claude-code-plugin-manifest.json`, `claude-code-marketplace.json`, `claude-code-settings.json#/properties/hooks`, `#/properties/mcpServers`.
  - Cursor (`plugins/cursor/`): official `cursor/plugins` `plugin.json` and `marketplace.json` schemas; verify base64 install deeplinks decode to valid JSON.
  - GitHub Copilot (`configs/copilot/`): VS Code uses `servers` + `inputs` (`type: "http"`); Copilot cloud agent uses `mcpServers`, `$COPILOT_MCP_` secret headers, and a 6-tool `core` allowlist (tools-only, no OAuth).
- **Claude Code hooks (`plugins/claude-code/hooks/ebbmem_hook.py`)**: Keep pure Python 3 stdlib-only, guard `stop_hook_active` against infinite loops, redact `Bearer`, `ebb_*`, and `api_key=`, and fail open.
- **Official MCP Registry (`registry/server.json`)**:
  - Save UTF-8 **without BOM**; validate with `mcp-publisher validate registry/server.json` (never run `login` or `publish` without explicit approval) and Draft-07 `server.schema.json` (`2025-12-11`).
  - Omit `packages` until a standalone MCP server package is published (client/adapter libraries do not declare `mcpName`).
  - Documented domain verification invariants for `ai.ebbmem/ebbmem`: strip the 12-byte ASN.1 SPKI header from DER Ed25519 public keys for `p=`, pass the 64-hex raw seed (never a PEM file) to `mcp-publisher`, and if using HTTP `.well-known/mcp-registry-auth` on static hosting with dotfile ignores, place `"!**/.well-known/**"` above `"**/.*"`.
