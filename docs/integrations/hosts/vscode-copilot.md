# VS Code and GitHub Copilot Integration Quickstart

Connect Visual Studio Code (Agent Mode / Copilot Chat) and the GitHub Copilot cloud agent to [EbbMem](https://ebbmem.ai) over remote Streamable HTTP (`/mcp/core`), enabling read-only `recall`, secret-redacted `remember`, and explicit post-use `reinforce`.

> **Untested live notice:** All snippets below use placeholder URLs (`https://ebbmem.example.com`) and are derived from current VS Code and GitHub Copilot documentation. Live end-to-end verification against a deployed public HTTPS endpoint is pending as a follow-up.

## What You Need

- **EbbMem endpoint:** A reachable EbbMem Streamable HTTP endpoint such as `https://ebbmem.example.com/mcp/core` (or `http://127.0.0.1:8320/mcp/core` for local self-hosting).
  - Use `/mcp/core` (6 core tools) for everyday coding, `/mcp/full` (15 tools, including `memory_search` / `memory_get` aliases and episodic/association tools) when broader inspection is needed, or `/mcp/admin` (18 tools) only with admin-scoped credentials.
- **Authentication:**
  - **VS Code (IDE):** Supports **OAuth 2.1** (RFC 9728 Protected Resource Metadata discovery + Client ID Metadata Documents) or **API-key headers** (`X-API-Key` / `Authorization: Bearer <token>`) prompted via VS Code `inputs`.
  - **GitHub Copilot cloud agent & code review:** Supports **API-key headers only** via `COPILOT_MCP_` repository secrets (no OAuth flow in cloud sandboxes).
- **Namespace:** Target namespace such as `acme:u:alice` passed via `X-Ebb-Namespace` (avoid per-thread or per-turn namespaces).
- **Target environment:** Visual Studio Code with GitHub Copilot Chat enabled; for Copilot cloud agent, repository admin access and the organization/enterprise `"MCP servers in Copilot"` policy enabled.
- **Optional agent instructions:** Copy [`instructions/AGENTS.md`](../../../instructions/AGENTS.md) or [`instructions/CORE.md`](../../../instructions/CORE.md) into your repository so the agent knows to reinforce only actually-used memories.

## Config Snippet

Ready-to-copy JSON files are also stored under [`configs/copilot/`](../../../configs/copilot/README.md).

### Option A: VS Code `.vscode/mcp.json` — API Key (with Secure Input Prompt)

VS Code workspace MCP configurations use `"servers"` at the top level and `"type": "http"`. Using `"inputs"` with `"password": true` keeps API keys out of git:

```json
{
  "servers": {
    "ebbmem": {
      "type": "http",
      "url": "https://ebbmem.example.com/mcp/core",
      "headers": {
        "X-API-Key": "${input:ebbmem_api_key}",
        "X-Ebb-Namespace": "acme:u:alice"
      }
    }
  },
  "inputs": [
    {
      "type": "promptString",
      "id": "ebbmem_api_key",
      "description": "EbbMem API key (sent via X-API-Key header)",
      "password": true
    }
  ]
}
```

See [`configs/copilot/vscode.mcp.json`](../../../configs/copilot/vscode.mcp.json).

### Option B: VS Code `.vscode/mcp.json` — OAuth 2.1

When your EbbMem server has OAuth 2.1 enabled on `/mcp`, omit static credential headers and let VS Code discover authorization metadata via RFC 9728 `WWW-Authenticate`:

```json
{
  "servers": {
    "ebbmem": {
      "type": "http",
      "url": "https://ebbmem.example.com/mcp/core"
    }
  }
}
```

See [`configs/copilot/vscode-oauth.mcp.json`](../../../configs/copilot/vscode-oauth.mcp.json).

### VS Code One-Click Install Links

VS Code supports `vscode:mcp/install?<url-encoded-json>` (or `vscode-insiders:mcp/install?...`) and the GitHub-safe HTTPS redirect wrapper `https://vscode.dev/redirect?url=...`:

```text
vscode:mcp/install?%7B%22name%22%3A%22ebbmem%22%2C%22type%22%3A%22http%22%2C%22url%22%3A%22https%3A%2F%2Febbmem.example.com%2Fmcp%2Fcore%22%7D
```

Web/Markdown-safe redirect form:

```markdown
[Install EbbMem in VS Code](https://vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522ebbmem%2522%252C%2522type%2522%253A%2522http%2522%252C%2522url%2522%253A%2522https%253A%252F%252Febbmem.example.com%252Fmcp%252Fcore%2522%257D)
```

> VS Code always opens a confirmation view displaying the server configuration before installing.

### Option C: GitHub Copilot Cloud Agent & Code Review (Repository Settings)

GitHub Copilot cloud agent and code review use a top-level `"mcpServers"` object configured in **Repository Settings -> Copilot -> Coding agent** on GitHub.com. Key constraints for Copilot cloud agent:

- **Tools only, no OAuth:** The cloud agent only consumes MCP tools (no MCP resources or prompts) and does not perform interactive OAuth flows.
- **Secret prefix:** Store your API key in GitHub repository/environment agent secrets with the mandatory `COPILOT_MCP_` prefix (for example `COPILOT_MCP_EBBMEM_API_KEY`).
- **Least-privilege allowlist:** Restrict `"tools"` explicitly to the six `core` tools rather than `"*"`.

```json
{
  "mcpServers": {
    "ebbmem": {
      "type": "http",
      "url": "https://ebbmem.example.com/mcp/core",
      "headers": {
        "Authorization": "Bearer $COPILOT_MCP_EBBMEM_API_KEY",
        "X-Ebb-Namespace": "acme:u:alice"
      },
      "tools": [
        "memory_remember",
        "memory_recall",
        "memory_reinforce",
        "memory_forget",
        "memory_context_pack",
        "memory_status"
      ]
    }
  }
}
```

See [`configs/copilot/copilot-cloud-agent.mcp.json`](../../../configs/copilot/copilot-cloud-agent.mcp.json) and [`configs/copilot/README.md`](../../../configs/copilot/README.md).

#### Organization & Enterprise Policy Note

- An organization or enterprise administrator must enable the **`MCP servers in Copilot`** policy (`Settings -> Copilot -> Policies` for organizations, or `AI controls -> MCP` for enterprises) before Copilot clients or cloud agents can connect to MCP servers.
- Administrators can also restrict endpoints via `allowedMcpServers` / `deniedMcpServers` in `managed-settings.json`, custom MCP registry enforcement, and Copilot custom firewall egress rules.
- Because the Copilot cloud agent invokes configured repository MCP tools autonomously without per-call interactive approval, keep the `tools` allowlist scoped to `/mcp/core` tools only.

## First Memory in Under Five Steps

1. **Add the EbbMem server:** Save `.vscode/mcp.json` (Option A or B) in your workspace and click **Start** in the MCP server editor lens (or open the Command Palette -> `MCP: List Servers`).
2. **Authenticate:** Complete the OAuth browser sign-in (Option B) or enter your API key when prompted by `${input:ebbmem_api_key}` (Option A).
3. **Store a durable memory:** In Copilot Chat (Agent mode), ask Copilot to remember a project convention or preference via `memory_remember` (`tier: "semantic"`, never storing secrets or tokens).
4. **Recall before answering:** In a new chat session, ask about that convention and confirm `memory_recall` retrieves it as reference data without altering storage strength.
5. **Reinforce used memories:** Verify the agent calls `memory_reinforce` (`memory_id`, `used_ids`, and `recall_id` when returned) only for memories that actually informed its answer, leaving `injected` mode OFF.

## Troubleshooting

- **`400 Bad Request` mentioning URL credentials:** Never append `?token=` or `?api_key=` to the `url`. Keep credentials in `headers` or OAuth bearer tokens.
- **`401 Unauthorized` in VS Code OAuth mode:** Confirm your EbbMem server serves `/.well-known/oauth-protected-resource` (RFC 9728) and check the VS Code MCP output channel.
- **Copilot cloud agent ignores the server:** Confirm the **`MCP servers in Copilot`** organization/enterprise policy is enabled, the secret name starts with `COPILOT_MCP_`, and the endpoint is reachable from GitHub's cloud sandbox over HTTPS.
- **`403` / `-32003` on admin tools:** `/mcp/core` intentionally excludes `memory_dream`, `memory_reflect`, and `memory_purge`. Use `/mcp/core` for agent workflows.
- **Memories decay despite frequent recalls:** `memory_recall` is read-only. Confirm your instructions prompt `memory_reinforce` on memories that were actually used.

## References

- [Host MCP Quickstarts](./README.md)
- [GitHub Copilot Config Snippets](../../../configs/copilot/README.md)
- [Agent Instruction Pack](../../../instructions/README.md)
- [Conformance Kit](../../../conformance/README.md)
