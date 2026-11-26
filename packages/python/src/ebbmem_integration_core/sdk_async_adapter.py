"""Asynchronous adapter wrapping AsyncEbbmemClient (0.2.0+) behind IC-1 invariants."""

from __future__ import annotations

import inspect
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any

from ebbmem_integration_core.errors import EbbmemApiError, UnsupportedCapabilityError
from ebbmem_integration_core.reinforce import DEFAULT_REINFORCE_DELTA, validate_delta
from ebbmem_integration_core.retry import validate_idempotency_key
from ebbmem_integration_core.sdk_common import (
    _detect_sdk_capabilities,
    _load_sdk_models,
    _to_plain_dict,
    _translate_sdk_exception,
    _validate_sdk_config,
)
from ebbmem_integration_core.transport_types import _get_header_ci


class AsyncEbbmemClientSdkAdapter:
    """Async counterpart to EbbmemClientSdkAdapter wrapping AsyncEbbmemClient (0.2.0+)."""

    def __init__(self, async_sdk_client: Any) -> None:
        _validate_sdk_config(async_sdk_client)
        self._sdk = async_sdk_client
        self._models = _load_sdk_models()
        self.capabilities = _detect_sdk_capabilities(async_sdk_client, self._models)

    def __repr__(self) -> str:
        has_inv = callable(getattr(self._sdk, "_invoke", None))
        return f"AsyncEbbmemClientSdkAdapter(has_invoke={has_inv})"

    async def _invoke_rest(
        self,
        name: str,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        invoke_fn = getattr(self._sdk, "_invoke", None)
        if not callable(invoke_fn):
            raise UnsupportedCapabilityError(
                "Async SDK adapter requires AsyncEbbmemClient._invoke or compatible async memory"
            )
        extra_kwargs: dict[str, Any] = {}
        sig = inspect.signature(invoke_fn)
        accepts_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if headers:
            if "headers" in sig.parameters or accepts_var_kw:
                extra_kwargs["headers"] = dict(headers)
            else:
                raise UnsupportedCapabilityError(
                    "Wrapped AsyncEbbmemClient._invoke does not support custom headers"
                )
        if timeout_s is not None:
            if "timeout_s" in sig.parameters or accepts_var_kw:
                extra_kwargs["timeout_s"] = timeout_s
            elif "timeout" in sig.parameters:
                extra_kwargs["timeout"] = timeout_s
            else:
                raise UnsupportedCapabilityError(
                    "Wrapped AsyncEbbmemClient._invoke does not support per-call timeout_s"
                )
        try:
            return await invoke_fn(
                name,
                method,
                path,
                json_body=json_body,
                params=params,
                idempotency_key=idempotency_key,
                **extra_kwargs,
            )
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def remember(
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
        if callable(getattr(self._sdk, "_invoke", None)):
            body: dict[str, Any] = {"content": content, "tier": tier}
            if tags is not None:
                body["tags"] = list(tags)
            if importance is not None:
                body["importance"] = importance
            if valence is not None:
                body["valence"] = valence
            if arousal is not None:
                body["arousal"] = arousal
            if session_id is not None:
                body["session_id"] = session_id
            if timestamp_ms is not None:
                body["timestamp_ms"] = timestamp_ms
            if namespace is not None:
                body["namespace"] = namespace
            if metadata is not None:
                body["metadata"] = dict(metadata)
            if idempotency_key is not None:
                body["idempotency_key"] = idempotency_key
            res = await self._invoke_rest(
                "remember",
                "POST",
                "/api/v1/memory/remember",
                json_body=body,
                headers=headers,
                idempotency_key=effective_idempotency_key,
            )
            return _to_plain_dict(res)

        if (
            any(
                x is not None
                for x in (valence, arousal, session_id, timestamp_ms, metadata, namespace)
            )
            or tier != "semantic"
            or bool(headers)
        ):
            raise UnsupportedCapabilityError(
                "Wrapped async SDK lacks _invoke needed for IC-1 remember fields/headers"
            )
        try:
            res = await self._sdk.memory.remember(
                content,
                tags=list(tags) if tags is not None else None,
                importance=importance,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
        return _to_plain_dict(res)

    async def recall(
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
        if callable(getattr(self._sdk, "_invoke", None)):
            body: dict[str, Any] = {"query": query, "limit": limit, "reinforce": False}
            if profile is not None:
                body["profile"] = profile
            if min_score is not None:
                body["min_score"] = min_score
            if domain is not None:
                body["domain"] = domain
            if namespace is not None:
                body["namespace"] = namespace
            if self.capabilities.recall_filters:
                if tier is not None:
                    body["tier"] = tier
                if tags:
                    body["tags"] = list(tags)
                if session_id is not None:
                    body["session_id"] = session_id
            res = await self._invoke_rest(
                "recall",
                "POST",
                "/api/v1/memory/recall",
                json_body=body,
                headers=headers,
                timeout_s=timeout_s,
            )
            return _to_plain_dict(res)

        if (
            any(
                x is not None
                for x in (namespace, profile, min_score, domain, tier, session_id, timeout_s)
            )
            or bool(tags)
            or bool(headers)
        ):
            raise UnsupportedCapabilityError(
                "Wrapped async SDK lacks _invoke needed for IC-1 recall fields/headers"
            )
        try:
            res = await self._sdk.memory.recall(query, limit=limit)
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
        return _to_plain_dict(res)

    async def reinforce(
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
        if callable(getattr(self._sdk, "_invoke", None)):
            body: dict[str, Any] = {"memory_id": memory_id, "delta": validated_delta}
            if used_ids:
                body["used_ids"] = list(used_ids)
            if recall_id is not None:
                body["recall_id"] = recall_id
            if namespace is not None:
                body["namespace"] = namespace
            res = await self._invoke_rest(
                "reinforce",
                "POST",
                "/api/v1/memory/reinforce",
                json_body=body,
                headers=headers,
            )
            return _to_plain_dict(res)

        if used_ids or recall_id is not None or namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Released ebbmem-client 0.2.0 facade lacks used_ids/recall_id/namespace/headers "
                "on reinforce"
            )
        try:
            res = await self._sdk.memory.reinforce(memory_id, delta=validated_delta)
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
        return _to_plain_dict(res)

    async def get(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if callable(getattr(self._sdk, "_invoke", None)):
            params = {"namespace": namespace} if namespace is not None else None
            encoded = urllib.parse.quote(memory_id, safe="")
            res = await self._invoke_rest(
                "get",
                "GET",
                f"/api/v1/memory/{encoded}",
                params=params,
                headers=headers,
            )
            return _to_plain_dict(res)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped async SDK lacks _invoke needed for get namespace/headers"
            )
        try:
            return _to_plain_dict(await self._sdk.memory.get(memory_id))
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def forget(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if callable(getattr(self._sdk, "_invoke", None)):
            params = {"namespace": namespace} if namespace is not None else None
            encoded = urllib.parse.quote(memory_id, safe="")
            res = await self._invoke_rest(
                "forget",
                "DELETE",
                f"/api/v1/memory/{encoded}",
                params=params,
                headers=headers,
            )
            return _to_plain_dict(res)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped async SDK lacks _invoke needed for forget namespace/headers"
            )
        try:
            return _to_plain_dict(await self._sdk.memory.forget(memory_id, purge=False))
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def purge(
        self,
        memory_id: str,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if callable(getattr(self._sdk, "_invoke", None)):
            params = {"namespace": namespace} if namespace is not None else None
            encoded = urllib.parse.quote(memory_id, safe="")
            res = await self._invoke_rest(
                "purge",
                "DELETE",
                f"/api/v1/memory/{encoded}/purge",
                params=params,
                headers=headers,
            )
            out = _to_plain_dict(res)
            if isinstance(out, dict) and "status" not in out and out.get("found") is not False:
                out["status"] = "purged"
                out.setdefault("irreversible", True)
            elif isinstance(out, dict) and out.get("status") == "purged":
                out.setdefault("irreversible", True)
            return out
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped async SDK lacks _invoke needed for purge namespace/headers"
            )
        try:
            out = _to_plain_dict(await self._sdk.memory.forget(memory_id, purge=True))
            if isinstance(out, dict) and "status" not in out and out.get("found") is not False:
                out["status"] = "purged"
                out.setdefault("irreversible", True)
            elif isinstance(out, dict) and out.get("status") == "purged":
                out.setdefault("irreversible", True)
            return out
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def status(
        self,
        *,
        namespace: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if callable(getattr(self._sdk, "_invoke", None)):
            params = {"namespace": namespace} if namespace is not None else None
            res = await self._invoke_rest(
                "status",
                "GET",
                "/api/v1/memory/status",
                params=params,
                headers=headers,
            )
            return _to_plain_dict(res)
        if namespace is not None or bool(headers):
            raise UnsupportedCapabilityError(
                "Wrapped async SDK lacks _invoke needed for status namespace/headers"
            )
        try:
            return _to_plain_dict(await self._sdk.memory.status())
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def health(self) -> dict[str, Any]:
        try:
            return _to_plain_dict(await self._sdk.health())
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def get_by_key(self, key: str, *, namespace: str | None = None) -> dict[str, Any]:
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
            res = fn(key, namespace=namespace)
            if inspect.isawaitable(res):
                res = await res
            return _to_plain_dict(res)
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def delete_by_key(self, key: str, *, namespace: str | None = None) -> dict[str, Any]:
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
            res = fn(key, namespace=namespace)
            if inspect.isawaitable(res):
                res = await res
            return _to_plain_dict(res)
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc

    async def list_namespaces(self, *, prefix: str | None = None) -> list[str]:
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
            res = fn(prefix=prefix)
            if inspect.isawaitable(res):
                res = await res
            return list(res)
        except EbbmemApiError:
            raise
        except Exception as exc:
            raise _translate_sdk_exception(exc) from exc
