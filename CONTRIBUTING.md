# Contributing to EbbMem Integrations

Thank you for contributing to `ebbmem/integrations`! All contributions are licensed under the [Apache License, Version 2.0](LICENSE).

## Developer Certificate of Origin (DCO)

Please sign off all commits with `git commit -s` to certify you have the right to submit the contribution under the Apache-2.0 license.

## Before Opening a Pull Request

Run lint, formatting, unit/compliance tests, and the mock conformance suite locally:

```bash
pip install -e "./conformance[dev]"
ruff check conformance
ruff format --check conformance
pytest -v conformance/tests
ebbmem-conformance --mode mock
```

## Public Repository Hygiene

To keep this repository safe and accurate for all users:

- **No secrets or credentials**: Never commit API keys, tokens, passwords, or private endpoints.
- **No telemetry-enabled defaults**: Adapters and conformance tools must never enable telemetry or phone-home reporting by default.
- **No internal hostnames or local paths**: Keep paths relative and hostnames generic (for example, `127.0.0.1` or `example.invalid`).
- **No unverified claims**: Do not add unmeasured latency, throughput, accuracy percentages, or unverified compliance certifications.
- **Preserve core memory semantics**: Keep `recall` strictly read-only, require explicit `reinforce` feedback on used memories, and keep `injected` (reinforce-all-shown) OFF by default.
