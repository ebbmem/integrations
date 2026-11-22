# Official MCP Registry Manifest (`server.json`)

> **Status: Validated locally only — NOT published.**
> Do not run `mcp-publisher login` or `mcp-publisher publish` until publishing and namespace verification are explicitly approved and the placeholder endpoint URL below is replaced with a live public HTTPS endpoint.

This directory contains [`server.json`](./server.json), the [Official MCP Registry](https://registry.modelcontextprotocol.io) manifest for EbbMem conforming to schema [`2025-12-11`](https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json).

## Current Manifest Scope

- **Namespace (`name`):** `io.github.ebbmem/ebbmem` — uses the GitHub organization namespace for [`github.com/ebbmem/integrations`](https://github.com/ebbmem/integrations). See [Namespace Options](#namespace-options) below if a custom domain namespace (`ai.ebbmem/ebbmem`) is preferred instead.
- **Transport (`remotes`):** Remote Streamable HTTP (`streamable-http`) pointing to a **clearly marked RFC 2606 placeholder URL** (`https://ebbmem.example.com/mcp/{profile}`), with `profile` selectable across EbbMem's `core` (6 tools, default), `full` (15 tools), and `admin` (18 tools, role-gated) MCP tool profiles.
- **Authentication headers:** Header-only credentials (`X-API-Key` optional when using OAuth 2.1 Bearer auth, plus optional `X-Ebb-Namespace`). Credentials are never passed in URLs.
- **Packages (`packages`):** Intentionally omitted today. No standalone MCP server package (npm, PyPI, Cargo, OCI, or MCPB) is published yet. The packages in `packages/python/` and `packages/typescript/` (`ebbmem-integration-core`) are shared integration libraries rather than MCP server artifacts, so listing them under `packages` would be inaccurate and would fail registry ownership checks.

## Local Validation (No Login / No Publish)

### 1. Official `mcp-publisher` CLI

Download the latest release binary from [`modelcontextprotocol/registry` releases](https://github.com/modelcontextprotocol/registry/releases) and run **only** `validate`:

```bash
mcp-publisher validate registry/server.json
```

`mcp-publisher validate` checks JSON syntax, schema version (`2025-12-11`), JSON Schema compliance, and semantic rules without authenticating or contacting the registry publish API.

### 2. JSON Schema Validation

Validate `registry/server.json` directly against the published Draft-07 schema (`https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json`), for example using `check-jsonschema` or Python `jsonschema`:

```bash
check-jsonschema \
  --schemafile https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json \
  registry/server.json
```

## Namespace Options

The Official MCP Registry verifies control of the **namespace** in `name` (and ownership of any listed `packages`), not control of the remote URL.

| Option | `name` Value | Verification Method | Notes |
| --- | --- | --- | --- |
| **GitHub org (current default)** | `io.github.ebbmem/ebbmem` | GitHub Actions OIDC (`mcp-publisher login github-oidc`) or org-owner PAT (`mcp-publisher login github --token <PAT>`) | Interactive `mcp-publisher login github` device flow does not currently grant GitHub organization namespaces (upstream issue [#1468](https://github.com/modelcontextprotocol/registry/issues/1468)); use GitHub Actions OIDC from `ebbmem/integrations` or a PAT from an `ebbmem` org Owner with `read:org` (classic) or `Organization permissions -> Members -> Read-only` (fine-grained). No repository scopes are needed. |
| **Domain (alternative)** | `ai.ebbmem/ebbmem` | DNS TXT at apex `ebbmem.ai` (`mcp-publisher login dns --domain ebbmem.ai`) or HTTP well-known file at `https://ebbmem.ai/.well-known/mcp-registry-auth` (`mcp-publisher login http --domain ebbmem.ai`) | Recommended by the MCP Registry docs when the remote URL is hosted on `ebbmem.ai`, so the server name and remote endpoint visibly share the same verified domain. Note: DNS TXT records must be placed on the apex (`ebbmem.ai`), not a `_mcp-auth` selector; HTTP well-known must return `200 OK` (`text/plain`, no redirects, <= 4096 bytes). |

## Future Package Ownership Verification (`packages`)

If a standalone MCP server artifact is published later and added to `packages` in `server.json`, it must first be published to a supported public registry with a matching ownership marker equal to `server.json`'s `name` (currently `io.github.ebbmem/ebbmem`, or `ai.ebbmem/ebbmem` if switched to domain verification):

- **npm (`registryType: "npm"`):** Add `"mcpName": "io.github.ebbmem/ebbmem"` to the MCP server package's `package.json`.
- **OCI container (`registryType: "oci"`):** Add `LABEL io.modelcontextprotocol.server.name="io.github.ebbmem/ebbmem"` to the image's Dockerfile.
- **Cargo (`registryType: "cargo"`):** Include visible markdown text `mcp-name: io.github.ebbmem/ebbmem` in the crate's README (crates.io strips HTML comments).
- **PyPI (`registryType: "pypi"`):** Include `<!-- mcp-name: io.github.ebbmem/ebbmem -->` (or visible `mcp-name: ...`) in the package README.

## Pre-Publish Checklist (After Explicit Approval)

1. Confirm final namespace choice (`io.github.ebbmem/ebbmem` vs `ai.ebbmem/ebbmem`) and complete namespace authentication setup.
2. Replace the placeholder remote URL `https://ebbmem.example.com/mcp/{profile}` with the live, publicly accessible HTTPS MCP endpoint.
3. Confirm the release `version` (published versions are immutable in the MCP Registry and cannot be overwritten).
4. Re-run `mcp-publisher validate registry/server.json` and JSON Schema validation.
5. Authenticate with the approved method and publish:

```bash
# Example (after approval and endpoint replacement only):
# Option A (GitHub org via OIDC in GitHub Actions):
mcp-publisher login github-oidc
# Option B (GitHub org via org-owner PAT with read:org / Members:Read):
# mcp-publisher login github --token "$GITHUB_ORG_READ_TOKEN"
# Option C (if switched to ai.ebbmem/ebbmem domain namespace):
# MCP_PRIVATE_KEY="<hex-ed25519-key>" mcp-publisher login dns --domain ebbmem.ai

mcp-publisher publish registry/server.json
```
