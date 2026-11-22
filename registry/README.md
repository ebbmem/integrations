# Official MCP Registry Manifest (`server.json`)

> **Status: Validated locally only — NOT published.**
> `mcp-publisher login` and `mcp-publisher publish` were **NOT** run. Do **not** run `login` or `publish` until `ebbmem.ai` domain verification is complete, `https://ebbmem.ai/mcp/{profile}` is confirmed live and publicly reachable over HTTPS, and publishing is explicitly approved.

This directory contains [`server.json`](./server.json), the [Official MCP Registry](https://registry.modelcontextprotocol.io) manifest for EbbMem conforming to schema [`2025-12-11`](https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json).

## Current Manifest Scope

- **Namespace (`name`):** `ai.ebbmem/ebbmem` — reverse-DNS namespace verified via `ebbmem.ai` domain ownership (DNS TXT or HTTP `.well-known`).
- **Version (`version`):** `0.1.0` — initial immutable release version.
- **Transport (`remotes`):** Remote Streamable HTTP (`streamable-http`) at `https://ebbmem.ai/mcp/{profile}`, with `profile` selectable across EbbMem's `core` (6 tools, default), `full` (15 tools), and `admin` (18 tools, role-gated) MCP tool profiles.
- **Authentication headers:** Header-only credentials (`X-API-Key` optional when using OAuth 2.1 Bearer auth, plus optional `X-Ebb-Namespace`). Credentials are never passed in URLs.
- **Packages (`packages`):** Intentionally omitted today. No standalone MCP server package (npm, PyPI, Cargo, OCI, or MCPB) is published yet. The packages in `packages/python/`, `packages/typescript/` (`ebbmem-integration-core`), and `packages/ai-sdk/` (`@ebbmem/ai-sdk`) are client and adapter libraries rather than standalone MCP server packages, so none declares `mcpName` and listing them under `packages` would fail registry ownership checks.

## Local Validation (No Login / No Publish)

### 1. Official `mcp-publisher` CLI

Download the release binary from [`modelcontextprotocol/registry` releases](https://github.com/modelcontextprotocol/registry/releases) and run **only** `validate` (ensure `registry/server.json` is UTF-8 without BOM):

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

## Domain Verification Steps for `ebbmem.ai` (Post-Merge, Manual)

The Official MCP Registry verifies control of `ai.ebbmem/*` via an Ed25519 (or ECDSA P-384) key proof published on `ebbmem.ai`. Store the private key securely outside git (never commit `.pem` or `.hex` key files).

### 1. Generate Ed25519 Keypair & Proof Values

Requires **OpenSSL >= 3.0** (on macOS, use `brew install openssl@3` — system LibreSSL lacks Ed25519 `genpkey`) or Python `cryptography`:

```bash
# Generate Ed25519 private key (PEM)
openssl genpkey -algorithm Ed25519 -out ebbmem-mcp-key.pem

# DER SPKI output is 44 bytes: 12-byte ASN.1 header (302a300506032b6570032100) + 32-byte raw public key.
# Strip the 12-byte SPKI prefix so `p=` is the base64-encoded raw 32-byte public key:
PUBLIC_KEY="$(openssl pkey -in ebbmem-mcp-key.pem -pubout -outform DER | tail -c 32 | base64)"
echo "v=MCPv1; k=ed25519; p=${PUBLIC_KEY}"

# Extract the raw 32-byte Ed25519 private seed as 64 hex chars for mcp-publisher.
# NEVER pass a PEM file to --private-key-file or MCP_PRIVATE_KEY:
openssl pkey -in ebbmem-mcp-key.pem -noout -text \
  | sed -n '/priv:/,/pub:/p' | sed '1d;$d' | tr -d ' :\n' > ebbmem-mcp-key.hex
chmod 600 ebbmem-mcp-key.pem ebbmem-mcp-key.hex
```

Equivalent cross-platform Python (`cryptography`) snippet:

```python
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

priv = ed25519.Ed25519PrivateKey.generate()
pub_b64 = base64.b64encode(
    priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
).decode()
seed_hex = priv.private_bytes(
    serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
).hex()
print(f"v=MCPv1; k=ed25519; p={pub_b64}")
# Write seed_hex to ebbmem-mcp-key.hex (64 hex chars) or export MCP_PRIVATE_KEY
```

### 2. Choose One Verification Method on `ebbmem.ai`

#### Option A — DNS TXT (Recommended)

DNS TXT grants `ai.ebbmem/*` and subdomains.

1. Add a TXT record at the **apex** `ebbmem.ai` (`@` — do **not** place it under `_mcp-auth.ebbmem.ai` or `_mcp-registry.ebbmem.ai`):
   ```text
   ebbmem.ai. IN TXT "v=MCPv1; k=ed25519; p=<base64_raw_32_byte_pubkey>"
   ```
2. Verify DNS propagation:
   ```bash
   dig TXT ebbmem.ai +short
   ```
   ```powershell
   Resolve-DnsName -Type TXT ebbmem.ai
   ```
3. Post-merge, only after `https://ebbmem.ai/mcp/{profile}` is live and publishing is approved:
   ```bash
   mcp-publisher login dns --domain ebbmem.ai --private-key-file ebbmem-mcp-key.hex
   # Or via environment variable:
   # MCP_PRIVATE_KEY="$(cat ebbmem-mcp-key.hex)" mcp-publisher login dns --domain ebbmem.ai

   mcp-publisher publish registry/server.json
   ```

#### Option B — HTTP `.well-known`

1. Serve `https://ebbmem.ai/.well-known/mcp-registry-auth` returning:
   - Status: **HTTP `200 OK`** (**no redirects** — `301`/`302`/`3xx` fails immediately)
   - Header: `Content-Type: text/plain`
   - Body (`<= 4096` bytes):
     ```text
     v=MCPv1; k=ed25519; p=<base64_raw_32_byte_pubkey>
     ```
2. If hosted as a static site (for example, Firebase Hosting), ensure hosting `ignore` rules (such as `"**/.*"`) do **not** exclude `.well-known/` (for example, add `"!**/.well-known/**"`).
3. Verify reachability and headers before login:
   ```bash
   curl -i -H 'Accept: text/plain' -A 'mcp-registry/1.0' https://ebbmem.ai/.well-known/mcp-registry-auth
   ```
4. Post-merge, only after `https://ebbmem.ai/mcp/{profile}` is live and publishing is approved:
   ```bash
   mcp-publisher login http --domain ebbmem.ai --private-key-file ebbmem-mcp-key.hex
   mcp-publisher publish registry/server.json
   ```

> **Key rotation:** Remove retired DNS TXT or `.well-known/mcp-registry-auth` proof entries once a key is rotated out.

## Future Package Ownership Verification (`packages`)

If a standalone MCP server artifact is published later and added to `packages` in `server.json`, it must first be published to a supported public registry with a matching ownership marker equal to `server.json`'s `name` (`ai.ebbmem/ebbmem`):

- **npm (`registryType: "npm"`):** Add `"mcpName": "ai.ebbmem/ebbmem"` to the MCP server package's `package.json`.
- **OCI container (`registryType: "oci"`):** Add `LABEL io.modelcontextprotocol.server.name="ai.ebbmem/ebbmem"` to the image's Dockerfile.
- **Cargo (`registryType: "cargo"`):** Include visible markdown text `mcp-name: ai.ebbmem/ebbmem` in the crate's README (crates.io strips HTML comments).
- **PyPI (`registryType: "pypi"`):** Include `<!-- mcp-name: ai.ebbmem/ebbmem -->` (or visible `mcp-name: ai.ebbmem/ebbmem`) in the package README.

## Pre-Publish Checklist (After Explicit Approval)

1. Complete `ebbmem.ai` DNS TXT (`@` apex) or HTTP `.well-known/mcp-registry-auth` verification setup.
2. Confirm `https://ebbmem.ai/mcp/{profile}` (`core`, `full`, `admin`) is live and publicly reachable over HTTPS.
3. Confirm the immutable release `version` (`0.1.0` — published versions in the MCP Registry cannot be overwritten).
4. Re-run `mcp-publisher validate registry/server.json` and JSON Schema validation.
5. Run `mcp-publisher login dns --domain ebbmem.ai --private-key-file ebbmem-mcp-key.hex` (or `login http`) and `mcp-publisher publish registry/server.json`.
