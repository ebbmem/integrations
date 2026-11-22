# `<Target Name>` Integration Quickstart

> **Template usage:** Copy this file when creating a new host guide or recipe. Keep placeholders generic (for example `https://your-ebbmem-host.example.invalid`, `127.0.0.1`, `ebb_your_api_key`), never place tokens in URL query parameters, preserve read-only `recall` + explicit `reinforce` semantics (`injected` OFF by default), and clearly label any snippet that has not been verified live.

One-sentence summary of how `<Target Name>` connects to [EbbMem](https://ebbmem.ai) (for example via Streamable HTTP `/mcp/core`, local stdio `ebb-mcp`, or a native adapter package) and what memory capabilities it enables.

## What You Need

- **EbbMem endpoint:** A reachable EbbMem server URL (such as `https://your-ebbmem-host.example.invalid/mcp/core` for Streamable HTTP, or `http://127.0.0.1:8320/mcp/core` for local self-hosting) or a local `ebb-mcp` stdio setup.
- **Header credential:** An API key (`X-API-Key` or `Authorization: Bearer <token>`) or OAuth 2.1 bearer token. Never pass credentials in URL query strings.
- **Namespace:** A target namespace (such as `<tenant>:u:<user_id>` via `X-Ebb-Namespace` or adapter `ScopeResolver` / `ScopingResolver`), avoiding per-thread or per-turn namespaces.
- **Target environment:** `<Target Name>` `<version or requirement>`.
- **Optional agent instructions:** Copy the matching wrapper from [`instructions/README.md`](../../instructions/README.md) (starting from [`instructions/CORE.md`](../../instructions/CORE.md)) so the agent knows when to recall, remember, and reinforce.

## Config Snippet

Replace the placeholder host, namespace, and header credential with your own values.

### MCP Configuration (if applicable)

```json
{
  "mcpServers": {
    "ebbmem": {
      "type": "http",
      "url": "https://your-ebbmem-host.example.invalid/mcp/core",
      "headers": {
        "Authorization": "Bearer ${EBBMEM_API_KEY}",
        "X-Ebb-Namespace": "acme:u:alice"
      }
    }
  }
}
```

### Native Adapter Configuration (if applicable)

```text
# Replace with the target package install command and minimal initialization snippet.
# Ensure fail_open stays true, telemetry stays off by default, and recall does not reinforce.
```

## First Memory in Under Five Steps

1. **Connect `<Target Name>` to EbbMem:** Apply the config snippet above (using `/mcp/core` for the six core tools, or `/mcp/full` if you also need `memory_search` / `memory_get` aliases and episodic/association tools).
2. **Store a durable memory:** Ask the agent (or call `memory_remember`) to save a preference or project fact with `tier: "semantic"` (and a deterministic `idempotency_key` on retries). Never store raw secrets, API keys, or passwords.
3. **Recall before answering:** Start a new turn or prompt and verify `memory_recall` (or `memory_search`) retrieves the stored memory inside `<ebbmem_untrusted_memory_context>` framing without modifying its storage strength.
4. **Reinforce only what was used:** After the agent uses the recalled memory in its response or action, verify `memory_reinforce` is called with the `memory_id`, `used_ids`, and `recall_id` (when returned), keeping `injected` (reinforce-all-shown) OFF.

## Troubleshooting

- **`400 Bad Request` mentioning URL credentials:** Move any `token` or `api_key` out of the URL query string or userinfo into the `Authorization: Bearer` or `X-API-Key` header.
- **`400 Bad Request` on profile or namespace conflict:** Do not send a `X-Ebb-MCP-Profile` header that disagrees with `/mcp/<profile>`, and ensure `X-Ebb-Namespace` matches any `namespace` parameter in the request body.
- **`403 Forbidden` / JSON-RPC `-32003` on admin tools:** `memory_dream`, `memory_reflect`, and `memory_purge` require an admin-scoped credential on `/mcp/admin`. Use `/mcp/core` for normal agent workflows.
- **`404 Not Found` on namespace access:** EbbMem returns `404` for unauthorized or non-existent namespaces. Confirm the credential has access to the configured `X-Ebb-Namespace`.
- **Memories fade even after retrieval:** `recall` is strictly read-only. Confirm your workflow or agent instruction pack invokes `memory_reinforce` on memories that were actually used.

## References

- [Integrations Overview & Coverage Matrix](./README.md)
- [Agent Instruction Pack](../../instructions/README.md)
- [Conformance Kit](../../conformance/README.md)
