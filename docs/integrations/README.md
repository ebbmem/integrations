# EbbMem Integrations

Welcome to the [EbbMem](https://ebbmem.ai) integrations hub. This repository provides an MCP-first universal tool surface specification, thin native framework core packages, an IC-1 conformance suite, and an agent instruction pack designed to preserve cognitive memory decay and reinforcement across every host and framework.

## Core Memory Invariants

Every EbbMem integration—whether over MCP or a native framework adapter—must preserve the same memory semantics:

1. **Read-only recall, decay unless reinforced:** Calling `recall` (`memory_recall` / `memory_search`) never increases storage strength or resets spacing state. Unused memories decay over time.
2. **Explicit usage feedback:** Call `reinforce` (`memory_reinforce`) with `memory_id`, `used_ids`, and optional `recall_id` (when exposure logging is enabled) **only** for memories actually used to answer or act. `injected` (reinforce-all-shown) mode stays **OFF** by default.
3. **Untrusted recall framing and redaction:** Wrap recalled content in `<ebbmem_untrusted_memory_context>` delimiters as reference data (never executable instructions) and redact secrets before `remember` or logging.
4. **Header-only credentials:** Send tokens exclusively via `Authorization: Bearer <token>` or `X-API-Key` headers—never in URL query parameters or userinfo.
5. **Fail-open resilience and safe retries:** Degrade gracefully (`fail_open = true`) if EbbMem is unreachable, retry only idempotent operations (and keyed `remember` with `idempotency_key`), and keep telemetry **OFF** by default.

## MCP Surface Overview

EbbMem exposes a Streamable HTTP `/mcp` endpoint (with profile routes `/mcp/core`, `/mcp/full`, and `/mcp/admin`, or the `X-Ebb-MCP-Profile` header) as well as local stdio tooling. Details are defined in the public descriptor [`conformance/golden/mcp_surface_v2.json`](../../conformance/golden/mcp_surface_v2.json).

- **Supported MCP protocol revisions:** `2026-07-28` (preferred stateless revision using `MCP-Protocol-Version`, `Mcp-Method`, and `Mcp-Name` headers), plus `2025-11-25`, `2025-06-18`, `2025-03-26`, and `2024-11-05`.
- **Tool profiles:**
  - `core` (6 tools, recommended default): `memory_remember`, `memory_recall`, `memory_reinforce`, `memory_forget`, `memory_context_pack`, `memory_status`.
  - `full` (15 tools): adds `memory_associate`, `memory_why_not`, `memory_introspect`, `memory_episodic_remember`, `memory_episodic_tail`, `memory_federated_recall`, `memory_rehearse`, plus universal aliases `memory_search` and `memory_get`.
  - `admin` (18 tools): adds role-gated `memory_dream`, `memory_reflect`, and `memory_purge`.
- **Tool metadata:** Tools include `title`, behavioral `annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`), and `outputSchema` / `structuredContent` for `memory_remember`, `memory_recall`, `memory_reinforce`, `memory_search`, and `memory_get`.

## Repository Foundations (`available` on `main`)

| Component | Path | Status | Summary |
| --- | --- | --- | --- |
| Conformance Kit (`ebbmem-conformance`) | [`conformance/README.md`](../../conformance/README.md) | `available` | 21 IC-1 YAML scenarios, stateful reference mock server, and MCP v2 surface validation |
| Agent Instruction Pack | [`instructions/README.md`](../../instructions/README.md) | `available` | Canonical [`CORE.md`](../../instructions/CORE.md) and wrappers for [`SKILL.md`](../../instructions/SKILL.md), [`AGENTS.md`](../../instructions/AGENTS.md), Cursor, OpenClaw, and Claude Code |
| Python Integration Core (`ebbmem-integration-core`) | [`packages/python/README.md`](../../packages/python/README.md) | `available` | Scoping, secret redaction, untrusted framing, fail-open/retry, reinforcement tracking, and optional `ebbmem-client` adapter (source on `main`) |
| TypeScript Integration Core (`ebbmem-integration-core`) | [`packages/typescript/README.md`](../../packages/typescript/README.md) | `available` | Scoping, secret redaction, untrusted framing, fail-open/retry, reinforcement tracking, and structural `@ebbmem/client` adapter (source on `main`) |
| Quickstart Template | [`_quickstart-template.md`](./_quickstart-template.md) | `available` | Standard template for host and framework integration quickstarts |

## Target Coverage Matrix

Status values reflect **only** what exists on the `main` branch of `ebbmem/integrations`:

- `available` — dedicated guide, plugin, or adapter package is present on `main`.
- `in progress` — partial support (such as a platform-specific wrapper in `instructions/`) exists on `main`, while the full guide, bundle, or adapter package is not yet on `main`.
- `planned` — target integration guide, recipe, or native adapter package is not yet on `main`.

<!-- BEGIN_COMPAT_TABLE -->
Badge tiers (evaluated by [`conformance/ts/src/badges.ts`](../../conformance/ts/src/badges.ts)):

- `gold` — Full IC-1 mock and live suites pass with MCP surface and safety invariants verified.
- `silver` — Full IC-1 mock suite passes with safety invariants verified (live run pending).
- `bronze` — Available on `main` with validated MCP surface, config schema, or partial mock hook checks.
- `unverified` — `in progress` / `planned`, or failed a mandatory safety invariant gate.

| Target | MCP Remote (`/mcp`) | MCP Local (`stdio`) | Native Adapter / Integration Shape | Distribution / Artifact | Status | Badge | On `main` & Notes |
| --- | :---: | :---: | --- | --- | :---: | :---: | --- |
| GitHub Copilot (VS Code) | Yes | Yes | — | `.vscode/mcp.json`, registry entry | `available` | `bronze` | [`hosts/vscode-copilot.md`](./hosts/vscode-copilot.md), [`configs/copilot/vscode.mcp.json`](../../configs/copilot/vscode.mcp.json), and [`registry/server.json`](../../registry/server.json) on `main`; untested live |
| GitHub Copilot cloud agent / code review | Yes (API key, no OAuth) | No | — | Repo settings snippet | `available` | `bronze` | [`configs/copilot/copilot-cloud-agent.mcp.json`](../../configs/copilot/copilot-cloud-agent.mcp.json) and [`hosts/vscode-copilot.md`](./hosts/vscode-copilot.md) on `main`; untested live |
| Claude Code / Claude Desktop | Yes | Yes | — | Plugin + marketplace | `available` | `bronze` | [`plugins/claude-code/`](../../plugins/claude-code/README.md), [`hosts/claude.md`](./hosts/claude.md), and [`instructions/claude-code/SKILL.md`](../../instructions/claude-code/SKILL.md) on `main`; hook tests against mock MCP; untested live |
| Cursor | Yes | Yes | — | Install deeplink, plugin | `available` | `bronze` | [`plugins/cursor/`](../../plugins/cursor/README.md), [`hosts/cursor.md`](./hosts/cursor.md), and [`instructions/cursor/ebbmem.mdc`](../../instructions/cursor/ebbmem.mdc) on `main`; untested live |
| Grok Bot / xAI API | Yes (public HTTPS) | No | — | Connector guide, API snippet | `planned` | `unverified` | Requires public HTTPS `/mcp` endpoint |
| LangGraph / LangChain / LangMem | Optional | No | `BaseStore`, middleware, tools | PyPI package | `available` | `silver` | [`packages/langgraph-python/`](../../packages/langgraph-python/README.md) (`ebbmem-langgraph`) source and mock IC-1 tests on `main`; PyPI publish and live pending |
| OpenClaw | Fallback | Fallback | Memory-slot plugin (`memory_search` / `memory_get`) | ClawHub package | `in progress` | `unverified` | Skill [`instructions/openclaw/SKILL.md`](../../instructions/openclaw/SKILL.md) on `main`; plugin package pending |
| Open WebUI | Yes | Via `mcpo` | Filter Function | Community function | `planned` | `unverified` | Filter Function and native MCP guide pending |
| Vercel AI SDK | Optional | No | Tools + model middleware | npm package | `available` | `silver` | [`packages/ai-sdk/`](../../packages/ai-sdk/README.md) (`@ebbmem/ai-sdk`) source and mock IC-1 tests on `main`; npm publish and live pending |
| OpenAI Agents SDK / ChatGPT | Yes (ChatGPT connector) | No | `Session` + memory tools | PyPI package, connector recipe | `available` | `silver` | [`packages/openai-agents-python/`](../../packages/openai-agents-python/README.md) (`ebbmem-openai-agents`) source, mock tests, and [`docs/recipes/chatgpt.md`](../recipes/chatgpt.md) on `main`; PyPI publish and live pending |
| LlamaIndex | Optional | No | Memory block | PyPI package | `planned` | `unverified` | Builds on [`packages/python/README.md`](../../packages/python/README.md) |
| CrewAI | Optional | No | Storage backend | PyPI package | `planned` | `unverified` | Builds on [`packages/python/README.md`](../../packages/python/README.md) |
| Google ADK | Optional | No | Memory service | PyPI package | `planned` | `unverified` | Builds on [`packages/python/README.md`](../../packages/python/README.md) |
| Microsoft Agent Framework / Semantic Kernel / AutoGen | Optional | No | Context provider | NuGet / PyPI | `planned` | `unverified` | Native provider + MCP support |
| Pydantic AI, Mastra | Yes | Yes | Setup recipes | Docs recipes | `planned` | `unverified` | MCP-first integration guides |
| n8n, Dify, Flowise, LibreChat | Yes | LibreChat only | Setup recipes | Docs recipes | `planned` | `unverified` | MCP-first workflow & UI guides |
<!-- END_COMPAT_TABLE -->

## Contributing an Integration Guide or Adapter

- Start from the [`_quickstart-template.md`](./_quickstart-template.md) template when adding a new host or recipe guide.
- Read [`CONTRIBUTING.md`](../../CONTRIBUTING.md) for DCO sign-off (`git commit -s`), public-repository hygiene rules, and local test commands.
- Validate all adapters against [`conformance/README.md`](../../conformance/README.md) (`ebbmem-conformance --mode mock`).
- For machine-readable documentation discovery, see [`llms.txt`](../../llms.txt) and [`README.md`](../../README.md).
