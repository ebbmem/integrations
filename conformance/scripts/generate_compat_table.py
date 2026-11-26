#!/usr/bin/env python3
"""Deterministic compatibility matrix generator and drift checker."""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

BEGIN_MARKER = "<!-- BEGIN_COMPAT_TABLE -->"
END_COMPAT_MARKER = "<!-- END_COMPAT_TABLE -->"

REQUIRED_FRAMEWORK_CATEGORIES = (
    "scoping_isolation",
    "cognitive_lifecycle",
    "resilience",
    "security",
    "closure_and_mcp",
)

BADGE_DISPLAY = {
    "gold": "`gold`",
    "silver": "`silver`",
    "bronze": "`bronze`",
    "unverified": "`unverified`",
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parent / "compat_manifest.json"


def default_target_path() -> Path:
    return repo_root() / "docs" / "integrations" / "README.md"


def evaluate_badge(evidence: dict[str, Any]) -> str:
    """Evaluate deterministic badge tier matching conformance/ts/src/badges.ts."""
    status = evidence["status"]
    kind = evidence["kind"]
    invariants = evidence["invariants"]
    mock_res = evidence["mock_results"]
    live_res = evidence["live_results"]

    if status != "available":
        return "unverified"
    if not invariants.get("header_only_auth") or not invariants.get("telemetry_disabled_default"):
        return "unverified"
    if kind in ("framework-adapter", "host-plugin") and not all(
        invariants.get(key)
        for key in (
            "injected_default_off",
            "read_only_recall",
            "untrusted_framing",
            "secret_redaction",
        )
    ):
        return "unverified"
    if int(mock_res["failed"]) > 0 or int(live_res["failed"]) > 0:
        return "unverified"

    full_mock = int(mock_res["required"]) > 0 and int(mock_res["passed"]) >= int(
        mock_res["required"]
    )
    full_live = int(live_res["required"]) > 0 and int(live_res["passed"]) >= int(
        live_res["required"]
    )
    mcp_ok = (not evidence.get("mcp_supported")) or bool(evidence.get("mcp_surface_validated"))

    if full_mock and full_live and mcp_ok:
        return "gold"

    if full_mock:
        categories = evidence.get("ic1_categories_covered") or []
        if kind != "framework-adapter" or not categories:
            return "silver"
        if all(cat in categories for cat in REQUIRED_FRAMEWORK_CATEGORIES):
            return "silver"

    if (
        evidence.get("mcp_surface_validated")
        or evidence.get("config_schema_validated")
        or int(mock_res["passed"]) > 0
    ):
        return "bronze"

    return "unverified"


def render_compat_section(manifest: dict[str, Any]) -> str:
    """Render deterministic badge legend + markdown compatibility matrix."""
    lines = [
        "Badge tiers (evaluated by [`conformance/ts/src/badges.ts`]"
        "(../../conformance/ts/src/badges.ts)):",
        "",
        "- `gold` — Full IC-1 mock and live suites pass with MCP surface and safety invariants "
        "verified.",
        "- `silver` — Full IC-1 mock suite passes with safety invariants verified (live run "
        "pending).",
        "- `bronze` — Available on `main` with validated MCP surface, config schema, or partial "
        "mock hook checks.",
        "- `unverified` — `in progress` / `planned`, or failed a mandatory safety invariant gate.",
        "",
        "| Target | MCP Remote (`/mcp`) | MCP Local (`stdio`) | Native Adapter / Integration Shape "
        "| Distribution / Artifact | Status | Badge | On `main` & Notes |",
        "| --- | :---: | :---: | --- | --- | :---: | :---: | --- |",
    ]
    for row in manifest["targets"]:
        ev = row["evidence"]
        status = ev["status"]
        badge = BADGE_DISPLAY[evaluate_badge(ev)]
        lines.append(
            f"| {row['target']} | {row['mcp_remote']} | {row['mcp_local']} "
            f"| {row['native_shape']} | {row['distribution']} | `{status}` "
            f"| {badge} | {row['notes']} |"
        )
    return "\n".join(lines)


def apply_compat_table(target_text: str, manifest: dict[str, Any]) -> str:
    """Replace content between BEGIN_COMPAT_TABLE and END_COMPAT_TABLE markers."""
    begin_idx = target_text.find(BEGIN_MARKER)
    end_idx = target_text.find(END_COMPAT_MARKER)
    if begin_idx < 0 or end_idx < 0 or end_idx < begin_idx:
        raise ValueError(
            f"Target document must contain {BEGIN_MARKER!r} before {END_COMPAT_MARKER!r}."
        )
    rendered = render_compat_section(manifest)
    before = target_text[: begin_idx + len(BEGIN_MARKER)]
    after = target_text[end_idx:]
    return f"{before}\n{rendered}\n{after}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or verify the EbbMem integration compatibility matrix.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=default_manifest_path(),
        help="Path to compat_manifest.json.",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=default_target_path(),
        help="Target markdown file containing compatibility table markers.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--check",
        action="store_true",
        help="Verify target matches generated table; exit 1 with diff on drift.",
    )
    mode_group.add_argument(
        "--write",
        action="store_true",
        help="Write updated compatibility table to target (default when --check is omitted).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    current_text = args.target.read_text(encoding="utf-8")
    expected_text = apply_compat_table(current_text, manifest)

    if args.check:
        if current_text == expected_text:
            print(f"OK: {args.target} matches {args.manifest}.")
            return 0
        diff = "".join(
            difflib.unified_diff(
                current_text.splitlines(keepends=True),
                expected_text.splitlines(keepends=True),
                fromfile=str(args.target),
                tofile=f"{args.target} (generated)",
            )
        )
        print(diff, file=sys.stderr, end="")
        print(
            "Compatibility table drift detected. Run: "
            "python conformance/scripts/generate_compat_table.py --write",
            file=sys.stderr,
        )
        return 1

    args.target.write_text(expected_text, encoding="utf-8")
    print(f"Updated {args.target} from {args.manifest}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
