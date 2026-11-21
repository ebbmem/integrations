"""Unit tests for reinforcement modes, delta validation, and ExposureBookkeeper."""

from __future__ import annotations

import pytest

from ebbmem_integration_core.errors import ValidationError
from ebbmem_integration_core.reinforce import (
    DEFAULT_REINFORCE_DELTA,
    ExposureBookkeeper,
    ReinforcementTracker,
    ReinforcePolicy,
    extract_cited_ids,
    validate_delta,
)


def test_delta_validation_and_clamping() -> None:
    assert DEFAULT_REINFORCE_DELTA == pytest.approx(0.1)
    assert validate_delta(0.25) == pytest.approx(0.25)
    assert validate_delta(-1.0) == pytest.approx(-1.0)
    assert validate_delta(1.0) == pytest.approx(1.0)
    assert validate_delta(2.5, clamp=True) == pytest.approx(1.0)
    assert validate_delta(-3.0, clamp=True) == pytest.approx(-1.0)

    for bad in (True, False, float("nan"), float("inf"), float("-inf"), "not-a-num", 1.5, -1.5):
        with pytest.raises(ValidationError):
            validate_delta(bad)


def test_reinforce_policy_injected_off_by_default() -> None:
    policy = ReinforcePolicy()
    assert policy.injected is False
    assert policy.mode == "cited"

    selected = policy.select_ids_to_reinforce(["m1", "m2", "m3"], ["m2", "m_unknown", "m2"])
    assert selected == ["m2"]

    opted_in = ReinforcePolicy(injected=True)
    assert opted_in.select_ids_to_reinforce(["m1", "m2"], ["m2"]) == ["m2", "m1"]


def test_extract_cited_ids_preserves_citation_order() -> None:
    text = "Based on [m2] and m1 (but not m10), we proceed."
    assert extract_cited_ids(text, ["m1", "m2", "m3"]) == ["m2", "m1"]


def test_exposure_bookkeeper_tracks_turns_bounds_and_isolation() -> None:
    bookkeeper = ExposureBookkeeper(max_entries=2)
    r1 = bookkeeper.record_recall(
        "acme:u:alice",
        {
            "recall_id": "rec-1",
            "results": [
                {"memory_id": "m1", "score": 0.9},
                {"memory_id": "m2", "score": 0.7},
            ],
        },
        turn_id="t1",
        timestamp_ms=1000,
    )
    assert r1.ranks == {"m1": 1, "m2": 2}
    assert r1.scores == {"m1": 0.9, "m2": 0.7}
    assert bookkeeper.get_by_recall_id("rec-1", namespace="other:u:bob") is None
    assert bookkeeper.get_by_recall_id("rec-1", namespace="acme:u:alice") == r1

    bookkeeper.record_recall(
        "acme:u:alice",
        {"results": [{"memory_id": "m3", "score": 0.8}]},
        turn_id="t2",
        timestamp_ms=2000,
    )
    bookkeeper.record_recall(
        "acme:u:alice",
        {"results": [{"memory_id": "m4", "score": 0.6}]},
        turn_id="t3",
        timestamp_ms=3000,
    )
    # t1 evicted by bounded capacity=2
    assert bookkeeper.get_for_turn("acme:u:alice", "t1") is None
    assert bookkeeper.get_by_recall_id("rec-1") is None
    assert bookkeeper.get_for_turn("acme:u:alice", "t3") is not None


def test_reinforcement_tracker_modes_and_recall_id_fallback() -> None:
    tracker = ReinforcementTracker()
    tracker.record_exposure(
        "acme:u:alice",
        {
            "results": [
                {"memory_id": "m1", "score": 0.9},
                {"memory_id": "m2", "score": 0.8},
            ]
        },
        turn_id="turn-no-id",
    )
    # Cited mode with no citations -> None (skip reinforce)
    assert (
        tracker.plan_reinforcement(
            "acme:u:alice",
            response_text="Nothing cited",
            turn_id="turn-no-id",
        )
        is None
    )

    # Cited mode with citation & absent recall_id -> clean fallback with recall_id=None
    plan_cited = tracker.plan_reinforcement(
        "acme:u:alice",
        response_text="Confirmed via memory m2.",
        turn_id="turn-no-id",
    )
    assert plan_cited is not None
    assert plan_cited.memory_id == "m2"
    assert plan_cited.used_ids == ()
    assert plan_cited.recall_id is None
    assert plan_cited.delta == pytest.approx(0.1)

    # Tool mode with explicit used_ids and recall_id present
    tracker.record_exposure(
        "acme:u:alice",
        {
            "recall_id": "rec-uuid-1",
            "results": [
                {"memory_id": "m1", "score": 0.9},
                {"memory_id": "m2", "score": 0.8},
                {"memory_id": "m3", "score": 0.7},
            ],
        },
        turn_id="turn-with-id",
    )
    plan_tool = tracker.plan_reinforcement(
        "acme:u:alice",
        mode="tool",
        used_ids=["m3", "m1"],
        turn_id="turn-with-id",
    )
    assert plan_tool is not None
    assert plan_tool.memory_id == "m3"
    assert plan_tool.used_ids == ("m1",)
    assert plan_tool.recall_id == "rec-uuid-1"

    # Feedback mode with negative delta
    plan_fb = tracker.plan_reinforcement(
        "acme:u:alice",
        mode="feedback",
        used_ids=["m9"],
        delta=-0.5,
    )
    assert plan_fb is not None
    assert plan_fb.memory_id == "m9"
    assert plan_fb.delta == pytest.approx(-0.5)


def test_plan_reinforcement_does_not_fallback_to_latest_on_explicit_miss() -> None:
    tracker = ReinforcementTracker(policy=ReinforcePolicy(mode="tool"))
    tracker.record_exposure(
        "acme:u:alice",
        {"recall_id": "rec-1", "results": [{"memory_id": "m1"}]},
        turn_id="turn-1",
    )

    assert (
        tracker.plan_reinforcement(
            "acme:u:alice",
            used_ids=["m1"],
            turn_id="turn-missing",
        )
        is None
    )
    assert (
        tracker.plan_reinforcement(
            "acme:u:alice",
            used_ids=["m1"],
            recall_id="rec-missing",
        )
        is None
    )


def test_exposure_bookkeeper_composite_recall_id_and_true_lru_eviction() -> None:
    bookkeeper = ExposureBookkeeper(max_entries=2)
    r_alice = bookkeeper.record_recall(
        "acme:u:alice",
        {"recall_id": "shared-rec", "results": [{"memory_id": "m-alice"}]},
        turn_id="t1",
    )
    r_bob = bookkeeper.record_recall(
        "other:u:bob",
        {"recall_id": "shared-rec", "results": [{"memory_id": "m-bob"}]},
        turn_id="t1",
    )
    assert bookkeeper.get_by_recall_id("shared-rec", namespace="acme:u:alice") == r_alice
    assert bookkeeper.get_by_recall_id("shared-rec", namespace="other:u:bob") == r_bob

    # Touch Alice's record to make it MRU before inserting a 3rd entry -> Bob's is evicted
    assert bookkeeper.get_for_turn("acme:u:alice", "t1") == r_alice
    bookkeeper.record_recall(
        "acme:u:carol",
        {"recall_id": "rec-3", "results": [{"memory_id": "m-carol"}]},
        turn_id="t3",
    )
    assert bookkeeper.get_by_recall_id("shared-rec", namespace="acme:u:alice") == r_alice
    assert bookkeeper.get_by_recall_id("shared-rec", namespace="other:u:bob") is None
