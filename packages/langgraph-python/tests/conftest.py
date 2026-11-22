"""Shared pytest fixtures for ebbmem-langgraph tests."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from ebbmem_integration_core import RetryPolicy

try:
    from ebbmem_conformance.mock_server import MockEbbmemServer
except ImportError:
    _CONFORMANCE_SRC = Path(__file__).resolve().parents[3] / "conformance" / "src"
    sys.path.insert(0, str(_CONFORMANCE_SRC))
    from ebbmem_conformance.mock_server import MockEbbmemServer


def make_fast_retry_policy(*, timeout_s: float = 2.0, max_retries: int = 2) -> RetryPolicy:
    return RetryPolicy(
        timeout_s=timeout_s,
        max_retries=max_retries,
        backoff_base_s=0.001,
        backoff_cap_s=0.005,
        jitter_fn=lambda _a, _b: 0.0,
    )


@pytest.fixture
def mock_server() -> Iterator[MockEbbmemServer]:
    with MockEbbmemServer() as server:
        yield server


@pytest.fixture
def fast_retry_policy() -> RetryPolicy:
    return make_fast_retry_policy()
