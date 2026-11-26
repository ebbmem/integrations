# EbbMem Conformance Kit (v1)

Language-neutral IC-1 conformance suite, Apache-2.0 stateful reference mock server, MCP v2 surface tests, TypeScript conformance runner, deterministic adapter badge rules, and local compatibility table generator for [EbbMem](https://ebbmem.ai) integrations.

## Components

- `scenarios/`: 21 YAML scenarios mapping 1-to-1 to the IC-1 v0.1 `[ic1.*]` conformance identifiers.
- `golden/mcp_surface_v2.json`: Public structural descriptor for MCP v2 protocol revisions (`2026-07-28` and legacy), tool profiles (`core`, `full`, `admin`), universal aliases (`memory_search`, `memory_get`), tool annotations, and `outputSchema` definitions.
- `src/ebbmem_conformance/`:
  - `mock_server.py`: Stateful `ThreadingHTTPServer` implementing IC-1 REST + MCP v2 wire semantics.
  - `client.py`: Standard-library HTTP and MCP JSON-RPC reference client demonstrating header-only auth, URL-credential refusal, secret redaction, fail-open recall, idempotent-only retries, untrusted recall framing, and explicit-use reinforcement (`injected` OFF by default).
  - `runner.py`: Python YAML scenario loader and deterministic assertion engine.
  - `cli.py`: `ebbmem-conformance` CLI (`--mode mock` and stubbed `--mode live`).
- `ts/` (`ebbmem-conformance-ts`):
  - TypeScript runner (`src/runner.ts`) with full parity across all 21 YAML scenarios, 10 step actions, `${var}` interpolation, Ajv Draft-07 `outputSchema` validation, and `ebbmem-integration-core` `client_check` verification.
  - `MockServerBridge` (`src/bridge.ts`) spawning `MockEbbmemServer` over stdio (honoring `PYTHON` env override, `.venv`, or `python`/`python3` >= 3.11).
  - Deterministic adapter badge evaluator (`src/badges.ts`) and CLI (`src/cli.ts`).
- `scripts/`:
  - `compat_manifest.json`: Machine-readable target coverage and conformance evidence manifest.
  - `generate_compat_table.py`: Stdlib-only Python generator and `--check` drift verifier for `docs/integrations/README.md`.

## Usage

### Python Conformance Suite

```bash
pip install -e "./conformance[dev]"
ebbmem-conformance --mode mock
ebbmem-conformance --mode live --base-url https://example.invalid  # exits 2 (stubbed in Python v0)
pytest -v conformance/tests
ruff check conformance && ruff format --check conformance
```

### TypeScript Conformance Runner (`conformance/ts`)

Build `packages/typescript` first so the local `ebbmem-integration-core` link resolves:

```bash
npm --prefix packages/typescript install --no-package-lock
npm --prefix packages/typescript run build

cd conformance/ts
npm install --no-package-lock
npm run typecheck
npm run build
npm test
```

Run the TypeScript CLI directly:

```bash
# Run all 21 scenarios against MockServerBridge
node conformance/ts/dist/cli.js --mode mock --json-report /path/to/report.json

# Run a single scenario
node conformance/ts/dist/cli.js --mode mock --scenario ic1.mcp.golden_schemas

# Run live wire-compatible scenarios against a target server
# (scenarios requiring mock server control hooks are skipped cleanly)
EBBMEM_LIVE_ALICE_KEY=... node conformance/ts/dist/cli.js --mode live --base-url https://your-ebbmem.example

# Evaluate adapter badges from compat_manifest.json
node conformance/ts/dist/cli.js --badges-json /path/to/badges.json
```

If your Python 3.11+ interpreter is not the default `python` on `PATH`, set `PYTHON=/path/to/python` before running `npm test` or `node dist/cli.js --mode mock`.

## Adapter Badge Rules

`conformance/ts/src/badges.ts` and `conformance/scripts/generate_compat_table.py` evaluate four ordered tiers (`gold` > `silver` > `bronze` > `unverified`) from `compat_manifest.json` evidence.

### Mandatory Security & Cognitive Safety Gate (Fail-Closed -> `unverified`)

Any target is forced to `unverified` if **any** of the following fails:

- `status !== "available"` (`"in progress"` or `"planned"` targets are always `unverified`).
- `headerOnlyAuth !== true` (URL query/userinfo credentials forbidden).
- `telemetryDisabledDefault !== true`.
- For `framework-adapter` and `host-plugin` targets: `injectedDefaultOff !== true`, `readOnlyRecall !== true`, `untrustedFraming !== true`, or `secretRedaction !== true`.
- `mockResults.failed > 0` or `liveResults.failed > 0`.

### Tier Assignment (When Safety Gate Passes)

- **`gold`**: `mockResults.passed >= mockResults.required > 0`, `liveResults.passed >= liveResults.required > 0`, and `mcpSurfaceValidated === true` (when `mcpSupported === true`).
- **`silver`**: `mockResults.passed >= mockResults.required > 0` (live suite untested/partial), and for `framework-adapter` targets all 5 IC-1 scenario categories are covered (`scoping_isolation`, `cognitive_lifecycle`, `resilience`, `security`, `closure_and_mcp`).
- **`bronze`**: `status === "available"`, safety gate passed, and at least one of `mcpSurfaceValidated === true`, `configSchemaValidated === true`, or `mockResults.passed > 0`.
- **`unverified`**: Fallback when none of `gold`/`silver`/`bronze` criteria are satisfied.

## Compatibility Table Generator

Regenerate the matrix inside `docs/integrations/README.md` (between `<!-- BEGIN_COMPAT_TABLE -->` and `<!-- END_COMPAT_TABLE -->`) or check for drift:

```bash
python conformance/scripts/generate_compat_table.py --write
python conformance/scripts/generate_compat_table.py --check
```

`--check` exits `0` when `docs/integrations/README.md` matches `conformance/scripts/compat_manifest.json` and exits `1` with a unified diff on drift.

## Nightly CI Follow-up Design

> Note: `.github/workflows/` is **not** modified in this PR. The workflow design below documents the planned follow-up automation for `ebbmem/integrations`.

1. **Schedule & Triggers**:
   - Scheduled nightly run (`cron: "0 4 * * *"` UTC) plus manual `workflow_dispatch`.
   - Optional pull-request path check on `conformance/**` and `docs/integrations/README.md` running `generate_compat_table.py --check`.
2. **Approved Infrastructure & Cost Policy**:
   - Runs exclusively on standard GitHub-hosted `ubuntu-latest` runners for the public `ebbmem/integrations` repository (**$0/month** runner cost).
   - Zero paid external LLM API keys, zero LLM-as-judge calls, zero cloud agents, and zero external SaaS or Docker services; all adapter tests execute deterministically in-process with mock models against `MockEbbmemServer`.
3. **Headless Conformance & Latest-Release Matrix**:
   - **Core conformance**: Python (`pytest -v conformance/tests`, `ebbmem-conformance --mode mock`) and TypeScript (`conformance/ts`: `npm run typecheck && npm run build && npm test`, `node dist/cli.js --mode mock`).
   - **Latest-release headless framework matrix**: Freshly installs latest upstream releases of `langgraph`/`langchain`/`langmem`, `openai-agents`, and `ai`/`zod` in isolated jobs and executes the `packages/python`, `packages/typescript`, `packages/langgraph-python`, `packages/openai-agents-python`, and `packages/ai-sdk` mock conformance suites.
   - **Headless host & config checks**: `plugins/claude-code` hook tests, `plugins/cursor` manifest/MCP checks, `configs/copilot` & `registry/server.json` JSON Schema validation, plus OpenClaw and Open WebUI headless test suites once merged.
4. **Deliberate-Break Negative Verification**:
   - A dedicated step executes controlled negative scenarios (assertion mismatch, broken `storage_strength` recall invariant, invalid `outputSchema`, and URL credential acceptance) and asserts the conformance runner exits **non-zero** so silent harness passes fail the build.
5. **Compatibility Table Drift Gate**:
   - Runs `python conformance/scripts/generate_compat_table.py --check` to ensure `docs/integrations/README.md` never drifts from `conformance/scripts/compat_manifest.json`.
