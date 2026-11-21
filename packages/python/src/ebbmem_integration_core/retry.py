"""Timeout, idempotent-only retry policy, backoff, and idempotency key helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import time
import urllib.parse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypeVar

from ebbmem_integration_core.errors import ValidationError

T = TypeVar("T")

DEFAULT_TIMEOUT_S: float = 30.0
DEFAULT_MAX_RETRIES: int = 2
DEFAULT_BACKOFF_BASE_S: float = 0.2
DEFAULT_BACKOFF_CAP_S: float = 8.0
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 502, 503, 504})

_ALWAYS_IDEMPOTENT_POST_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/memory/recall",
        "/api/v1/memory/recall/federated",
        "/api/v1/memory/why-not",
    }
)
_KEYED_REMEMBER_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/memory/remember",
        "/api/v1/memory/remember/batch",
    }
)


def is_valid_idempotency_key(value: Any) -> bool:
    """Return True if value is a 1..256-char printable ASCII VCHAR (0x21..0x7E) string."""
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and all(0x21 <= ord(ch) <= 0x7E for ch in value)
    )


def validate_idempotency_key(key: str) -> str:
    """Validate an idempotency key against IC-1 VCHAR 1..256 rules."""
    if not is_valid_idempotency_key(key):
        raise ValidationError(
            "invalid_idempotency_key: must be 1..256 printable ASCII VCHAR characters (0x21..0x7E)"
        )
    return key


def make_idempotency_key(*parts: Any, prefix: str = "ebb_") -> str:
    """Generate a deterministic IC-1 VCHAR idempotency key (`ebb_<64-hex>`)."""
    if not prefix or not all(0x21 <= ord(ch) <= 0x7E for ch in prefix):
        raise ValidationError("prefix must contain printable ASCII VCHAR characters")
    canonical = json.dumps(
        parts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    key = f"{prefix}{digest}"
    if len(key) > 256:
        raise ValidationError("Generated idempotency key exceeds 256 characters")
    return key


def extract_idempotency_key(
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
) -> str | None:
    """Extract a non-empty idempotency key from headers (case-insensitive) or JSON body."""
    if headers:
        for k, v in headers.items():
            if k.lower() == "idempotency-key" and isinstance(v, str) and v:
                return v
    if isinstance(json_body, Mapping):
        body_key = json_body.get("idempotency_key")
        if isinstance(body_key, str) and body_key:
            return body_key
    return None


def is_idempotent_request(
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
) -> bool:
    """Determine whether a request is safe to retry under IC-1 / standard SDK rules.

    Retried:
    - GET and HEAD requests
    - DELETE /api/v1/memory/{id} (forget; excludes /purge)
    - POST /api/v1/memory/recall, /recall/federated, /why-not
    - POST /api/v1/memory/remember (or /remember/batch) ONLY when an idempotency key is present

    NEVER retried:
    - Unkeyed remember
    - reinforce
    - purge
    """
    method_upper = method.upper()
    normalized_path = urllib.parse.urlsplit(path).path.rstrip("/")

    if method_upper in {"GET", "HEAD"}:
        return True

    if method_upper == "DELETE":
        if normalized_path.startswith("/api/v1/memory/") and not normalized_path.endswith("/purge"):
            return True
        return False

    if method_upper == "POST":
        if normalized_path == "/api/v1/memory/reinforce":
            return False
        if normalized_path in _ALWAYS_IDEMPOTENT_POST_PATHS:
            return True
        if normalized_path in _KEYED_REMEMBER_PATHS:
            return extract_idempotency_key(headers=headers, json_body=json_body) is not None
        return False

    return False


def parse_retry_after(headers: Mapping[str, str] | None) -> float | None:
    """Parse a numeric Retry-After header in seconds, returning None if absent/invalid."""
    if not headers:
        return None
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                parsed = float(str(v).strip())
            except ValueError:
                return None
            if math.isfinite(parsed) and parsed >= 0.0:
                return parsed
            return None
    return None


def compute_backoff_s(
    attempt: int,
    *,
    base_s: float = DEFAULT_BACKOFF_BASE_S,
    cap_s: float = DEFAULT_BACKOFF_CAP_S,
    jitter_fn: Callable[[float, float], float] | None = None,
    retry_after_s: float | None = None,
) -> float:
    """Compute bounded exponential backoff `min(cap, base * 2^attempt) + uniform(0, base/2)`."""
    if retry_after_s is not None and math.isfinite(retry_after_s) and retry_after_s >= 0.0:
        return float(retry_after_s)
    bounded = min(cap_s, base_s * (2**attempt))
    rand = jitter_fn if jitter_fn is not None else random.uniform
    return bounded + rand(0.0, bounded / 2.0)


@dataclass(frozen=True)
class RetryPolicy:
    """Configurable IC-1 retry and timeout policy."""

    timeout_s: float = DEFAULT_TIMEOUT_S
    max_retries: int = DEFAULT_MAX_RETRIES
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S
    sleep_fn: Callable[[float], None] | None = None
    async_sleep_fn: Callable[[float], Awaitable[None]] | None = None
    jitter_fn: Callable[[float, float], float] | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValidationError("timeout_s must be a positive finite number")
        if self.max_retries < 0:
            raise ValidationError("max_retries must be >= 0")
        if not math.isfinite(self.backoff_base_s) or self.backoff_base_s < 0:
            raise ValidationError("backoff_base_s must be >= 0")
        if not math.isfinite(self.backoff_cap_s) or self.backoff_cap_s < 0:
            raise ValidationError("backoff_cap_s must be >= 0")

    def sleep(self, delay_s: float) -> None:
        if delay_s <= 0:
            return
        fn = self.sleep_fn if self.sleep_fn is not None else time.sleep
        fn(delay_s)

    async def async_sleep(self, delay_s: float) -> None:
        if delay_s <= 0:
            return
        fn = self.async_sleep_fn if self.async_sleep_fn is not None else asyncio.sleep
        await fn(delay_s)

    def delay_for_attempt(self, attempt: int, *, retry_after_s: float | None = None) -> float:
        return compute_backoff_s(
            attempt,
            base_s=self.backoff_base_s,
            cap_s=self.backoff_cap_s,
            jitter_fn=self.jitter_fn,
            retry_after_s=retry_after_s,
        )
