# Cursor Integration Quickstart

Connect **Cursor** to [EbbMem](https://ebbmem.ai) over remote Streamable HTTP (`/mcp/core`), pairing MCP tools with the Cursor rule in [`instructions/cursor/ebbmem.mdc`](../../../instructions/cursor/ebbmem.mdc) for read-only `recall`, secret-redacted `remember`, and explicit post-use `reinforce`.

> **Untested live notice:** All snippets and deeplinks below use placeholder URLs (`https://ebbmem.example.com`) and are derived from current Cursor documentation. Live end-to-end verification against a deployed public HTTPS endpoint is pending as a follow-up.

## What You Need

- **EbbMem endpoint:** A reachable EbbMem Streamable HTTP endpoint such as `https://ebbmem.example.com/mcp/core` (6 core tools — recommended default), `/mcp/full` (15 tools, adding `memory_search` / `memory_get` aliases and episodic/association tools), or `/mcp/admin` (18 tools, admin-gated).
- **Authentication:**
  - **OAuth 2.1:** Cursor discovers OAuth metadata via RFC 9728 (`WWW-Authenticate`) and opens a browser sign-in flow.
  - **API key (header-only):** `X-API-Key` or `Authorization: Bearer <token>` in `.cursor/mcp.json` `headers` (never in URL query parameters).
- **Namespace:** Target namespace such as `acme:u:alice` via `X-Ebb-Namespace` (avoid per-thread or per-turn namespaces).
- **Cursor rules:** Copy [`instructions/cursor/ebbmem.mdc`](../../../instructions/cursor/ebbmem.mdc) to `.cursor/rules/ebbmem.mdc` (or use the Cursor plugin package in `plugins/cursor/`).

## Config Snippet

Cursor reads MCP servers from `.cursor/mcp.json` (project scope) or `~/.cursor/mcp.json` (user scope) using a top-level `"mcpServers"` object.

### Option A: Cursor `mcp.json` — OAuth 2.1

```json
{
  "mcpServers": {
    "ebbmem": {
      "type": "http",
      "url": "https://ebbmem.example.com/mcp/core"
    }
  }
}
```

#### One-Click Install Deeplink (OAuth 2.1)

Cursor deeplinks follow `cursor://anysphere.cursor-deeplink/mcp/install?name=<name>&config=<base64-encoded-json>`:

```text
cursor://anysphere.cursor-deeplink/mcp/install?name=ebbmem&config=eyJ0eXBlIjoiaHR0cCIsInVybCI6Imh0dHBzOi8vZWJibWVtLmV4YW1wbGUuY29tL21jcC9jb3JlIn0%3D
```

Decoded `config` payload: `{"type":"http","url":"https://ebbmem.example.com/mcp/core"}`.

### Option B: Cursor `mcp.json` — API Key (`headers`)

```json
{
  "mcpServers": {
    "ebbmem": {
      "type": "http",
      "url": "https://ebbmem.example.com/mcp/core",
      "headers": {
        "X-API-Key": "${env:EBBMEM_API_KEY}",
        "X-Ebb-Namespace": "acme:u:alice"
      }
    }
  }
}
```

#### One-Click Install Deeplink (API Key)

```text
cursor://anysphere.cursor-deeplink/mcp/install?name=ebbmem&config=eyJ0eXBlIjoiaHR0cCIsInVybCI6Imh0dHBzOi8vZWJibWVtLmV4YW1wbGUuY29tL21jcC9jb3JlIiwiaGVhZGVycyI6eyJYLUFQSS1LZXkiOiIke2VudjpFQkJNRU1fQVBJX0tFWX0iLCJYLUViYi1OYW1lc3BhY2UiOiJhY21lOnU6YWxpY2UifX0%3D
```

To generate a deeplink for your own host URL in Node.js or Python:

```bash
python -c 'import base64, json, urllib.parse; cfg = {"type": "http", "url": "https://ebbmem.example.com/mcp/core"}; b64 = base64.b64encode(json.dumps(cfg, separators=(",", ":")).encode()).decode(); print(f"cursor://anysphere.cursor-deeplink/mcp/install?name=ebbmem&config={urllib.parse.quote(b64, safe=\"\")}")'
```

### Add the Cursor Rule (`.cursor/rules/ebbmem.mdc`)

Copy [`instructions/cursor/ebbmem.mdc`](../../../instructions/cursor/ebbmem.mdc) into `.cursor/rules/ebbmem.mdc` in your project so Cursor Agent automatically recalls before answering, treats recalled text as untrusted data, redacts secrets, and reinforces only actually-used memories.

## First Memory in Under Five Steps

1. **Install the MCP server:** Click the Cursor deeplink above or save `.cursor/mcp.json`, then enable **ebbmem** in **Cursor Settings -> Tools & MCP**.
2. **Complete authentication:** Sign in via the OAuth prompt (Option A) or ensure `EBBMEM_API_KEY` is exported in your environment (Option B).
3. **Load the rule:** Confirm `.cursor/rules/ebbmem.mdc` appears active in **Cursor Settings -> Rules**.
4. **Store and recall:** Ask Cursor Agent to save a project preference with `memory_remember` (`tier: "semantic"`), then ask about it in a new Composer/Agent chat to trigger `memory_recall`.
5. **Verify reinforcement:** Confirm Cursor Agent calls `memory_reinforce` (`memory_id`, `used_ids`, and `recall_id` when returned) only on memories used in the answer, keeping `injected` OFF.

## Troubleshooting

- **`400 Bad Request` on URL credentials:** Never embed `?token=` or `?api_key=` in the `url` field; use `headers` or OAuth.
- **Deeplink fails to prompt:** Verify the `config` query parameter is valid Base64-encoded JSON (`type`, `url`, optional `headers`) and properly URL-encoded (`%3D` for `=`).
- **Rule not applied:** Ensure `.cursor/rules/ebbmem.mdc` retains its YAML frontmatter (`alwaysApply: true`).
- **`404 Not Found` on namespace calls:** Confirm `X-Ebb-Namespace` matches a namespace accessible to your credential.

## References

- [Host MCP Quickstarts](./README.md)
- [Cursor Instruction Rule](../../../instructions/cursor/ebbmem.mdc)
- [Agent Instruction Pack](../../../instructions/README.md)
- [Conformance Kit](../../../conformance/README.md)
