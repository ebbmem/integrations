"""Unit tests for IC-1 scoping resolver, namespace validation, tags, and metadata."""

from __future__ import annotations

import pytest

from ebbmem_integration_core.errors import NamespaceError, ReservedMetadataError, ValidationError
from ebbmem_integration_core.scoping import (
    ScopeResolver,
    check_namespace_conflict,
    normalize_tags_and_session,
    validate_metadata,
    validate_namespace,
)


def test_validate_namespace_accepts_valid_and_rejects_invalid() -> None:
    assert validate_namespace("acme:u:alice") == "acme:u:alice"
    assert validate_namespace("app_1.prod/ns-2") == "app_1.prod/ns-2"

    for bad in ("", "a" * 257, "has space", "acme*all", "ns?query=1", "ns\nnewline"):
        with pytest.raises(NamespaceError):
            validate_namespace(bad)


def test_default_per_user_namespace_and_no_explosion() -> None:
    resolver = ScopeResolver(tenant="acme", user_id="alice", agent_id="planner-1")
    r1 = resolver.resolve(thread_id="thread-101", tuple_segments=["org", "support"])
    r2 = resolver.resolve(thread_id="thread-202", tuple_segments=["org", "billing"])

    assert r1.namespace == "acme:u:alice"
    assert r2.namespace == "acme:u:alice"
    assert r1.soul_scope == "agent"
    assert r1.soul_id == "planner-1"
    assert r1.soul_path == "/api/v1/soul/agent/planner-1"
    assert r1.session_id == "thread-101"
    assert r1.thread_key == "thread-101"
    assert r2.session_id == "thread-202"
    assert r1.tags == ("org", "support")
    assert r2.tags == ("org", "billing")


def test_explicit_app_namespace_overrides_per_user() -> None:
    resolver = ScopeResolver(tenant="acme", user_id="alice", app_namespace="shared:kb")
    assert resolver.resolve().namespace == "shared:kb"
    assert resolver.resolve(app_namespace="custom:ns").namespace == "custom:ns"


def test_header_body_namespace_conflict_raises() -> None:
    assert check_namespace_conflict("acme:u:alice", "acme:u:alice") == "acme:u:alice"
    assert check_namespace_conflict("acme:u:alice", None) == "acme:u:alice"
    assert check_namespace_conflict(None, "acme:u:alice") == "acme:u:alice"
    assert check_namespace_conflict(None, None) is None

    with pytest.raises(NamespaceError, match="namespace_conflict"):
        check_namespace_conflict("acme:u:alice", "acme:u:bob")


def test_normalize_tags_strips_hash_extracts_session_and_deduplicates() -> None:
    tags, domain, session_id = normalize_tags_and_session(
        [" #Work ", "session:sess-42", "Work", "#finance", "session:sess-99", ""],
        tuple_segments=[" #team-alpha ", "finance"],
        domain=" #Engineering ",
        session_id=None,
    )
    assert tags == ["Work", "finance", "team-alpha"]
    assert domain == "Engineering"
    assert session_id == "sess-42"

    # Explicit session_id takes precedence over session:<id> tag while still stripping session:*
    tags2, _, sid2 = normalize_tags_and_session(
        ["session:ignored", "#topic"],
        session_id="explicit-sess",
    )
    assert tags2 == ["topic"]
    assert sid2 == "explicit-sess"


def test_validate_metadata_rejects_reserved_ebb_prefix() -> None:
    assert validate_metadata({"source": "chat", "priority": 1}) == {
        "source": "chat",
        "priority": 1,
    }
    assert validate_metadata({"sub": {"ebb.nested": 1}}) == {"sub": {"ebb.nested": 1}}
    for bad_key in ("ebb.internal", "EBB.internal", " ebb.internal"):
        with pytest.raises(ReservedMetadataError, match="reserved_metadata_prefix"):
            validate_metadata({bad_key: "forbidden"})
    with pytest.raises(ValidationError):
        validate_metadata("not-a-mapping")  # type: ignore[arg-type]


def test_check_namespace_conflict_rejects_empty_or_whitespace_header() -> None:
    with pytest.raises(NamespaceError, match="namespace_invalid"):
        check_namespace_conflict("   ", "acme:u:alice")
    with pytest.raises(NamespaceError, match="namespace_invalid"):
        check_namespace_conflict("", None)
    with pytest.raises(NamespaceError, match="namespace_invalid"):
        check_namespace_conflict(None, "   ")
