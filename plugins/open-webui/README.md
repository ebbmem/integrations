# EbbMem Filter Function for Open WebUI

Self-contained Open WebUI (`v0.13.0+`) Filter Function connecting chats to [EbbMem](https://ebbmem.ai) for per-user read-only `inlet` recall (injected inside `<ebbmem_untrusted_memory_context>` delimiters) and default-off `outlet` capture and selective post-use reinforcement.

> **Untested live notice:** Default endpoints use placeholder URLs (`https://ebbmem.example.com`). This filter is verified with unit tests and the local EbbMem conformance mock server only; live verification against a deployed Open WebUI instance and public HTTPS endpoint is pending as a follow-up. It has **not** been submitted to the Open WebUI Community Hub.

## What Is Included

| Path | Purpose |
| --- | --- |
| [`ebbmem_filter.py`](./ebbmem_filter.py) | Single-file Open WebUI Filter Function (`Filter`, `Valves`, `UserValves`, pure-stdlib HTTP client with optional `ebbmem_integration_core` delegation) |
| [`README.md`](./README.md) | Installation, `Valves` / `UserValves` reference, safety guarantees, and test instructions |
| [`tests/test_ebbmem_filter.py`](./tests/test_ebbmem_filter.py) | Unit and `MockEbbmemServer` isolation, framing, redaction, reinforcement, and fail-open tests |

## Installation in Open WebUI

1. Open Open WebUI (`v0.13.0` or later) as an administrator and go to **Admin Panel -> Functions -> `+` (Create Function)**.
2. Copy the full contents of [`ebbmem_filter.py`](./ebbmem_filter.py) into the editor and save.
   - The file declares `requirements: pydantic` (already present in Open WebUI) and uses Python standard library HTTP (`urllib.request`) so it does **not** require unpublished PyPI packages at install time.
3. Open the function's **Valves** (gear icon) and configure:
   - `base_url`: your EbbMem server URL (for example `https://ebbmem.example.com`, without credentials in the URL).
   - `api_key` **or** `bearer_token`: set one header credential only (never both).
   - `tenant`: tenant prefix for per-user isolation (for example `acme`, producing namespaces like `acme:u:<user_id>`).
4. Enable the Filter Function globally or assign it to specific models in **Admin Panel -> Models**.

For native `MCP (Streamable HTTP)` or `mcpo` tool-server setup alongside (or instead of) the Filter Function, see [the Open WebUI Integration Quickstart](../../docs/integrations/hosts/open-webui.md).

## Valves & UserValves Reference

### Admin `Valves`

| Valve | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | `bool` | `True` | Master switch for filter execution (`False` passes `body` through unchanged). |
| `base_url` | `str` | `""` | EbbMem server base URL (`http://` or `https://`, no userinfo or query tokens). Empty disables calls. |
| `api_key` | `str` | `""` | API key sent via `X-API-Key` header (mutually exclusive with `bearer_token`). |
| `bearer_token` | `str` | `""` | Bearer/OAuth token sent via `Authorization: Bearer` header (mutually exclusive with `api_key`). |
| `tenant` | `str` | `"default"` | Tenant prefix used to derive `<tenant>:u:<sanitized_user_id>` from `__user__["id"]`. |
| `profile` | `str` | `""` | Optional EbbMem recall profile name. |
| `recall_limit` | `int` | `5` | Maximum memories recalled per user turn (clamped to `1..50`). |
| `min_score` | `float \| None` | `None` | Optional minimum recall score threshold. |
| `tier` | `str` | `"episodic"` | Memory tier used when `auto_remember` is enabled (`working`, `episodic`, `semantic`, `procedural`). |
| `fail_open` | `bool` | `True` | Return `body` untouched when EbbMem is unreachable, times out, or configuration is invalid. |
| `auto_remember` | `bool` | `False` | Automatically store redacted turn summaries in `outlet` (default **OFF**). |
| `auto_reinforce` | `bool` | `True` | Reinforce recalled memories in `outlet` according to the `injected` policy. |
| `injected` | `bool` | `False` | Reinforce all recalled memories even if unused in the assistant reply (default **OFF**). |
| `reinforce_delta` | `float` | `0.1` | Reinforcement delta within `[-1.0, 1.0]`. |
| `redact_before_remember` | `bool` | `True` | Mask API keys, Bearer tokens, JWTs, PEM blocks, and secret assignments before `remember`. |
| `timeout_s` | `float` | `5.0` | HTTP request timeout in seconds. |
| `max_retries` | `int` | `1` | Retry count for idempotent calls only (`recall` and keyed `remember`; never `reinforce`). |
| `telemetry_enabled` | `bool` | `False` | Telemetry flag (default **OFF**; zero telemetry is emitted). |

### Per-User `UserValves`

`UserValves` intentionally exposes **only** safe per-user preferences and excludes `tenant`, `namespace`, `user_id`, `base_url`, credentials, and `telemetry_enabled` so users cannot override namespace boundaries:

| UserValve | Type | Default | Description |
| --- | --- | --- | --- |
| `enabled` | `bool` | `True` | Allows an individual user to pause EbbMem for their chats. |
| `recall_limit` | `int \| None` | `None` | Optional per-user recall limit override (clamped to `1..valves.recall_limit`). |
| `auto_remember` | `bool \| None` | `None` | Allows an individual user to opt out (`False`) when admin `auto_remember` is enabled. |

## Filter Behavior & Safety Guarantees

- **Zero telemetry:** `telemetry_enabled` defaults to `False` and zero outbound telemetry is ever sent; requests go solely to `valves.base_url`.
- **Strict per-user `__user__` isolation:** Namespaces are derived exclusively as `<tenant>:u:<sanitized_user_id>` from `__user__["id"]`. Missing or empty `__user__.id` fails closed (never falling back to a shared default namespace).
- **Header-only auth & URL credential rejection:** Sends `Authorization: Bearer` or `X-API-Key` (never both), and rejects URL userinfo (`user:pass@host`) or credential query parameters (`?token=`, `?api_key=`, etc.) without echoing raw secrets.
- **Read-only `inlet` recall & untrusted framing:** Queries `POST /api/v1/memory/recall` with `reinforce: false` across chats (no `session_id` filter on recall), handles both `str` and multimodal `list` message blocks, and injects memories wrapped in `<ebbmem_untrusted_memory_context>` with escaped attributes and nested closing tags.
- **Default-off `outlet` capture & post-use reinforcement:** `auto_remember` and `injected` are both `False` by default. When `auto_reinforce` is enabled with `injected=False`, only memories explicitly cited by ID or meaningfully used in the assistant reply are reinforced (passing `recall_id` when present); `/api/v1/memory/reinforce` is never retried.
- **Fail-open resilience:** Network errors, timeouts, `5xx` responses, and invalid configuration log redacted warnings and return `body` untouched when `fail_open=True`.

## Running Unit & Mock Conformance Tests

```bash
pytest -v plugins/open-webui/tests
ruff check plugins/open-webui
ruff format --check plugins/open-webui
```
