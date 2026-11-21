# EbbMem Integrations

Official open-source (Apache-2.0) conformance suite, agent instruction pack, and adapter packages for [EbbMem](https://ebbmem.ai) — cognitive memory for AI agents.

## Why EbbMem Integrations Differ from CRUD Vector Stores

EbbMem models human-inspired memory dynamics rather than static key-value or vector CRUD:

1. **Memories decay unless reinforced.** Unused memories fade over time. Calling `recall` is strictly **read-only** — retrieving a memory never boosts its storage strength or resets its spacing state.
2. **Explicit usage feedback is required.** Adapters and agents must call `reinforce` (`memory_id`, `used_ids`, and optional `recall_id` when exposure logging is enabled) **only on memories that were actually used** to answer or act.
3. **Reinforce-all-shown (`injected`) is OFF by default.** Blindly reinforcing every retrieved memory creates uninformative popularity loops that crowd out genuinely useful knowledge.
4. **Untrusted recall framing & header-only credentials.** Recalled text must be framed as untrusted reference data (never executed as instructions), and credentials must travel only in `Authorization: Bearer` or `X-API-Key` headers.

For background on cognitive memory architecture and evaluation concepts, see:

- [EbbMem](https://ebbmem.ai)
- [Spector](https://github.com/spectrayan/spector)
- [Memory Fundamentals](https://github.com/spectrayan/memory-fundamentals)

## Repository Layout

| Path | Purpose |
| --- | --- |
| `conformance/` | Language-neutral IC-1 conformance YAML scenarios, reference mock server, client helpers, CLI, and test suite |
| `instructions/` | Canonical agent memory discipline (`CORE.md`) and wrappers for Agent Skills, `AGENTS.md`, Cursor, OpenClaw, and Claude Code |
| `packages/python/` | Python `ebbmem-integration-core` package (scoping, redaction/framing, fail-open/retry, reinforcement tracking, SDK adapter) |
| `packages/typescript/` | TypeScript `ebbmem-integration-core` package (scoping, redaction/framing, fail-open/retry, reinforcement tracking, SDK adapter) |

## Quickstart: Run Conformance Tests

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e "./conformance[dev]"

ruff check conformance
ruff format --check conformance
pytest -v conformance/tests
ebbmem-conformance --mode mock
```

Live conformance mode (`--mode live --base-url <url>`) is stubbed in v0 and validates URL credential safety before exiting with status code `2`.

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
