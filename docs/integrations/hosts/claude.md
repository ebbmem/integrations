# Claude Code and Claude Desktop Integration Quickstart

Connect **Claude Code** (CLI / IDE) and **Claude Desktop** to [EbbMem](https://ebbmem.ai) over remote Streamable HTTP (`/mcp/core`), enabling read-only `recall`, secret-redacted `remember`, and explicit post-use `reinforce`.

> **Untested live notice:** All snippets below use placeholder URLs (`https://ebbmem.example.com`) and are derived from current Anthropic Claude Code and Claude Desktop documentation. Live end-to-end verification against a deployed public HTTPS endpoint is pending as a follow-up.

## What You Need

- **EbbMem endpoint:** A reachable EbbMem Streamable HTTP URL such as `https://ebbmem.example.com/mcp/core` (6 core tools — recommended default), `/mcp/full` (15 tools, adding `memory_search` / `memory_get` aliases and episodic/association tools), or `/mcp/admin` (18 tools, admin-gated).
- **Authentication:**
  - **OAuth 2.1:** Supported natively by Claude Code (`/mcp` auth flow) and Claude Desktop Custom Connectors over public HTTPS (port 443).
  - **API key (header-only):** `X-API-Key` or `Authorization: Bearer <token>`. Never place credentials in URL query parameters.
- **Namespace:** Target namespace such as `acme:u:alice` via `X-Ebb-Namespace` (avoid per-thread or per-turn namespaces).
- **Agent skill (recommended):** Copy [`instructions/claude-code/SKILL.md`](../../../instructions/claude-code/SKILL.md) to `.claude/skills/ebbmem/SKILL.md`, or install the local plugin bundle in `plugins/claude-code/` once available.

## Config Snippet

### Option A: Claude Code CLI — OAuth 2.1

Register the remote HTTP server without static headers and authenticate via `/mcp` inside Claude Code:

```bash
claude mcp add --transport http ebbmem https://ebbmem.example.com/mcp/core
```

Add `--scope project` to write a shareable `.mcp.json` file at your repository root:

```bash
claude mcp add --scope project --transport http ebbmem https://ebbmem.example.com/mcp/core
```

Verify connection status with `claude mcp list` and run `/mcp` inside a session to complete OAuth sign-in.

### Option B: Claude Code CLI — API Key (`--header`)

Pass the API key and namespace strictly via HTTP headers:

```bash
claude mcp add --transport http ebbmem https://ebbmem.example.com/mcp/core \
  --header "X-API-Key: ${EBBMEM_API_KEY}" \
  --header "X-Ebb-Namespace: acme:u:alice"
```

Equivalent project-scoped `.mcp.json`:

```json
{
  "mcpServers": {
    "ebbmem": {
      "type": "http",
      "url": "https://ebbmem.example.com/mcp/core",
      "headers": {
        "Authorization": "Bearer ${EBBMEM_API_KEY}",
        "X-Ebb-Namespace": "acme:u:alice"
      }
    }
  }
}
```

### Option C: Claude Desktop — Native Custom Connector (OAuth 2.1)

For publicly reachable HTTPS EbbMem servers (port `443`, reachable from Anthropic's connector infrastructure):

1. Open **Claude Desktop -> Settings -> Connectors** (or **Customize -> Connectors**).
2. Click **Add custom connector**.
3. Enter your server URL (`https://ebbmem.example.com/mcp/core`).
4. Optionally expand **Advanced settings** to supply an OAuth Client ID / Secret if your authorization server requires pre-registered credentials.
5. Click **Add**, then **Connect** to complete OAuth authorization.

### Option D: Claude Desktop — Local Bridge (`claude_desktop_config.json`, API Key)

If your server uses API-key headers rather than OAuth or is only reachable locally, bridge stdio to Streamable HTTP via `mcp-remote` in `claude_desktop_config.json` (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows; requires Node.js 18+):

```json
{
  "mcpServers": {
    "ebbmem": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://ebbmem.example.com/mcp/core",
        "--header",
        "X-API-Key: ${EBBMEM_API_KEY}",
        "--header",
        "X-Ebb-Namespace: acme:u:alice"
      ]
    }
  }
}
```

Fully quit and restart Claude Desktop after editing `claude_desktop_config.json`.

> **Tip:** If you want automatic session-start recall, stop-time capture/reinforcement hooks, and the EbbMem skill bundled together for Claude Code, use the plugin package under `plugins/claude-code/`.

## First Memory in Under Five Steps

1. **Connect EbbMem:** Run `claude mcp add --transport http ...` (Claude Code) or add the Custom Connector / config block (Claude Desktop), and verify the server shows connected in `claude mcp list` or Connectors settings.
2. **Install the skill:** Place [`instructions/claude-code/SKILL.md`](../../../instructions/claude-code/SKILL.md) in `.claude/skills/ebbmem/SKILL.md` so Claude follows EbbMem decay and redaction rules.
3. **Remember a durable fact:** Ask Claude to remember a workspace decision or user preference with `memory_remember` (`tier: "semantic"`, never storing secrets or API keys).
4. **Recall before answering:** Start a new session and ask about that decision; confirm `memory_recall` retrieves it without mutating storage strength.
5. **Reinforce used memories:** Confirm Claude calls `memory_reinforce` (`memory_id`, `used_ids`, and `recall_id` when returned) only for memories actually used in the answer, keeping `injected` OFF.

## Troubleshooting

- **`400 Bad Request` on URL credentials:** Move any `token` or `api_key` out of the URL query string into `--header` / `headers` or OAuth bearer tokens.
- **Claude Desktop Custom Connector cannot reach the server:** Custom Connectors require a publicly reachable HTTPS endpoint on port `443` (not `127.0.0.1` or an internal VPN). Use Option D (`claude_desktop_config.json`) for local or header-key setups.
- **`claude mcp list` fails to authenticate:** Run `/mcp` inside an active Claude Code session to refresh OAuth login, or confirm `${EBBMEM_API_KEY}` is exported in the shell environment before starting Claude Code.
- **`404 Not Found` on namespace operations:** EbbMem returns `404` for unauthorized or non-existent namespaces; confirm `X-Ebb-Namespace` matches your credential scope.

## References

- [Host MCP Quickstarts](./README.md)
- [Claude Code Instruction Wrapper](../../../instructions/claude-code/SKILL.md)
- [Agent Instruction Pack](../../../instructions/README.md)
- [Conformance Kit](../../../conformance/README.md)
