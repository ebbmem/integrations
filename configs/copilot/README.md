# GitHub Copilot & VS Code MCP Configuration Snippets

Ready-to-adapt MCP server configurations for [EbbMem](https://ebbmem.ai) in Visual Studio Code and the GitHub Copilot cloud agent / code review.

> **Untested live notice:** Placeholder URLs (`https://ebbmem.example.com`) are used throughout. Live testing against a deployed public HTTPS endpoint and Copilot cloud sandbox is tracked as a follow-up. No GitHub MCP Registry submission has been made.

## Files

| File | Target | Auth Mode |
| --- | --- | --- |
| [`vscode.mcp.json`](./vscode.mcp.json) | `.vscode/mcp.json` (VS Code workspace) | `X-API-Key` header via `${input:ebbmem_api_key}` prompt |
| [`vscode-oauth.mcp.json`](./vscode-oauth.mcp.json) | `.vscode/mcp.json` (VS Code workspace) | OAuth 2.1 (RFC 9728 discovery) |
| [`copilot-cloud-agent.mcp.json`](./copilot-cloud-agent.mcp.json) | GitHub repo Copilot coding agent settings (`mcpServers`) | `Authorization: Bearer $COPILOT_MCP_EBBMEM_API_KEY` |

## Copilot Cloud Agent & Code Review Requirements

1. **Repository settings placement:** Paste the contents of [`copilot-cloud-agent.mcp.json`](./copilot-cloud-agent.mcp.json) into **Repository Settings -> Copilot -> Coding agent -> MCP configuration** on GitHub.com (uses top-level `mcpServers`, unlike VS Code's `.vscode/mcp.json` `servers` key).
2. **`COPILOT_MCP_` secret prefix:** Create an agent secret in repository or environment settings whose name starts with `COPILOT_MCP_` (for example `COPILOT_MCP_EBBMEM_API_KEY`). Reference it via `$COPILOT_MCP_EBBMEM_API_KEY` inside `headers`. Never embed raw keys or query-string credentials (`?token=`, `?api_key=`).
3. **Tools allowlist (`core` tools only):** The snippet restricts `"tools"` to the six `/mcp/core` tools (`memory_remember`, `memory_recall`, `memory_reinforce`, `memory_forget`, `memory_context_pack`, `memory_status`), preventing access to `/mcp/admin` tools (`memory_dream`, `memory_reflect`, `memory_purge`).
4. **Tools only, no OAuth:** Copilot cloud agent consumes MCP **tools only** (not MCP resources or prompts) and does **not** support interactive OAuth flows — use an API-key header backed by a `COPILOT_MCP_` secret.
5. **Organization / enterprise policy:** An administrator must enable **`MCP servers in Copilot`** (`Organization Settings -> Copilot -> Policies` or `Enterprise AI controls -> MCP`) before Copilot clients or cloud agents can connect. Once enabled on a repository, Copilot cloud agent and code review execute allowlisted MCP tools autonomously without per-call human confirmation.
6. **GitHub MCP Registry:** Listing EbbMem in the GitHub MCP Registry requires explicit approval and is not included here.

## References

- [VS Code & GitHub Copilot Quickstart](../../docs/integrations/hosts/vscode-copilot.md)
- [Agent Instruction Pack](../../instructions/README.md)
