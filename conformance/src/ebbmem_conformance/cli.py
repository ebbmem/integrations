"""CLI entrypoint for ebbmem-conformance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ebbmem_conformance.client import CredentialInUrlError, redact_secrets, validate_base_url
from ebbmem_conformance.runner import run_all_scenarios


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ebbmem-conformance",
        description="Run IC-1 conformance scenarios for EbbMem integrations.",
    )
    parser.add_argument(
        "--mode",
        choices=("mock", "live"),
        required=True,
        help="Execution mode ('mock' runs MockEbbmemServer; 'live' is stubbed).",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        help="Optional single IC-1 scenario ID to run (e.g. ic1.roundtrip.remember_recall).",
    )
    parser.add_argument(
        "--scenarios-dir",
        type=Path,
        default=None,
        help="Optional custom directory containing YAML scenario files.",
    )
    parser.add_argument(
        "--json-report",
        type=Path,
        default=None,
        help="Optional path to write structured JSON results.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Target server URL for --mode live.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.mode == "live":
        if not args.base_url:
            print("Error: --base-url is required with --mode live.", file=sys.stderr)
            return 1
        try:
            validate_base_url(args.base_url)
        except CredentialInUrlError as exc:
            print(f"Security error in --base-url: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"Invalid --base-url: {redact_secrets(str(exc))}", file=sys.stderr)
            return 1
        print("Live conformance mode is stubbed in v0; use --mode mock for reference verification.")
        return 2

    results = run_all_scenarios(
        scenarios_dir=args.scenarios_dir,
        scenario_filter=args.scenario,
    )
    if not results:
        print("Error: no matching conformance scenarios found.", file=sys.stderr)
        return 1

    passed_count = sum(1 for r in results if r.passed)
    total_count = len(results)

    print(f"EbbMem IC-1 Conformance (mock): {passed_count}/{total_count} passed")
    for res in results:
        status_label = "PASS" if res.passed else "FAIL"
        print(f"  [{status_label}] {res.id} ({res.step_count} steps, {res.duration_ms}ms)")
        if res.error:
            print(f"         error: {res.error}")

    if args.json_report is not None:
        report_payload: dict[str, Any] = {
            "mode": "mock",
            "passed": passed_count,
            "total": total_count,
            "results": [
                {
                    "id": r.id,
                    "title": r.title,
                    "passed": r.passed,
                    "duration_ms": r.duration_ms,
                    "step_count": r.step_count,
                    "error": r.error,
                }
                for r in results
            ],
        }
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")

    return 0 if passed_count == total_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
