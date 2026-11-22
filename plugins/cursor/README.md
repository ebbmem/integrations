# EbbMem Bundle for Cursor

Cursor plugin package and one-click MCP install deeplinks for [EbbMem](https://ebbmem.ai), bundling the remote Streamable HTTP MCP server (`/mcp/core`), the AlwaysApply Cursor rule (`rules/ebbmem.mdc`), and the EbbMem agent skill (`skills/ebbmem-memory/SKILL.md`).

> **Untested live notice & Marketplace status:** Default MCP URLs use placeholder `https://ebbmem.example.com/mcp/core`. Live deeplink install and auth testing against a deployed public HTTPS endpoint are pending as a follow-up. This package has **not** been submitted to the public Cursor Marketplace (external marketplace submission requires explicit approval).

## Package Layout

| Path | Purpose |
| --- | --- |
| `.cursor-plugin/plugin.json` | Cursor plugin manifest (`https://cursor.com/schemas/cursor-plugin/plugin.json`) |
| `.cursor-plugin/marketplace.json` | Local/team marketplace catalog (`https://cursor.com/schemas/cursor-plugin/marketplace.json`) |
| `.mcp.json` | Remote Streamable HTTP MCP definition (`/mcp/core`, header-only auth) |
| `rules/ebbmem.mdc` | AlwaysApply Cursor rule copied from [`instructions/cursor/ebbmem.mdc`](../../instructions/cursor/ebbmem.mdc) |
| `skills/ebbmem-memory/SKILL.md` | Agent skill copied from [`instructions/SKILL.md`](../../instructions/SKILL.md) |

## One-Click MCP Install Deeplinks

Cursor deeplinks use `cursor://anysphere.cursor-deeplink/mcp/install?name=ebbmem&config=<base64-json>`:

- **OAuth 2.1 (browser sign-in via RFC 9728 discovery):**

  ```text
  cursor://anysphere.cursor-deeplink/mcp/install?name=ebbmem&config=eyJ0eXBlIjoiaHR0cCIsInVybCI6Imh0dHBzOi8vZWJibWVtLmV4YW1wbGUuY29tL21jcC9jb3JlIn0%3D
  ```

- **API Key (`X-API-Key` + `X-Ebb-Namespace` via `${env:EBBMEM_API_KEY}`):**

  ```text
  cursor://anysphere.cursor-deeplink/mcp/install?name=ebbmem&config=eyJ0eXBlIjoiaHR0cCIsInVybCI6Imh0dHBzOi8vZWJibWVtLmV4YW1wbGUuY29tL21jcC9jb3JlIiwiaGVhZGVycyI6eyJYLUFQSS1LZXkiOiIke2VudjpFQkJNRU1fQVBJX0tFWX0iLCJYLUViYi1OYW1lc3BhY2UiOiJhY21lOnU6YWxpY2UifX0%3D
  ```

## Manual / Team Installation

1. **Install via team marketplace or local plugin:** Point Cursor's team/local plugin settings to `.cursor-plugin/marketplace.json`, or copy `rules/ebbmem.mdc` to `.cursor/rules/ebbmem.mdc` and `.mcp.json` to `.cursor/mcp.json`.
2. **Configure credentials:** Export `EBBMEM_API_KEY` (if using API-key headers) or remove `headers` from `.mcp.json` to use OAuth 2.1. Never place credentials in URL query parameters.
3. **Verify rule activation:** Open **Cursor Settings -> Rules** and confirm `ebbmem.mdc` is active (`alwaysApply: true`).
