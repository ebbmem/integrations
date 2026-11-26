# Open WebUI Integration Quickstart

Connect **Open WebUI** (`v0.13.x`) to [EbbMem](https://ebbmem.ai) via the per-user **Filter Function** (`plugins/open-webui/ebbmem_filter.py`), native **MCP (Streamable HTTP)** tool servers, or **`mcpo`** for custom headers and stdio `ebb-mcp` — enabling read-only `recall`, secret-redacted `remember`, and explicit post-use `reinforce`.

> **Untested live notice:** All snippets below use placeholder URLs (`https://ebbmem.example.com`, `http://127.0.0.1:8320`) and are derived from current Open WebUI `v0.13.x` documentation. Live end-to-end verification against a deployed Open WebUI instance and public HTTPS endpoint is pending as a follow-up.

## What You Need

- **EbbMem endpoint:**
  - **Filter Function (REST):** Base server URL (for example `https://ebbmem.example.com` or `http://127.0.0.1:8320`) targeting `/api/v1/memory/{recall,remember,reinforce}`.
  - **MCP (`MCP (Streamable HTTP)`):** `https://ebbmem.example.com/mcp/core` (6 core tools — recommended default) or `/mcp/full` (15 tools, adding `memory_search` / `memory_get` aliases and episodic/association tools).
- **Header-only credentials:** `Authorization: Bearer <token>` or `X-API-Key` (never both, and never embedded in URL query parameters or userinfo).
- **Namespace:** Target namespace `<tenant>:u:<user_id>` (derived automatically from `__user__` by the Filter Function, or supplied via `X-Ebb-Namespace` / `mcpo` for MCP).
- **Open WebUI `v0.13.x` with persistent `WEBUI_SECRET_KEY`:** Set a fixed `WEBUI_SECRET_KEY` environment variable on your Open WebUI container/process so stored External Tool tokens and OAuth credentials remain decryptable across restarts.
- **Optional agent instructions:** Pair MCP tool usage with [`instructions/CORE.md`](../../../instructions/CORE.md) so models respect read-only `recall`, untrusted context framing, and selective `reinforce`.

## Config Snippet

Choose the integration path that fits your deployment. You can also combine **Option A (Filter Function)** for automatic per-user recall/reinforcement with **Option B** or **Option C** for explicit memory tools.

### Option A: EbbMem Filter Function (`plugins/open-webui/ebbmem_filter.py`)

The Filter Function runs in Python inside Open WebUI with zero external PyPI dependencies beyond `pydantic` (already bundled with Open WebUI) and enforces strict per-user isolation:

1. In Open WebUI, open **Admin Panel -> Functions -> `+` (Create Function)**.
2. Paste [`plugins/open-webui/ebbmem_filter.py`](../../../plugins/open-webui/ebbmem_filter.py) and click **Save**.
3. Open **Valves** (gear icon) and set:
   - `base_url`: `https://ebbmem.example.com`
   - `api_key` **or** `bearer_token`: set one credential only (never both)
   - `tenant`: e.g. `acme` (derives `<tenant>:u:<sanitized_user_id>` strictly from `__user__["id"]`)
4. Leave safety defaults as-is unless your deployment requires opting in:
   - `auto_remember = False` (OFF by default)
   - `injected = False` (reinforce only memories actually cited or used in the response)
   - `telemetry_enabled = False` (zero telemetry emitted)
   - `redact_before_remember = True`
   - `fail_open = True`
5. Enable the Filter globally or attach it to specific models in **Admin Panel -> Models**.

See [`plugins/open-webui/README.md`](../../../plugins/open-webui/README.md) for the complete `Valves` and `UserValves` reference.

### Option B: Native MCP (`MCP (Streamable HTTP)`)

Open WebUI `v0.13.x` supports native Streamable HTTP MCP servers configured by an administrator:

1. Ensure `WEBUI_SECRET_KEY` is set to a persistent secret in Open WebUI's environment before saving credentials.
2. Open **Admin Panel -> Settings -> External Tools -> `+ Add Connection`**.
3. Set **Type** to **`MCP (Streamable HTTP)`** (**never** select `OpenAPI` for an EbbMem `/mcp/*` endpoint).
4. Set **URL** to `https://ebbmem.example.com/mcp/core` (or `/mcp/full`).
5. Under **Auth**, select **Bearer** and paste your token (or configure **OAuth 2.1** if your EbbMem deployment advertises OAuth metadata).
6. Save and verify the EbbMem tools appear in the model tool picker.

> **Bearer-only UI limitation:** Open WebUI's native `MCP (Streamable HTTP)` connection form only exposes a `Bearer` token (or OAuth 2.1) auth field for static credentials and does not provide arbitrary custom static header fields. If you need `X-API-Key` + `X-Ebb-Namespace` headers without OAuth/Bearer or want to expose local stdio `ebb-mcp`, use **Option C (`mcpo`)** below.

### Option C: `mcpo` Proxy (Custom `X-Ebb-Namespace` / `X-API-Key` Headers or Stdio `ebb-mcp`)

[`mcpo`](https://github.com/open-webui/mcpo) exposes MCP servers (remote Streamable HTTP with custom headers or local stdio `ebb-mcp`) as an OpenAPI tool server that Open WebUI consumes natively.

Create a `config.json` file:

```json
{
  "mcpServers": {
    "ebbmem-remote": {
      "type": "streamable-http",
      "url": "https://ebbmem.example.com/mcp/core",
      "headers": {
        "Authorization": "Bearer ${EBBMEM_API_KEY}",
        "X-Ebb-Namespace": "acme:u:alice"
      }
    },
    "ebbmem-local": {
      "command": "ebb-mcp",
      "args": ["--profile", "core"],
      "env": {
        "EBBMEM_URL": "http://127.0.0.1:8320",
        "EBBMEM_API_KEY": "${EBBMEM_API_KEY}",
        "EBBMEM_NAMESPACE": "acme:u:alice"
      }
    }
  }
}
```

Start `mcpo` with a local proxy API key and register `http://127.0.0.1:8000/ebbmem-remote` (or `/ebbmem-local`) as an **OpenAPI** connection in **Admin Panel -> Settings -> External Tools**:

```bash
mcpo --port 8000 --config config.json --api-key "replace-with-local-proxy-key"
```

*(Keep `mcpo` bound to localhost or behind authenticated reverse-proxy rules, and remember that `mcpo` uses a static `X-Ebb-Namespace` per configured route rather than dynamic `__user__` switching.)*

## First Memory in Under Five Steps

1. **Connect Open WebUI to EbbMem:** Install the Filter Function (Option A), register `MCP (Streamable HTTP)` (Option B), or start `mcpo` (Option C).
2. **Store a durable preference:** With `auto_remember=True` (or via MCP `memory_remember`), save a user preference such as `tier: "semantic"` without storing raw secrets or API keys.
3. **Verify per-user isolation & read-only recall:** Open a new chat as the same user and ask about that preference. Confirm recall injects `<ebbmem_untrusted_memory_context>` without modifying `storage_strength`, and verify a second Open WebUI user (`__user__["id"]`) cannot recall the first user's memory.
4. **Verify selective reinforcement (`injected=False`):** Confirm `outlet` (or `memory_reinforce`) passes `memory_id`, `used_ids`, and `recall_id` only for memories actually cited or used in the response while `injected` remains OFF.
5. **Confirm fail-open behavior:** Temporarily point `base_url` to an unreachable local port (with `fail_open=True`) and confirm Open WebUI chats still complete normally without throwing errors.

## Troubleshooting

- **`400 Bad Request` mentioning URL credentials:** Move any `token`, `api_key`, or `user:pass@` out of the URL into `Valves.api_key` / `Valves.bearer_token` (Option A), the native Bearer field (Option B), or `mcpo` `headers` (Option C).
- **UI hangs or errors when adding a native MCP server:** Confirm **Type** is set to **`MCP (Streamable HTTP)`**, not `OpenAPI`. Use `OpenAPI` only when connecting through `mcpo`.
- **`Error decrypting tokens` after Open WebUI restart:** Set a persistent `WEBUI_SECRET_KEY` environment variable on Open WebUI and re-save your External Tool credentials.
- **Need `X-Ebb-Namespace` or `X-API-Key` headers with MCP:** Native `MCP (Streamable HTTP)` UI supports Bearer/OAuth only; use **Option C (`mcpo`)** for custom static headers or **Option A (Filter Function)** for automatic per-user `__user__` isolation.
- **`404 Not Found` on namespace access or memories fading despite recalls:** EbbMem returns `404` for unauthorized/missing namespaces; check your `tenant` / `X-Ebb-Namespace` scope. `recall` is strictly read-only — verify `auto_reinforce=True` (Filter) or explicit `memory_reinforce` calls (MCP) are reinforcing used memories while `injected` stays OFF.

## References

- [Host MCP Quickstarts](./README.md)
- [Open WebUI Filter Function README](../../../plugins/open-webui/README.md)
- [Agent Instruction Pack](../../../instructions/README.md)
- [Conformance Kit](../../../conformance/README.md)
