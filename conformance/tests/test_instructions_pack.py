"""Pytest suite verifying P0-14 agent instruction pack completeness and consistency."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTRUCTIONS_DIR = REPO_ROOT / "instructions"

CANONICAL_AND_WRAPPERS = [
    INSTRUCTIONS_DIR / "CORE.md",
    INSTRUCTIONS_DIR / "SKILL.md",
    INSTRUCTIONS_DIR / "AGENTS.md",
    INSTRUCTIONS_DIR / "cursor" / "ebbmem.mdc",
    INSTRUCTIONS_DIR / "openclaw" / "SKILL.md",
    INSTRUCTIONS_DIR / "claude-code" / "SKILL.md",
]


def _extract_frontmatter(text: str) -> dict:
    assert text.startswith("---\n"), "Expected YAML frontmatter"
    end = text.index("\n---\n", 4)
    return yaml.safe_load(text[4:end])


def test_instruction_pack_files_exist() -> None:
    expected = CANONICAL_AND_WRAPPERS + [INSTRUCTIONS_DIR / "README.md"]
    for path in expected:
        assert path.is_file(), f"Missing instruction file: {path}"


def test_core_instruction_is_concise() -> None:
    lines = (INSTRUCTIONS_DIR / "CORE.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) < 60, f"CORE.md should stay concise (<60 lines), got {len(lines)}"


@pytest.mark.parametrize(
    "path,required_keys",
    [
        (INSTRUCTIONS_DIR / "SKILL.md", ("name", "description")),
        (INSTRUCTIONS_DIR / "cursor" / "ebbmem.mdc", ("description", "alwaysApply")),
        (INSTRUCTIONS_DIR / "openclaw" / "SKILL.md", ("name", "description")),
        (INSTRUCTIONS_DIR / "claude-code" / "SKILL.md", ("name", "description")),
    ],
)
def test_wrapper_frontmatter(path: Path, required_keys: tuple[str, ...]) -> None:
    fm = _extract_frontmatter(path.read_text(encoding="utf-8"))
    for key in required_keys:
        assert key in fm, f"{path} frontmatter missing {key}"


@pytest.mark.parametrize(
    "path", CANONICAL_AND_WRAPPERS, ids=[p.name for p in CANONICAL_AND_WRAPPERS]
)
def test_core_rules_preserved_across_all_files(path: Path) -> None:
    lower = path.read_text(encoding="utf-8").lower()
    required_terms = (
        "memory_recall",
        "read-only",
        "decay",
        "memory_reinforce",
        "used_ids",
        "recall_id",
        "injected",
        "memory_remember",
        "secret",
        "untrusted",
        "memory_forget",
        "memory_purge",
        "authorization: bearer",
        "x-api-key",
    )
    for term in required_terms:
        assert term in lower, f"{path} missing required discipline term: {term!r}"


def test_openclaw_skill_includes_aliases() -> None:
    text = (INSTRUCTIONS_DIR / "openclaw" / "SKILL.md").read_text(encoding="utf-8")
    assert "memory_search" in text
    assert "memory_get" in text
