# EbbMem Plugin for Claude Code

Official Claude Code plugin bundling the EbbMem remote Streamable HTTP MCP server (`/mcp/core`), the EbbMem memory discipline skill, zero-telemetry lifecycle hooks (`SessionStart` recall + `Stop` capture and reinforcement), and a local marketplace catalog for `/plugin install`.

> **Untested live notice:** Default endpoints use placeholder URLs (`https://ebbmem.example.com/mcp/core`). Live verification against a public hosted EbbMem endpoint is pending as a follow-up. This plugin has **not** been submitted to any external Claude plugin directory.

## What Is Included

| Path | Purpose |
| --- | --- |
| `.claude-plugin/plugin.json` | Plugin manifest (`https://json.schemastore.org/claude-code-plugin-manifest.json`) |
| `.claude-plugin/marketplace.json` | Local marketplace catalog (`https://json.schemastore.org/claude-code-marketplace.json`) |
| `.mcp.json` | Remote Streamable HTTP MCP server definition (`/mcp/core`, header-only auth) |
| `skills/ebbmem-memory/SKILL.md` | Agent skill copied from [`instructions/claude-code/SKILL.md`](../../instructions/claude-code/SKILL.md) |
| `hooks/hooks.json` & `hooks/ebbmem_hook.py` | Cross-platform Python 3 stdlib hooks (`SessionStart` read-only recall, `Stop` redacted capture + used-memory reinforcement) |
| `tests/test_ebbmem_hook.py` | Unit and local mock-MCP integration tests for hooks and schema validation |

## Installation from Local Marketplace

1. **Export environment variables** before starting Claude Code:

   ```bash
   export EBBMEM_MCP_URL="https://ebbmem.example.com/mcp/core"
   export EBBMEM_API_KEY="ebb_your_api_key"
   export EBBMEM_NAMESPACE="acme:u:alice"
   ```

   *(If your server uses OAuth 2.1 instead of static API keys, omit `EBBMEM_API_KEY` from `.mcp.json` and authenticate via `/mcp` inside Claude Code.)*

2. **Add the local marketplace and install the plugin** from the repository root:

   ```text
   /plugin marketplace add ./plugins/claude-code
   /plugin install ebbmem@ebbmem-local
   ```

## Hook Behavior & Safety Guarantees

- **Zero telemetry:** Hooks send requests only to your configured `EBBMEM_MCP_URL`.
- **Cross-platform:** `hooks/ebbmem_hook.py` uses Python 3 standard library modules only (`python3` or `python` on Linux, macOS, and Windows Git Bash).
- **Header-only auth & URL safety:** Rejects credentials embedded in URL query parameters (`?token=`, `?api_key=`) or userinfo (`user:pass@host`).
- **`SessionStart` (read-only recall):** Queries `memory_recall`, wraps recalled text in `<ebbmem_untrusted_memory_context>` delimiters, and injects it via `hookSpecificOutput.additionalContext`.
- **`Stop` (redacted capture & selective reinforcement):** Checks `stop_hook_active` to avoid recursive loops, redacts API keys/tokens/private keys before calling `memory_remember` (`tier: "episodic"` with deterministic SHA-256 `idempotency_key`), and calls `memory_reinforce` (passing `recall_id` when available) **only** for recalled memories actually referenced in the assistant's response — keeping `injected` (reinforce-all-shown) OFF by default.
- **Fail-open:** If `EBBMEM_MCP_URL` / `EBBMEM_API_KEY` are unset or the server is unreachable, hooks exit `0` without interrupting Claude Code.

## Running Hook & Schema Tests

```bash
pytest -v plugins/claude-code/tests
```
