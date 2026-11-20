"""Automated public repository compliance checks for ebbmem/integrations."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "node_modules",
}


def _iter_repo_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(REPO_ROOT).parts
        if any(
            part in SKIP_DIRS or part.startswith(".venv") or part.endswith(".egg-info")
            for part in rel_parts
        ):
            continue
        files.append(path)
    return sorted(files)


def _forbidden_patterns() -> list[tuple[str, re.Pattern[str]]]:
    # Constructed dynamically so this compliance test does not trigger on its own source.
    return [
        ("private_repo", re.compile("spectrayan" + "/" + "ebbmem", re.IGNORECASE)),
        ("proprietary_license", re.compile("LicenseRef-" + "Proprietary", re.IGNORECASE)),
        ("internal_adr", re.compile("ADR-" + "INT-01", re.IGNORECASE)),
        ("internal_plan", re.compile(r"\bPLAN" + r"\.md\b")),
        ("confidential_marker", re.compile(r"\bCONFIDEN" + r"TIAL\b", re.IGNORECASE)),
        ("internal_hostname", re.compile(r"\bz8" + r"90\b", re.IGNORECASE)),
        ("local_windows_path", re.compile(r"D:\\" + r"git\\", re.IGNORECASE)),
        ("local_container_path", re.compile(r"/work" + r"space/")),
        ("private_ip_192", re.compile(r"\b192\." + r"168\.")),
        ("private_ip_10", re.compile(r"\b10\." + r"0\.")),
        ("bot_username", re.compile("jarvis" + "pectrayan", re.IGNORECASE)),
        ("hipaa_claim", re.compile(r"\bHI" + r"PAA\b", re.IGNORECASE)),
        ("soc2_claim", re.compile(r"\bSOC\s?" + r"2\b", re.IGNORECASE)),
        ("gdpr_claim", re.compile(r"GDPR[\s-]+(?:compliant|certified)", re.IGNORECASE)),
        ("aaif_claim", re.compile(r"\bAA" + r"IF\b")),
        ("unverified_kv_claim", re.compile(r"90%\s+LLM\s+KV", re.IGNORECASE)),
        ("internal_engine_name", re.compile(r"\bEbb" + r"Engine\b")),
        ("internal_scan_phrase", re.compile("fused 6-" + "phase", re.IGNORECASE)),
    ]


def test_no_forbidden_public_repo_strings() -> None:
    patterns = _forbidden_patterns()
    violations: list[str] = []
    for path in _iter_repo_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for label, pattern in patterns:
            if pattern.search(text):
                violations.append(f"{rel}: matched {label}")
    assert not violations, "Public compliance violations:\n" + "\n".join(violations)


def test_readme_documents_public_links_and_decay_semantics() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    lower = readme.lower()
    assert "https://ebbmem.ai" in readme
    assert "https://github.com/spectrayan/spector" in readme
    assert "https://github.com/spectrayan/memory-fundamentals" in readme
    assert "decay" in lower
    assert "read-only" in lower
    assert "reinforce" in lower
    assert "injected" in lower
