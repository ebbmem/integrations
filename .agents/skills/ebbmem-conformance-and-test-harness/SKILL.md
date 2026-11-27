---
name: ebbmem-conformance-and-test-harness
description: >-
  Step-by-step build, test, and pre-PR verification runbook for
  ebbmem/integrations across Python packages, TypeScript packages,
  MockServerBridge, IC-1 YAML conformance runners (Python and conformance/ts),
  deliberate-break negative testing, and compat_manifest.json table drift
  checks. Use when running tests, debugging build/import failures, adding
  conformance scenarios, updating compatibility badges, or verifying a PR.
---

# EbbMem Conformance & Multi-Package Test Harness Runbook

Use this skill to build, test, and verify changes across Python and TypeScript packages in `ebbmem/integrations` without hitting common monorepo harness pitfalls.

## 1. TypeScript Monorepo Build Order (`file:` Local Links)

`packages/ai-sdk`, `plugins/openclaw`, and `conformance/ts` depend on `"ebbmem-integration-core": "file:..."` pointing to `packages/typescript/`, whose compiled entry point lives in gitignored `packages/typescript/dist/index.js`.

1. **Always build `packages/typescript` first** before installing or testing any downstream TypeScript package (otherwise Node fails with `ERR_MODULE_NOT_FOUND` on `ebbmem-integration-core/dist/index.js`):
   ```bash
   cd packages/typescript
   npm install --no-package-lock
   npm run typecheck
   npm run build
   npm test
   ```
2. **Then install and test downstream TS packages** (always passing `--no-package-lock` to match `.npmrc`):
   ```bash
   cd <packages/ai-sdk | plugins/openclaw | conformance/ts>
   npm install --no-package-lock
   npm run typecheck
   npm run build
   npm test
   ```
3. **Node version requirements**: `packages/typescript` requires Node `>=20`; `packages/ai-sdk` and `plugins/openclaw` require Node `>=22`.

## 2. `MockServerBridge` & `PYTHONPATH`-Free Isolation

TypeScript IC-1 conformance suites spawn Python's `MockEbbmemServer` over stdin/stdout JSON-RPC via `MockServerBridge`.

1. **Explicit Python interpreter override**: `resolvePythonExecutable()` checks `PYTHON`, then repo `.venv`, then `python`/`python3` (`>=3.11`). If your default `python` lacks `pyyaml`/`jsonschema` or is not the project virtualenv, export `PYTHON` before running TS tests or CLI:
   ```bash
   # POSIX
   PYTHON=/path/to/.venv/bin/python npm test
   # PowerShell
   $env:PYTHON = "path\to\.venv\Scripts\python.exe"; npm test
   ```
2. **Verify `PYTHONPATH` independence**: `findConformanceSrc()` locates `conformance/src/ebbmem_conformance/mock_server.py` from `process.cwd()` even when tests are compiled into a temporary `--outDir`. When auditing `MockServerBridge`, verify tests pass with `PYTHONPATH` unset.

## 3. Python Venvs, Ruff & Dual-Mode Tests

1. **Editable installs**: Avoid `--no-build-isolation` in fresh virtualenvs lacking `setuptools` (fails with `Cannot import build backend 'setuptools.build_meta'`). Standard editable install:
   ```bash
   pip install -e "./conformance[dev]" -e "./packages/python[dev]"
   # Add optional adapter packages as needed:
   pip install -e "./packages/langgraph-python[dev]" -e "./packages/openai-agents-python[dev]"
   ```
2. **Ruff consistency**: All Python packages configure `line-length = 100`, `target-version = "py311"`, `select = ["E", "F", "I", "UP", "B"]`. For standalone plugin folders without a `pyproject.toml` (`plugins/open-webui/`, `plugins/claude-code/`), pass those flags explicitly:
   ```bash
   ruff check --line-length 100 --target-version py311 --select E,F,I,UP,B plugins/open-webui plugins/claude-code
   ruff format --check plugins/open-webui plugins/claude-code
   ```
3. **Dual-mode Open WebUI test pass**: Run `plugins/open-webui/tests` both **without** `ebbmem_integration_core` on `sys.path` (standalone fallback) and **with** `PYTHONPATH` pointing to `packages/python/src` and `conformance/src` (`with_core` mode) so both paths pass identically.
4. **Pytest import hygiene**: Run `pytest` from the repository root and do not write `from tests.conftest import ...` in test files (breaks collection depending on working directory).

## 4. IC-1 Conformance Runners & Deliberate-Break Verification

1. **Run both Python and TypeScript runners in mock mode**:
   ```bash
   ebbmem-conformance --mode mock
   node conformance/ts/dist/cli.js --mode mock
   ```
   Both must pass all 21 `[ic1.*]` scenarios (`passed=21, failed=0, skipped=0`).
2. **Maintain runner parity (`runner.py` vs. `conformance/ts/src/runner.ts`)**:
   - All 10 step actions (`rest`, `mcp`, `advance_clock`, `set_exposure_logging`, `erase_namespace`, `set_in_flight`, `assert_memory_state`, `assert_recall_id_valid`, `assert_no_logged_credentials`, `client_check`) and all 6 `client_check` ops must match.
   - Enforce type-preserving full `${var}` interpolation vs. inline string coercion, negative list index traversal, fullmatch regexes (`^(?:...)$`), strict array-length equality, and Ajv Draft-07 `outputSchema` validation.
3. **Deliberate-break negative checks**: Whenever modifying either conformance runner, verify negative probes fail non-zero (`exit 1`):
   - Broken HTTP status, JSON subset, array length, `expect_absent_keys`, or `assert_memory_state` (`storage_strength` mutation on recall).
   - Substring regex on `expect_regex` or missing required field / missing `structuredContent` on MCP `outputSchema`.
   - URL credential injection on `--mode live --base-url "https://example.invalid?api_key=SECRET_CHK"` exits non-zero and never echoes `SECRET_CHK` to stdout/stderr.

## 5. Compatibility Matrix & Badge Sync

- `conformance/ts/src/badges.ts` and `conformance/scripts/generate_compat_table.py` must evaluate identical fail-closed safety gates and tier assignments (`gold` > `silver` > `bronze` > `unverified`).
- After editing `conformance/scripts/compat_manifest.json`:
  ```bash
  python conformance/scripts/generate_compat_table.py --write
  python conformance/scripts/generate_compat_table.py --check
  npm --prefix conformance/ts test
  ```

## 6. Pre-PR Verification Gate

Run all applicable checks before pushing:

1. **Public compliance & instruction pack**:
   ```bash
   pytest -v conformance/tests/test_public_compliance.py conformance/tests/test_instructions_pack.py
   ```
2. **Full conformance & touched package suites** (`ruff check`, `ruff format --check`, `pytest`, `ebbmem-conformance --mode mock`, `npm run typecheck && npm run build && npm test`).
3. **Per-commit `<= 1,000` line cap check** across every commit in `origin/main..HEAD` (not only `HEAD`):
   ```bash
   for sha in $(git rev-list origin/main..HEAD); do
     git diff-tree --no-commit-id --name-only -r "$sha" | while read -r f; do
       lines=$(git show "$sha:$f" 2>/dev/null | wc -l)
       [ "$lines" -le 1000 ] || echo "OVER_1000: $sha $f ($lines)"
     done
   done
   ```
4. **Git & scope hygiene**:
   - Confirm `git merge-base --is-ancestor origin/main HEAD` exits `0`.
   - Confirm every commit in `origin/main..HEAD` has a Conventional Commit header and `Signed-off-by:` trailer (`git commit -s`).
   - Confirm `git diff --stat origin/main -- .github` is empty (unless CI changes are explicitly in scope) and `git status --short` is clean with zero lockfiles, `.venv*`, `dist/`, `build/`, or binary artifacts.
