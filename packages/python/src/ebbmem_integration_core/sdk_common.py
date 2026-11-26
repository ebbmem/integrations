"""Shared helpers for sync and async ebbmem-client SDK adapters."""

from __future__ import annotations

import json
import types
from collections.abc import Mapping
from typing import Annotated, Any, Union, get_args, get_origin

from ebbmem_integration_core.errors import (
    ApiValidationError,
    AuthError,
    EbbmemApiError,
    IdempotencyConflictError,
    NotFoundError,
    RateLimitError,
    RequestTimeoutError,
    ServerError,
    TransportError,
)
from ebbmem_integration_core.retry import parse_retry_after
from ebbmem_integration_core.security import (
    redact_payload,
    redact_secrets,
    validate_auth_credentials,
    validate_base_url,
)
from ebbmem_integration_core.transport_types import SdkCapabilities


def _to_plain_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, Mapping):
        return dict(obj)
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        res = to_dict()
        if isinstance(res, Mapping):
            return dict(res)
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        res = model_dump()
        if isinstance(res, Mapping):
            return dict(res)
    attrs = getattr(obj, "__dict__", None)
    if isinstance(attrs, dict):
        return {k: v for k, v in attrs.items() if not k.startswith("_")}
    return {}


def _validate_sdk_config(sdk_client: Any) -> None:
    config = getattr(sdk_client, "config", None)
    if config is None:
        return
    base_url = getattr(config, "base_url", None)
    if isinstance(base_url, str) and base_url:
        validate_base_url(base_url)
    token = getattr(config, "token", None)
    api_key = getattr(config, "api_key", None)
    validate_auth_credentials(bearer_token=token, api_key=api_key)


def _parse_sdk_error_body(raw_body: Any) -> Any:
    if raw_body is None:
        return None
    if isinstance(raw_body, Mapping):
        return dict(raw_body)
    if isinstance(raw_body, bytes):
        text = raw_body.decode("utf-8", errors="replace")
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if isinstance(raw_body, str):
        if not raw_body.strip():
            return None
        try:
            return json.loads(raw_body)
        except json.JSONDecodeError:
            return raw_body
    return raw_body


def _translate_sdk_exception(exc: Exception) -> EbbmemApiError:
    status: int | None = None
    code: str | None = None
    raw_body: Any = None
    retry_after: float | None = None
    headers: Mapping[str, Any] | None = None
    saw_timeout = False
    saw_connection = False

    cur: BaseException | None = exc
    seen: set[int] = set()
    for _ in range(16):
        if cur is None or id(cur) in seen:
            break
        seen.add(id(cur))
        if isinstance(cur, TimeoutError):
            saw_timeout = True
        if "ConnectionError" in type(cur).__name__:
            saw_connection = True
        if status is None:
            cur_status = getattr(cur, "status", None)
            if isinstance(cur_status, int) and not isinstance(cur_status, bool):
                status = cur_status
        if code is None:
            cur_code = getattr(cur, "code", None)
            if isinstance(cur_code, str) and cur_code:
                code = cur_code
        if raw_body is None:
            cur_body = getattr(cur, "body", None)
            if cur_body is not None:
                raw_body = cur_body
        if retry_after is None:
            cur_retry = getattr(cur, "retry_after", None)
            if isinstance(cur_retry, int | float) and not isinstance(cur_retry, bool):
                retry_after = float(cur_retry)
        if headers is None:
            cur_headers = getattr(cur, "headers", None)
            if isinstance(cur_headers, Mapping):
                headers = cur_headers
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)

    parsed_body = _parse_sdk_error_body(raw_body)
    redacted_body = redact_payload(parsed_body) if parsed_body is not None else None
    detail: str | None = None
    if isinstance(parsed_body, Mapping):
        if code is None:
            body_code = parsed_body.get("code")
            if isinstance(body_code, str) and body_code:
                code = body_code
            else:
                nested_err = parsed_body.get("error")
                if isinstance(nested_err, Mapping):
                    nested_code = nested_err.get("code")
                    if isinstance(nested_code, str) and nested_code:
                        code = nested_code
        raw_detail = parsed_body.get("detail")
        if isinstance(raw_detail, str) and raw_detail:
            detail = raw_detail
        elif isinstance(parsed_body.get("message"), str) and parsed_body.get("message"):
            detail = str(parsed_body["message"])
        elif isinstance(parsed_body.get("error"), Mapping):
            err_msg = parsed_body["error"].get("message")
            if isinstance(err_msg, str) and err_msg:
                detail = err_msg

    if retry_after is None and headers is not None:
        retry_after = parse_retry_after({str(k): str(v) for k, v in headers.items()})

    raw_msg = str(exc).strip()
    if not raw_msg and detail:
        raw_msg = detail
    message = redact_secrets(raw_msg)

    if isinstance(exc, TimeoutError) or (status is None and saw_timeout):
        return RequestTimeoutError(message or "SDK request timed out", body=redacted_body)
    if status in {401, 403}:
        return AuthError(message, status=status, code=code, detail=detail, body=redacted_body)
    if status == 404:
        return NotFoundError(message, status=status, code=code, detail=detail, body=redacted_body)
    if status in {409, 410}:
        return IdempotencyConflictError(
            message, status=status, code=code, detail=detail, body=redacted_body
        )
    if status == 429:
        return RateLimitError(
            message,
            status=status,
            code=code,
            detail=detail,
            retry_after=retry_after,
            body=redacted_body,
        )
    if status in {400, 422}:
        if isinstance(code, str) and code.startswith("EBB-100-02") and code != "EBB-100-023":
            return IdempotencyConflictError(
                message, status=status, code=code, detail=detail, body=redacted_body
            )
        return ApiValidationError(
            message, status=status, code=code, detail=detail, body=redacted_body
        )
    if isinstance(status, int) and status >= 500:
        return ServerError(message, status=status, code=code, detail=detail, body=redacted_body)
    if saw_connection:
        return TransportError(message, body=redacted_body)
    return EbbmemApiError(message, status=status, code=code, detail=detail, body=redacted_body)


def _annotation_accepts_float(annotation: Any) -> bool:
    if annotation is float:
        return True
    origin = get_origin(annotation)
    if origin is Annotated:
        args = get_args(annotation)
        return _annotation_accepts_float(args[0]) if args else False
    if origin in (Union, types.UnionType):
        return any(_annotation_accepts_float(arg) for arg in get_args(annotation))
    return False


def _model_field_accepts_float(req_cls: Any, field_name: str) -> bool:
    fields = getattr(req_cls, "model_fields", None)
    if not isinstance(fields, Mapping) or field_name not in fields:
        return True
    field_info = fields[field_name]
    annotation = getattr(field_info, "annotation", None)
    if annotation is None:
        return True
    return _annotation_accepts_float(annotation)


def _load_sdk_models() -> dict[str, Any]:
    """Import ebbmem_client generated models if ebbmem-client is installed."""
    try:
        from ebbmem_client.generated.models.memory_id import MemoryId
        from ebbmem_client.generated.models.recall_request_model import RecallRequestModel
        from ebbmem_client.generated.models.reinforce_request_model import ReinforceRequestModel
        from ebbmem_client.generated.models.remember_request_model import RememberRequestModel
        from ebbmem_client.generated.models.used_ids_inner import UsedIdsInner
    except ImportError:
        return {}
    models: dict[str, Any] = {
        "MemoryId": MemoryId,
        "RecallRequestModel": RecallRequestModel,
        "ReinforceRequestModel": ReinforceRequestModel,
        "RememberRequestModel": RememberRequestModel,
        "UsedIdsInner": UsedIdsInner,
    }
    try:
        from ebbmem_client.generated.models.session_id import SessionId

        models["SessionId"] = SessionId
    except ImportError:
        pass
    return models


def _detect_sdk_capabilities(sdk_client: Any, models: Mapping[str, Any]) -> SdkCapabilities:
    raw = getattr(sdk_client, "raw", None)
    raw_mem = getattr(raw, "memory", None)
    has_raw = raw_mem is not None
    recall_cls = models.get("RecallRequestModel")
    recall_fields = getattr(recall_cls, "model_fields", {}) if recall_cls is not None else {}
    has_recall_filters = (
        all(f in recall_fields for f in ("tier", "tags", "session_id"))
        if recall_fields
        else bool(getattr(sdk_client, "supports_recall_filters", False))
    )
    has_exact_key = callable(getattr(raw_mem, "get_by_key", None)) or callable(
        getattr(getattr(sdk_client, "memory", None), "get_by_key", None)
    )
    has_ns_list = callable(getattr(raw_mem, "list_namespaces", None)) or callable(
        getattr(getattr(sdk_client, "memory", None), "list_namespaces", None)
    )
    return SdkCapabilities(
        tier_on_remember=has_raw,
        session_id_on_remember=has_raw,
        metadata_on_remember=has_raw,
        recall_filters=has_recall_filters,
        recall_breakdown_and_id=True,
        reinforce_used_ids_and_recall_id=has_raw,
        reinforce_default_delta_0_1=True,
        deterministic_idempotency_key=True,
        exact_key_get=has_exact_key,
        exact_key_delete=has_exact_key,
        metadata_filter_on_list_recall=bool(getattr(sdk_client, "supports_metadata_filter", False)),
        namespace_prefix_list=has_ns_list,
        notes=(
            "Released ebbmem-client 0.2.0 facade omits tier/session_id/metadata on remember, "
            "profile/min_score/domain on recall, and used_ids/recall_id on reinforce (where "
            "facade delta defaults to 1.0). Adapter bridges these via raw.memory generated models "
            "(wrapping SessionId and rejecting float valence/arousal where RememberRequestModel "
            "enforces StrictInt), while P0-09 features (get_by_key, delete_by_key, "
            "metadata_filter, list_namespaces) and recall tier/tags/session_id filters raise "
            "UnsupportedCapabilityError when absent.",
        ),
    )
