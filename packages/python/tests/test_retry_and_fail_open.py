"""Unit tests for retry policy, backoff, idempotency keys, and fail-open behavior."""

from __future__ import annotations

import http.client
import json
import urllib.request

import pytest

from ebbmem_integration_core.client import IntegrationClient
from ebbmem_integration_core.errors import (
    ApiValidationError,
    RateLimitError,
    RequestTimeoutError,
    ServerError,
    TransportError,
    ValidationError,
)
from ebbmem_integration_core.retry import (
    RetryPolicy,
    compute_backoff_s,
    is_idempotent_request,
    make_idempotency_key,
    parse_retry_after,
    validate_idempotency_key,
)
from ebbmem_integration_core.scoping import ScopeResolver
from ebbmem_integration_core.transport import SdkCapabilities, StdlibHttpTransport


def test_make_and_validate_idempotency_key() -> None:
    k1 = make_idempotency_key("acme:u:alice", "remember", "hello")
    k2 = make_idempotency_key("acme:u:alice", "remember", "hello")
    k3 = make_idempotency_key("acme:u:alice", "remember", "world")
    assert k1 == k2
    assert k1 != k3
    assert k1.startswith("ebb_")
    assert len(k1) == 68
    assert validate_idempotency_key(k1) == k1

    for bad in ("", "has space", "bad\tchar", "x" * 257):
        with pytest.raises(ValidationError):
            validate_idempotency_key(bad)


def test_is_idempotent_request_rules() -> None:
    assert is_idempotent_request("GET", "/api/v1/memory/status") is True
    assert is_idempotent_request("HEAD", "/healthz") is True
    assert is_idempotent_request("POST", "/api/v1/memory/recall") is True
    assert is_idempotent_request("DELETE", "/api/v1/memory/m-123") is True
    assert is_idempotent_request("DELETE", "/api/v1/memory/m-123/purge") is False

    # Unkeyed remember and reinforce must NEVER be retried
    assert (
        is_idempotent_request("POST", "/api/v1/memory/remember", json_body={"content": "x"})
        is False
    )
    assert (
        is_idempotent_request("POST", "/api/v1/memory/reinforce", json_body={"memory_id": "m1"})
        is False
    )

    # Keyed remember (header case-insensitive or body key) IS retried
    assert (
        is_idempotent_request(
            "POST",
            "/api/v1/memory/remember",
            headers={"idempotency-key": "k-1"},
            json_body={"content": "x"},
        )
        is True
    )
    assert (
        is_idempotent_request(
            "POST",
            "/api/v1/memory/remember",
            json_body={"content": "x", "idempotency_key": "k-2"},
        )
        is True
    )


def test_compute_backoff_and_retry_after() -> None:
    assert compute_backoff_s(0, jitter_fn=lambda a, b: 0.0) == pytest.approx(0.2)
    assert compute_backoff_s(0, jitter_fn=lambda a, b: b) == pytest.approx(0.3)
    assert compute_backoff_s(10, jitter_fn=lambda a, b: 0.0) == pytest.approx(8.0)
    assert compute_backoff_s(2, retry_after_s=1.5) == pytest.approx(1.5)
    assert parse_retry_after({"retry-after": "2.5"}) == pytest.approx(2.5)
    assert parse_retry_after({"Retry-After": "not-a-number"}) is None


class _FailingRecallTransport:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.capabilities = SdkCapabilities()
        self.log_sink: list[str] = []

    def recall(self, **kwargs: object) -> dict[str, object]:
        raise self.exc


def test_fail_open_recall_degrades_and_redacts_logs() -> None:
    for err in (
        RequestTimeoutError("timeout with Bearer tok_leak_1"),
        ServerError("503 ebb_secret_leak", status=503),
        RateLimitError("429 rate limited", status=429),
    ):
        logs: list[str] = []
        client = IntegrationClient(
            transport=_FailingRecallTransport(err),  # type: ignore[arg-type]
            fail_open=True,
            log_sink=logs,
        )
        res = client.recall("hello", app_namespace="acme:u:alice")
        assert res == {"query": "hello", "count": 0, "results": [], "degraded": True}
        assert len(logs) == 1
        assert "tok_leak_1" not in logs[0]
        assert "ebb_secret_leak" not in logs[0]

    # fail_open=False re-raises transient errors
    strict_client = IntegrationClient(
        transport=_FailingRecallTransport(ServerError("503 down", status=503)),  # type: ignore[arg-type]
        fail_open=False,
    )
    with pytest.raises(ServerError):
        strict_client.recall("hello", app_namespace="acme:u:alice")

    # Non-transient validation error always raises even when fail_open=True
    val_client = IntegrationClient(
        transport=_FailingRecallTransport(ApiValidationError("422 bad", status=422)),  # type: ignore[arg-type]
        fail_open=True,
    )
    with pytest.raises(ApiValidationError):
        val_client.recall("hello", app_namespace="acme:u:alice")


def test_retry_policy_validation() -> None:
    with pytest.raises(ValidationError):
        RetryPolicy(timeout_s=0)
    with pytest.raises(ValidationError):
        RetryPolicy(max_retries=-1)


def test_make_idempotency_key_unicode_and_codepoint_key_order() -> None:
    import hashlib

    k_unicode = make_idempotency_key("unicode", "café", "日本語", "😀")
    expected_canonical = json.dumps(
        ["unicode", "café", "日本語", "😀"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    expected = "ebb_" + hashlib.sha256(expected_canonical.encode("utf-8")).hexdigest()
    assert k_unicode == expected

    k_dict = make_idempotency_key({"a": 1, "B": 2, "_c": 3})
    expected_dict_canonical = '[{"B":2,"_c":3,"a":1}]'
    assert k_dict == "ebb_" + hashlib.sha256(expected_dict_canonical.encode("utf-8")).hexdigest()


def test_stdlib_transport_wraps_oserror_and_http_exception_as_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for exc in (
        http.client.RemoteDisconnected("peer closed"),
        ConnectionResetError("reset by peer"),
    ):
        attempts = 0

        def fake_urlopen(
            _req: object,
            timeout: float | None = None,
            _err: Exception = exc,
        ) -> object:
            nonlocal attempts
            del timeout
            attempts += 1
            raise _err

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        logs: list[str] = []
        transport = StdlibHttpTransport(
            "https://example.invalid",
            api_key="ebb_test_key",
            retry_policy=RetryPolicy(
                timeout_s=1.0,
                max_retries=2,
                backoff_base_s=0.0,
                backoff_cap_s=0.0,
                jitter_fn=lambda _a, _b: 0.0,
            ),
            log_sink=logs,
        )
        with pytest.raises(TransportError):
            transport.recall(query="hello", namespace="acme:u:alice")
        assert attempts == 3

        client = IntegrationClient(
            transport=transport,
            scope=ScopeResolver(app_namespace="acme:u:alice"),
            fail_open=True,
            log_sink=logs,
        )
        res = client.recall("hello")
        assert res == {"query": "hello", "count": 0, "results": [], "degraded": True}
