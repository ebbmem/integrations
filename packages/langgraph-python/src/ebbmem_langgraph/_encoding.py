"""Namespace tuple mapping, value encoding, and Item hydration for EbbmemStore."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from langgraph.store.base import Item, SearchItem, get_text_at_path, tokenize_path

STORE_TAG = "lg:store"
META_KEY = "lg_key"
META_NAMESPACE = "lg_namespace"
META_VALUE = "lg_value"
META_CREATED_AT = "lg_created_at"
META_UPDATED_AT = "lg_updated_at"
META_INDEXED = "lg_indexed"


def _hash_payload(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def namespace_tag(namespace: tuple[str, ...]) -> str:
    """Return the exact LangGraph namespace tuple tag."""
    return f"lg:ns:{_hash_payload(list(namespace))}"


def prefix_tag(prefix: tuple[str, ...]) -> str:
    """Return the prefix tag for depth `len(prefix)`."""
    return f"lg:pfx:{len(prefix)}:{_hash_payload(list(prefix))}"


def item_tag(namespace: tuple[str, ...], key: str) -> str:
    """Return the exact `(namespace, key)` tag."""
    return f"lg:item:{_hash_payload([list(namespace), key])}"


def composite_external_key(namespace: tuple[str, ...], key: str) -> str:
    """Deterministic external key encoding both LangGraph tuple and key."""
    return f"lg:{_hash_payload([list(namespace), key])}:{key}"


def build_store_tags(namespace: tuple[str, ...], key: str) -> list[str]:
    """Build deterministic LangGraph hierarchy tags for EbbMem storage."""
    tags = [STORE_TAG, namespace_tag(namespace), item_tag(namespace, key)]
    for depth in range(1, len(namespace) + 1):
        tags.append(prefix_tag(namespace[:depth]))
    return tags


def normalize_value(value: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a LangGraph item value mapping into a plain JSON-friendly dict."""
    if not isinstance(value, Mapping):
        raise TypeError("Store item value must be a Mapping")
    return _coerce_json_value(dict(value))


def _coerce_json_value(obj: Any) -> Any:
    if obj is None or isinstance(obj, bool | int | float | str):
        return obj
    if isinstance(obj, Mapping):
        return {str(k): _coerce_json_value(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_coerce_json_value(v) for v in obj]
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return _coerce_json_value(model_dump(mode="json"))
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def _collect_strings(obj: Any, out: list[str]) -> None:
    if isinstance(obj, str):
        stripped = obj.strip()
        if stripped:
            out.append(stripped)
        return
    if isinstance(obj, Mapping):
        for v in obj.values():
            _collect_strings(v, out)
        return
    if isinstance(obj, list | tuple):
        for v in obj:
            _collect_strings(v, out)


def extract_searchable_content(
    value: dict[str, Any],
    *,
    index: Literal[False] | list[str] | None = None,
    default_fields: Sequence[str] | None = None,
) -> tuple[str, bool]:
    """Extract searchable text from a LangGraph value dict for EbbMem `remember`.

    Returns `(content, is_indexed)`.
    """
    if index is False:
        return "(unindexed)", False

    fields: Sequence[str] | None = index if isinstance(index, list) else default_fields
    extracted: list[str] = []
    if fields:
        for field_path in fields:
            if field_path == "$":
                _collect_strings(value, extracted)
            else:
                texts = get_text_at_path(value, tokenize_path(field_path))
                for t in texts:
                    if isinstance(t, str) and t.strip():
                        extracted.append(t.strip())
    else:
        _collect_strings(value, extracted)

    if extracted:
        # Deduplicate preserving order
        unique = list(dict.fromkeys(extracted))
        return "\n".join(unique), True

    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return (serialized if serialized.strip() else "{}"), True


def parse_timestamp(raw: Any, fallback_ms: Any = None) -> datetime:
    """Parse an ISO timestamp or epoch milliseconds into a UTC `datetime`."""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    if isinstance(fallback_ms, int | float) and not isinstance(fallback_ms, bool):
        return datetime.fromtimestamp(float(fallback_ms) / 1000.0, tz=UTC)
    return datetime.now(UTC)


def decode_stored_record(
    record: Mapping[str, Any],
    *,
    score: float | None = None,
    as_search_item: bool = False,
) -> tuple[Item | SearchItem, bool, int] | None:
    """Hydrate a LangGraph `Item` or `SearchItem` from an EbbMem record dict.

    Returns `(item, is_indexed, sort_ts_ms)` or `None` if not a LangGraph store record.
    """
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    raw_ns = metadata.get(META_NAMESPACE)
    raw_key = metadata.get(META_KEY)
    raw_val = metadata.get(META_VALUE)
    if not isinstance(raw_ns, list | tuple) or not isinstance(raw_key, str):
        return None
    if not isinstance(raw_val, Mapping):
        return None

    namespace = tuple(str(seg) for seg in raw_ns)
    created_ms = record.get("created_at_ms")
    created_at = parse_timestamp(metadata.get(META_CREATED_AT), fallback_ms=created_ms)
    updated_at = parse_timestamp(metadata.get(META_UPDATED_AT), fallback_ms=created_ms)
    is_indexed = bool(metadata.get(META_INDEXED, True))
    sort_ts_ms = int(updated_at.timestamp() * 1000)

    if as_search_item:
        item: Item | SearchItem = SearchItem(
            namespace=namespace,
            key=raw_key,
            value=dict(raw_val),
            created_at=created_at,
            updated_at=updated_at,
            score=score,
        )
    else:
        item = Item(
            namespace=namespace,
            key=raw_key,
            value=dict(raw_val),
            created_at=created_at,
            updated_at=updated_at,
        )
    return item, is_indexed, sort_ts_ms


def extract_message_text(message: Any) -> str:
    """Extract plain text from a LangChain message object or dict."""
    content = (
        message.get("content", "")
        if isinstance(message, Mapping)
        else getattr(message, "content", "")
    )
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(p for p in parts if p)
    return str(content) if content is not None else ""
