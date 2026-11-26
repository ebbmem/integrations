"""Synchronous adapter wrapping released ebbmem-client (0.2.0+) behind IC-1 invariants."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ebbmem_integration_core.errors import (
    EbbmemApiError,
    UnsupportedCapabilityError,
    ValidationError,
)
from ebbmem_integration_core.reinforce import DEFAULT_REINFORCE_DELTA, validate_delta
from ebbmem_integration_core.retry import validate_idempotency_key
from ebbmem_integration_core.sdk_common import (
    _detect_sdk_capabilities,
    _load_sdk_models,
    _model_field_accepts_float,
    _to_plain_dict,
    _translate_sdk_exception,
    _validate_sdk_config,
)
from ebbmem_integration_core.security import redact_secrets
from ebbmem_integration_core.transport_types import (
    HttpResponse,
    _get_header_ci,
    _http_error_from_response,
    _merge_headers_ci,
)


class EbbmemClientSdkAdapter:
    """Thin adapter wrapping released sync ebbmem-client (0.2.0+) behind IC-1 invariants."""

    def __init__(self, sdk_client: Any) -> None:
        _validate_sdk_config(sdk_client)
        self._sdk = sdk_client
        self._models = _load_sdk_models()
        self.capabilities = _detect_sdk_capabilities(sdk_client, self._models)

    def __repr__(self) -> str:
        return f"EbbmemClientSdkAdapter(has_raw={getattr(self._sdk, 'raw', None) is not None})"

    def _invoke(
        self,
        name: str,
        op: Callable[[dict[str, str]], Any],
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        invoke_fn = getattr(self._sdk, "_invoke", None)
        try:
            if callable(invoke_fn):
                return invoke_fn(name, op, idempotency_key=idempotency_key)
            return op({})
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def remember(
        self,
        *,
        content: str,
        namespace: str | None = None,
        tier: str = "semantic",
        tags: Sequence[str] | None = None,
        importance: float | None = None,
        valence: float | None = None,
        arousal: float | None = None,
        session_id: str | None = None,
        timestamp_ms: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if idempotency_key is not None:
            validate_idempotency_key(idempotency_key)
        header_idem_key = _get_header_ci(headers, "idempotency-key")
        effective_idempotency_key = (
            idempotency_key
            if idempotency_key is not None
            else (header_idem_key if isinstance(header_idem_key, str) and header_idem_key else None)
        )
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "remember", None)):
            req_cls = self._models.get("RememberRequestModel")
            session_id_cls = self._models.get("SessionId")
            if isinstance(valence, bool) or isinstance(arousal, bool):
                raise ValidationError("valence and arousal must be numeric, not bool")
            if req_cls is not None:
                if (
                    valence is not None
                    and isinstance(valence, float)
                    and not _model_field_accepts_float(req_cls, "valence")
                ) or (
                    arousal is not None
                    and isinstance(arousal, float)
                    and not _model_field_accepts_float(req_cls, "arousal")
                ):
                    raise UnsupportedCapabilityError(
                        "Released ebbmem-client 0.2.0 RememberRequestModel requires integer "
                        "valence [-128, 127] and arousal [0, 255]; float values are unsupported "
                        "(use StdlibHttpTransport)"
                    )
            effective_session_id: Any = session_id
            if session_id is not None and session_id_cls is not None:
                if not isinstance(session_id, session_id_cls):
                    try:
                        effective_session_id = session_id_cls(session_id)
                    except ValueError as exc:
                        raise ValidationError(redact_secrets(str(exc))) from exc
            payload: dict[str, Any] = {
                "content": content,
                "tier": tier,
                "tags": list(tags) if tags is not None else None,
                "importance": importance,
                "valence": valence,
                "arousal": arousal,
                "session_id": effective_session_id,
                "timestamp_ms": timestamp_ms,
                "namespace": namespace,
                "metadata": dict(metadata) if metadata is not None else None,
                "idempotency_key": idempotency_key,
            }
            clean_payload = {k: v for k, v in payload.items() if v is not None}
            if req_cls is not None:
                try:
                    model = req_cls(**clean_payload)
                except ValueError as exc:
                    raise ValidationError(redact_secrets(str(exc))) from exc
            else:
                model = clean_payload
            result = self._invoke(
                "remember",
                lambda hdrs: raw_mem.remember(
                    model,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
                idempotency_key=effective_idempotency_key,
            )
            return _to_plain_dict(result)

        if (
            any(
                x is not None
                for x in (valence, arousal, session_id, timestamp_ms, metadata, namespace)
            )
            or tier != "semantic"
            or bool(headers)
        ):
            raise UnsupportedCapabilityError(
                "Wrapped SDK lacks raw.memory.remember needed for IC-1 remember fields/headers"
            )
        try:
            result = self._sdk.memory.remember(
                content,
                tags=list(tags) if tags is not None else None,
                importance=importance,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
        return _to_plain_dict(result)

    def recall(
        self,
        *,
        query: str,
        namespace: str | None = None,
        limit: int = 10,
        tier: str | None = None,
        tags: Sequence[str] | None = None,
        session_id: str | None = None,
        profile: str | None = None,
        min_score: float | None = None,
        domain: str | None = None,
        timeout_s: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if (tier is not None or tags or session_id is not None) and (
            not self.capabilities.recall_filters
        ):
            raise UnsupportedCapabilityError(
                "Released ebbmem-client 0.2.0 RecallRequestModel lacks tier/tags/session_id filters"
            )
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "recall", None)):
            req_cls = self._models.get("RecallRequestModel")
            payload: dict[str, Any] = {
                "query": query,
                "limit": limit,
                "reinforce": False,
                "profile": profile,
                "min_score": min_score,
                "domain": domain,
                "namespace": namespace,
            }
            if self.capabilities.recall_filters:
                if tier is not None:
                    payload["tier"] = tier
                if tags:
                    payload["tags"] = list(tags)
                if session_id is not None:
                    payload["session_id"] = session_id
            clean_payload = {k: v for k, v in payload.items() if v is not None}
            model = req_cls(**clean_payload) if req_cls is not None else clean_payload
            result = self._invoke(
                "recall",
                lambda hdrs: raw_mem.recall(
                    model,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                    _request_timeout=timeout_s,
                ),
            )
            return _to_plain_dict(result)

        if (
            any(
                x is not None
                for x in (namespace, profile, min_score, domain, tier, session_id, timeout_s)
            )
            or bool(tags)
            or bool(headers)
        ):
            raise UnsupportedCapabilityError(
                "Wrapped SDK lacks raw.memory.recall needed for IC-1 recall fields/headers"
            )
        try:
            result = self._sdk.memory.recall(query, limit=limit)
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
        return _to_plain_dict(result)

    def reinforce(
        self,
        *,
        memory_id: str,
        used_ids: Sequence[str] = (),
        recall_id: str | None = None,
        delta: float = DEFAULT_REINFORCE_DELTA,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        validated_delta = validate_delta(delta)
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "reinforce", None)):
            req_cls = self._models.get("ReinforceRequestModel")
            mem_id_cls = self._models.get("MemoryId")
            used_inner_cls = self._models.get("UsedIdsInner")
            if req_cls is not None and mem_id_cls is not None:
                try:
                    payload: dict[str, Any] = {
                        "memory_id": mem_id_cls(memory_id),
                        "delta": validated_delta,
                        "used_ids": (
                            [used_inner_cls(uid) for uid in used_ids]
                            if used_inner_cls is not None and used_ids
                            else (list(used_ids) if used_ids else None)
                        ),
                        "recall_id": recall_id,
                        "namespace": namespace,
                    }
                    clean_payload = {k: v for k, v in payload.items() if v is not None}
                    model = req_cls(**clean_payload)
                except ValueError as exc:
                    raise ValidationError(redact_secrets(str(exc))) from exc
            else:
                model = {
                    "memory_id": memory_id,
                    "delta": validated_delta,
                    **({"used_ids": list(used_ids)} if used_ids else {}),
                    **({"recall_id": recall_id} if recall_id is not None else {}),
                    **({"namespace": namespace} if namespace is not None else {}),
                }
            result = self._invoke(
                "reinforce",
                lambda hdrs: raw_mem.reinforce(
                    model,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
            )
            return _to_plain_dict(result)

        if used_ids or recall_id is not None or namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Released ebbmem-client 0.2.0 facade lacks used_ids/recall_id/namespace/headers "
                "on reinforce"
            )
        try:
            result = self._sdk.memory.reinforce(memory_id, delta=validated_delta)
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
        return _to_plain_dict(result)

    def get(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "get_memory", None)):
            result = self._invoke(
                "get",
                lambda hdrs: raw_mem.get_memory(
                    memory_id,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
            )
            return _to_plain_dict(result)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped SDK lacks raw.memory.get_memory needed for namespace/headers"
            )
        try:
            return _to_plain_dict(self._sdk.memory.get(memory_id))
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def forget(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "forget_memory", None)):
            result = self._invoke(
                "forget",
                lambda hdrs: raw_mem.forget_memory(
                    memory_id,
                    purge=False,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
            )
            return _to_plain_dict(result)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped SDK lacks raw.memory.forget_memory needed for namespace/headers"
            )
        try:
            return _to_plain_dict(self._sdk.memory.forget(memory_id, purge=False))
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def purge(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "purge_memory", None)):
            purge_raw = getattr(raw_mem, "purge_memory_without_preload_content", None)
            if callable(purge_raw) and hasattr(raw_mem, "api_client"):

                def _op_purge_raw(hdrs: dict[str, str]) -> dict[str, Any]:
                    resp = purge_raw(
                        memory_id,
                        x_ebb_namespace=namespace,
                        _headers=_merge_headers_ci(hdrs, headers),
                    )
                    raw_bytes = resp.read()
                    status_code = getattr(resp, "status", 200)
                    text = (
                        raw_bytes.decode("utf-8", errors="replace")
                        if isinstance(raw_bytes, bytes)
                        else (raw_bytes or "")
                    )
                    if not 200 <= status_code <= 299:
                        try:
                            from ebbmem_client.generated.exceptions import ApiException

                            raise ApiException.from_response(
                                http_resp=resp,
                                body=text,
                                data=None,
                            )
                        except ImportError:
                            resp_headers = {
                                str(k): str(v) for k, v in getattr(resp, "headers", {}).items()
                            }
                            try:
                                err_body: Any = json.loads(text) if text else None
                            except json.JSONDecodeError:
                                err_body = {"raw": redact_secrets(text)}
                            raise _http_error_from_response(
                                HttpResponse(
                                    status=status_code, headers=resp_headers, body=err_body
                                )
                            ) from None
                    parsed = json.loads(text) if text else {}
                    out = _to_plain_dict(parsed)
                    if "status" not in out and out.get("found") is not False:
                        out["status"] = "purged"
                    if "irreversible" not in out and out.get("status") == "purged":
                        out["irreversible"] = True
                    return out

                return self._invoke("purge", _op_purge_raw)

            result = self._invoke(
                "purge",
                lambda hdrs: raw_mem.purge_memory(
                    memory_id,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
            )
            out = _to_plain_dict(result)
            if "status" not in out and out.get("found") is not False:
                out["status"] = "purged"
            if "irreversible" not in out and out.get("status") == "purged":
                out["irreversible"] = True
            return out
        if raw_mem is not None and callable(getattr(raw_mem, "forget_memory", None)):
            result = self._invoke(
                "purge",
                lambda hdrs: raw_mem.forget_memory(
                    memory_id,
                    purge=True,
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
            )
            return _to_plain_dict(result)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped SDK lacks raw.memory.forget_memory needed for namespace/headers"
            )
        try:
            return _to_plain_dict(self._sdk.memory.forget(memory_id, purge=True))
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def status(
        self,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        raw_mem = getattr(getattr(self._sdk, "raw", None), "memory", None)
        if raw_mem is not None and callable(getattr(raw_mem, "status", None)):
            result = self._invoke(
                "status",
                lambda hdrs: raw_mem.status(
                    x_ebb_namespace=namespace,
                    _headers=_merge_headers_ci(hdrs, headers),
                ),
            )
            return _to_plain_dict(result)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped SDK lacks raw.memory.status needed for namespace/headers"
            )
        try:
            return _to_plain_dict(self._sdk.memory.status())
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def health(self) -> dict[str, Any]:
        try:
            return _to_plain_dict(self._sdk.health())
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def get_by_key(self, key: str, *, namespace: str | None = None) -> dict[str, Any]:
        if not self.capabilities.exact_key_get:
            raise UnsupportedCapabilityError(
                "get_by_key requires P0-09 exact-key APIs not present in ebbmem-client 0.2.0"
            )
        fn = getattr(getattr(self._sdk, "memory", None), "get_by_key", None) or getattr(
            getattr(getattr(self._sdk, "raw", None), "memory", None), "get_by_key", None
        )
        if not callable(fn):
            raise UnsupportedCapabilityError(
                "get_by_key requires P0-09 exact-key APIs not present in ebbmem-client 0.2.0"
            )
        try:
            return _to_plain_dict(fn(key, namespace=namespace))
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def delete_by_key(self, key: str, *, namespace: str | None = None) -> dict[str, Any]:
        if not self.capabilities.exact_key_delete:
            raise UnsupportedCapabilityError(
                "delete_by_key requires P0-09 exact-key APIs not present in ebbmem-client 0.2.0"
            )
        fn = getattr(getattr(self._sdk, "memory", None), "delete_by_key", None) or getattr(
            getattr(getattr(self._sdk, "raw", None), "memory", None), "delete_by_key", None
        )
        if not callable(fn):
            raise UnsupportedCapabilityError(
                "delete_by_key requires P0-09 exact-key APIs not present in ebbmem-client 0.2.0"
            )
        try:
            return _to_plain_dict(fn(key, namespace=namespace))
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    def list_namespaces(self, *, prefix: str | None = None) -> list[str]:
        if not self.capabilities.namespace_prefix_list:
            raise UnsupportedCapabilityError(
                "list_namespaces requires P0-09 namespace-prefix API not in ebbmem-client 0.2.0"
            )
        fn = getattr(getattr(self._sdk, "memory", None), "list_namespaces", None) or getattr(
            getattr(getattr(self._sdk, "raw", None), "memory", None), "list_namespaces", None
        )
        if not callable(fn):
            raise UnsupportedCapabilityError(
                "list_namespaces requires P0-09 namespace-prefix API not in ebbmem-client 0.2.0"
            )
        try:
            return list(fn(prefix=prefix))
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
