# EbbMem Conformance Kit (v0)

Language-neutral IC-1 conformance suite, fresh Apache-2.0 stateful reference mock server, and MCP v2 surface tests for [EbbMem](https://ebbmem.ai) integrations.

## Components

- `scenarios/`: 21 YAML scenarios mapping 1-to-1 to the IC-1 v0.1 `[ic1.*]` conformance identifiers.
- `golden/mcp_surface_v2.json`: Public structural descriptor for MCP v2 protocol revisions (`2026-07-28` and legacy), tool profiles (`core`, `full`, `admin`), universal aliases (`memory_search`, `memory_get`), tool annotations, and `outputSchema` definitions.
- `src/ebbmem_conformance/`:
  - `mock_server.py`: Stateful `ThreadingHTTPServer` implementing IC-1 REST + MCP v2 wire semantics.
  - `client.py`: Standard-library HTTP and MCP JSON-RPC client demonstrating header-only auth, URL-credential refusal, secret redaction, fail-open recall, idempotent-only retries, untrusted recall framing, and explicit-use reinforcement (`injected` OFF by default).
  - `runner.py`: YAML scenario loader and deterministic assertion engine.
  - `cli.py`: `ebbmem-conformance` CLI (`--mode mock` and stubbed `--mode live`).

## Usage

```bash
pip install -e "./conformance[dev]"
ebbmem-conformance --mode mock
ebbmem-conformance --mode live --base-url https://example.invalid  # exits 2 (stubbed in v0)
pytest -v conformance/tests
```
