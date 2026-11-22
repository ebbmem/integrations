# Host MCP Quickstarts

Remote [Streamable HTTP](https://modelcontextprotocol.io) quickstarts for connecting coding assistants and desktop clients to [EbbMem](https://ebbmem.ai).

> **Note:** Snippets in this directory use placeholder URLs (`https://ebbmem.example.com`) and have **not** been tested live against a public hosted endpoint yet. Live end-to-end verification is tracked as a follow-up once a reachable HTTPS deployment is available.

## Available Host Guides

| Host | Auth Variants | Guide |
| --- | --- | --- |
| **VS Code + GitHub Copilot** | OAuth 2.1, API key (`inputs`), Copilot cloud agent (`COPILOT_MCP_`) | [vscode-copilot.md](./vscode-copilot.md) |
| **Claude Code + Claude Desktop** | OAuth 2.1, API key (`--header` / stdio bridge) | [claude.md](./claude.md) |
| **Cursor** | OAuth 2.1, API key (`headers`), install deeplink | [cursor.md](./cursor.md) |

## MCP Endpoint Profiles & Security Rules

- **Endpoint profiles:**
  - `/mcp/core` (**recommended default**): 6 core tools (`memory_remember`, `memory_recall`, `memory_reinforce`, `memory_forget`, `memory_context_pack`, `memory_status`).
  - `/mcp/full`: 15 tools, adding `memory_search` / `memory_get` aliases, episodic timeline tools, and association/introspection tools.
  - `/mcp/admin`: 18 tools, adding admin-gated maintenance tools (`memory_dream`, `memory_reflect`, `memory_purge`).
- **Header-only credentials:** Send keys and bearer tokens strictly in `Authorization: Bearer <token>` or `X-API-Key` headers, never in URL query strings.
- **Cognitive discipline:** `memory_recall` is strictly read-only. Memories decay over time unless reinforced; call `memory_reinforce` only for memories actually used in an answer or action, keeping `injected` (reinforce-all-shown) OFF by default.

## References

- [Repository Overview](../../../README.md)
- [Agent Instruction Pack](../../../instructions/README.md)
- [GitHub Copilot Config Snippets](../../../configs/copilot/README.md)
- [Conformance Kit](../../../conformance/README.md)
