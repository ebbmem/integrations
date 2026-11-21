"""Unit tests for EbbmemClientSdkAdapter and AsyncEbbmemClientSdkAdapter (0.2.0 gaps & bridges)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Annotated, Any

import pytest

from ebbmem_integration_core.errors import (
    ApiValidationError,
    CredentialConflictError,
    CredentialInUrlError,
    EbbmemApiError,
    IdempotencyConflictError,
    UnsupportedCapabilityError,
    ValidationError,
)
from ebbmem_integration_core.transport import (
    AsyncEbbmemClientSdkAdapter,
    EbbmemClientSdkAdapter,
    HttpResponse,
    _http_error_from_response,
    _translate_sdk_exception,
)


@dataclass
class _FakeConfig:
    base_url: str
    token: str | None = None
    api_key: str | None = None


class _FakeRawMemory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    def remember(self, body: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("remember", body, kwargs))
        return {"id": "m-1", "tier": body["tier"], "session_id": body.get("session_id")}

    def recall(self, body: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("recall", body, kwargs))
        return {
            "query": body["query"],
            "count": 1,
            "recall_id": "rec-1",
            "results": [{"memory_id": "m-1", "content": "hi", "score": 0.9}],
        }

    def reinforce(self, body: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("reinforce", body, kwargs))
        return {
            "status": "reinforced",
            "outcome": "counted",
            "memory_id": body["memory_id"],
            "delta": body["delta"],
            "used_ids": body.get("used_ids"),
            "recall_id": body.get("recall_id"),
        }

    def get_memory(self, memory_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_memory", memory_id, kwargs))
        return {"id": memory_id}

    def forget_memory(self, memory_id: str, purge: bool = False, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("forget_memory", (memory_id, purge), kwargs))
        return {"status": "purged" if purge else "forgotten", "memory_id": memory_id}

    def purge_memory(self, memory_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("purge_memory", memory_id, kwargs))
        return {"status": "purged", "memory_id": memory_id, "irreversible": True}

    def status(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("status", None, kwargs))
        return {"status": "ok"}


class _FakeRaw:
    def __init__(self) -> None:
        self.memory = _FakeRawMemory()


class _FakeSyncSdk:
    def __init__(
        self,
        base_url: str = "https://example.invalid",
        *,
        token: str | None = None,
        api_key: str | None = "ebb_k1",
    ) -> None:
        self.config = _FakeConfig(base_url=base_url, token=token, api_key=api_key)
        self.raw = _FakeRaw()
        self.memory = object()

    def _invoke(self, name: str, op: Any, *, idempotency_key: str | None = None) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return op(headers)

    def health(self) -> dict[str, Any]:
        return {"status": "ok"}


class _FakeAsyncSdk:
    def __init__(self) -> None:
        self.config = _FakeConfig(base_url="https://example.invalid", api_key="ebb_k1")
        self.raw = _FakeRaw()
        self.memory = object()
        self.calls: list[tuple[str, str, str, Any, dict[str, Any] | None]] = []

    async def _invoke(
        self,
        name: str,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        del idempotency_key
        self.calls.append((name, method, path, json_body, params))
        if name == "reinforce":
            return {"status": "reinforced", "outcome": "counted", "delta": json_body["delta"]}
        return {"ok": True, "path": path, "body": json_body}

    async def health(self) -> dict[str, Any]:
        return {"status": "ok"}


class _FakeAsyncInvokeWithHeadersSdk(_FakeAsyncSdk):
    def __init__(self) -> None:
        super().__init__()
        self.extra_calls: list[tuple[dict[str, str] | None, float | None]] = []

    async def _invoke(
        self,
        name: str,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        headers: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        self.extra_calls.append((headers, timeout_s))
        return await super()._invoke(
            name,
            method,
            path,
            json_body=json_body,
            params=params,
            idempotency_key=idempotency_key,
        )


class _FakeFacadeMemory:
    def remember(self, content: str, **_kwargs: Any) -> dict[str, Any]:
        return {"id": "m-facade", "content": content}

    def recall(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        return {"query": query, "count": 0, "limit": limit, "results": []}

    def reinforce(self, memory_id: str, *, delta: float = 0.1) -> dict[str, Any]:
        return {"memory_id": memory_id, "delta": delta}

    def get(self, memory_id: str) -> dict[str, Any]:
        return {"id": memory_id}

    def forget(self, memory_id: str, *, purge: bool = False) -> dict[str, Any]:
        return {"id": memory_id, "status": "purged" if purge else "forgotten"}

    def status(self) -> dict[str, Any]:
        return {"status": "ok"}


class _FakeFacadeSyncSdk:
    def __init__(self) -> None:
        self.config = _FakeConfig(base_url="https://example.invalid", api_key="ebb_k1")
        self.memory = _FakeFacadeMemory()

    def health(self) -> dict[str, Any]:
        return {"status": "ok"}


class _FakeFacadeAsyncMemory:
    async def remember(self, content: str, **_kwargs: Any) -> dict[str, Any]:
        return {"id": "m-async-facade", "content": content}

    async def recall(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        return {"query": query, "count": 0, "limit": limit, "results": []}

    async def reinforce(self, memory_id: str, *, delta: float = 0.1) -> dict[str, Any]:
        return {"memory_id": memory_id, "delta": delta}

    async def get(self, memory_id: str) -> dict[str, Any]:
        return {"id": memory_id}

    async def forget(self, memory_id: str, *, purge: bool = False) -> dict[str, Any]:
        return {"id": memory_id, "status": "purged" if purge else "forgotten"}

    async def status(self) -> dict[str, Any]:
        return {"status": "ok"}


class _FakeFacadeAsyncSdk:
    def __init__(self) -> None:
        self.config = _FakeConfig(base_url="https://example.invalid", api_key="ebb_k1")
        self.memory = _FakeFacadeAsyncMemory()

    async def health(self) -> dict[str, Any]:
        return {"status": "ok"}


def test_sdk_adapter_enforces_url_and_dual_auth_safety() -> None:
    with pytest.raises(CredentialInUrlError):
        EbbmemClientSdkAdapter(_FakeSyncSdk(base_url="https://example.invalid?token=secret"))
    with pytest.raises(CredentialConflictError):
        EbbmemClientSdkAdapter(_FakeSyncSdk(token="tok_1", api_key="ebb_k1"))


def test_sdk_adapter_bridges_020_gaps_and_flags_p009_unsupported() -> None:
    sdk = _FakeSyncSdk()
    adapter = EbbmemClientSdkAdapter(sdk)

    assert adapter.capabilities.tier_on_remember is True
    assert adapter.capabilities.reinforce_default_delta_0_1 is True
    assert adapter.capabilities.exact_key_get is False
    assert adapter.capabilities.exact_key_delete is False
    assert adapter.capabilities.metadata_filter_on_list_recall is False
    assert adapter.capabilities.namespace_prefix_list is False

    rem = adapter.remember(
        content="hi",
        namespace="acme:u:alice",
        tier="episodic",
        session_id="sess-1",
        metadata={"k": "v"},
        idempotency_key="ebb_idem_1",
    )
    assert rem["tier"] == "episodic"
    assert rem["session_id"] == "sess-1"

    # Recall passes profile/min_score/domain while flagging missing 0.2.0 filters
    rec = adapter.recall(
        query="hi",
        namespace="acme:u:alice",
        profile="core",
        min_score=0.4,
        domain="ops",
    )
    assert rec["recall_id"] == "rec-1"
    with pytest.raises(UnsupportedCapabilityError):
        adapter.recall(query="hi", tier="semantic")

    # Reinforce defaults delta to 0.1 (NOT 1.0) and forwards used_ids & recall_id
    reinf = adapter.reinforce(
        memory_id="m-1",
        used_ids=["m-2"],
        recall_id="rec-1",
        namespace="acme:u:alice",
    )
    assert reinf["delta"] == pytest.approx(0.1)
    assert reinf["used_ids"] == ["m-2"]
    assert reinf["recall_id"] == "rec-1"

    assert adapter.forget("m-1", namespace="acme:u:alice")["status"] == "forgotten"
    purged = adapter.purge("m-1", namespace="acme:u:alice")
    assert purged["status"] == "purged"
    assert purged["irreversible"] is True
    assert any(c[0] == "purge_memory" and c[1] == "m-1" for c in sdk.raw.memory.calls)
    assert not any(c[0] == "forget_memory" and c[1] == ("m-1", True) for c in sdk.raw.memory.calls)

    with pytest.raises(UnsupportedCapabilityError):
        adapter.get_by_key("ext-1")
    with pytest.raises(UnsupportedCapabilityError):
        adapter.delete_by_key("ext-1")
    with pytest.raises(UnsupportedCapabilityError):
        adapter.list_namespaces(prefix="acme:")


def test_async_sdk_adapter_parity() -> None:
    async def run() -> None:
        sdk = _FakeAsyncSdk()
        adapter = AsyncEbbmemClientSdkAdapter(sdk)
        rem = await adapter.remember(
            content="async hi",
            namespace="acme:u:alice",
            tier="procedural",
            session_id="s-async",
        )
        assert rem["body"]["tier"] == "procedural"
        reinf = await adapter.reinforce(
            memory_id="m-1",
            used_ids=["m-2"],
            recall_id="rec-a",
        )
        assert reinf["delta"] == pytest.approx(0.1)

        await adapter.forget("m-1", namespace="acme:u:alice")
        forget_call = next(c for c in sdk.calls if c[0] == "forget")
        assert forget_call[4] == {"namespace": "acme:u:alice"}

        with pytest.raises(UnsupportedCapabilityError):
            await adapter.remember(content="hi", headers={"X-Custom": "1"})
        with pytest.raises(UnsupportedCapabilityError):
            await adapter.recall(query="hi", timeout_s=1.5)

        with pytest.raises(UnsupportedCapabilityError):
            await adapter.get_by_key("ext-1")
        with pytest.raises(UnsupportedCapabilityError):
            await adapter.delete_by_key("ext-1")
        with pytest.raises(UnsupportedCapabilityError):
            await adapter.list_namespaces(prefix="acme:")

        assert (await adapter.health())["status"] == "ok"

        hdr_sdk = _FakeAsyncInvokeWithHeadersSdk()
        hdr_adapter = AsyncEbbmemClientSdkAdapter(hdr_sdk)
        await hdr_adapter.recall(query="q", headers={"X-Test": "ok"}, timeout_s=2.5)
        assert hdr_sdk.extra_calls[-1] == ({"X-Test": "ok"}, 2.5)

    asyncio.run(run())


def test_facade_only_sync_and_async_adapters_reject_unsupported_fields() -> None:
    sync_adapter = EbbmemClientSdkAdapter(_FakeFacadeSyncSdk())
    assert sync_adapter.remember(content="ok")["id"] == "m-facade"
    assert sync_adapter.recall(query="ok")["query"] == "ok"
    with pytest.raises(UnsupportedCapabilityError):
        sync_adapter.remember(content="bad", namespace="acme:u:alice")
    with pytest.raises(UnsupportedCapabilityError):
        sync_adapter.recall(query="bad", namespace="acme:u:alice")
    with pytest.raises(UnsupportedCapabilityError):
        sync_adapter.recall(query="bad", profile="core")
    with pytest.raises(UnsupportedCapabilityError):
        sync_adapter.reinforce(memory_id="m1", namespace="acme:u:alice")
    with pytest.raises(UnsupportedCapabilityError):
        sync_adapter.get("m1", namespace="acme:u:alice")
    with pytest.raises(UnsupportedCapabilityError):
        sync_adapter.forget("m1", headers={"X-Test": "1"})

    async def run() -> None:
        async_adapter = AsyncEbbmemClientSdkAdapter(_FakeFacadeAsyncSdk())
        assert (await async_adapter.remember(content="ok"))["id"] == "m-async-facade"
        assert (await async_adapter.recall(query="ok"))["query"] == "ok"
        with pytest.raises(UnsupportedCapabilityError):
            await async_adapter.remember(content="bad", namespace="acme:u:alice")
        with pytest.raises(UnsupportedCapabilityError):
            await async_adapter.remember(content="bad", tier="episodic")
        with pytest.raises(UnsupportedCapabilityError):
            await async_adapter.recall(query="bad", namespace="acme:u:alice")
        with pytest.raises(UnsupportedCapabilityError):
            await async_adapter.recall(query="bad", profile="core")
        with pytest.raises(UnsupportedCapabilityError):
            await async_adapter.reinforce(memory_id="m1", namespace="acme:u:alice")
        with pytest.raises(UnsupportedCapabilityError):
            await async_adapter.get("m1", namespace="acme:u:alice")
        with pytest.raises(UnsupportedCapabilityError):
            await async_adapter.forget("m1", headers={"X-Test": "1"})

    asyncio.run(run())


class _FakeSdkHttpError(Exception):
    def __init__(self, status: int, code: str | None, message: str = "conflict") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def test_sdk_adapter_maps_400_and_422_idempotency_conflict_errors() -> None:
    class _ErrSyncSdk(_FakeSyncSdk):
        def _invoke(self, name: str, op: Any, *, idempotency_key: str | None = None) -> Any:
            del name, op
            if idempotency_key == "ebb_400":
                raise _FakeSdkHttpError(400, "EBB-100-021", "idempotency key reuse")
            if idempotency_key == "ebb_422":
                raise _FakeSdkHttpError(422, "EBB-100-029", "idempotency conflict")
            raise _FakeSdkHttpError(400, "EBB-100-001", "invalid field")

    sync_adapter = EbbmemClientSdkAdapter(_ErrSyncSdk())
    with pytest.raises(IdempotencyConflictError) as exc_400:
        sync_adapter.remember(content="hi", idempotency_key="ebb_400")
    assert exc_400.value.status == 400
    assert exc_400.value.code == "EBB-100-021"

    with pytest.raises(IdempotencyConflictError) as exc_422:
        sync_adapter.remember(content="hi", idempotency_key="ebb_422")
    assert exc_422.value.status == 422
    assert exc_422.value.code == "EBB-100-029"

    with pytest.raises(ApiValidationError) as exc_val:
        sync_adapter.remember(content="hi", idempotency_key="ebb_other")
    assert exc_val.value.status == 400
    assert exc_val.value.code == "EBB-100-001"

    class _ErrAsyncSdk(_FakeAsyncSdk):
        async def _invoke(
            self,
            name: str,
            method: str,
            path: str,
            *,
            json_body: Any = None,
            params: dict[str, Any] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            del name, method, path, json_body, params
            if idempotency_key == "ebb_400":
                raise _FakeSdkHttpError(400, "EBB-100-021", "idempotency key reuse")
            if idempotency_key == "ebb_422":
                raise _FakeSdkHttpError(422, "EBB-100-029", "idempotency conflict")
            raise _FakeSdkHttpError(422, "EBB-100-002", "validation error")

    async def run() -> None:
        async_adapter = AsyncEbbmemClientSdkAdapter(_ErrAsyncSdk())
        with pytest.raises(IdempotencyConflictError) as a_400:
            await async_adapter.remember(content="hi", idempotency_key="ebb_400")
        assert a_400.value.status == 400
        assert a_400.value.code == "EBB-100-021"

        with pytest.raises(IdempotencyConflictError) as a_422:
            await async_adapter.remember(content="hi", idempotency_key="ebb_422")
        assert a_422.value.status == 422
        assert a_422.value.code == "EBB-100-029"

        with pytest.raises(ApiValidationError) as a_val:
            await async_adapter.remember(content="hi", idempotency_key="ebb_other")
        assert a_val.value.status == 422
        assert a_val.value.code == "EBB-100-002"

    asyncio.run(run())


def test_sdk_adapter_purge_falls_back_to_forget_memory_when_purge_memory_absent() -> None:
    class _NoPurgeRawMemory:
        def __init__(self) -> None:
            self.calls: list[tuple[str, Any, dict[str, Any]]] = []

        def forget_memory(
            self, memory_id: str, purge: bool = False, **kwargs: Any
        ) -> dict[str, Any]:
            self.calls.append(("forget_memory", (memory_id, purge), kwargs))
            return {"status": "purged" if purge else "forgotten", "memory_id": memory_id}

    sdk = _FakeSyncSdk()
    sdk.raw.memory = _NoPurgeRawMemory()  # type: ignore[assignment]
    adapter = EbbmemClientSdkAdapter(sdk)
    res = adapter.purge("m-fb", namespace="acme:u:alice")
    assert res["status"] == "purged"
    assert sdk.raw.memory.calls[0][0] == "forget_memory"
    assert sdk.raw.memory.calls[0][1] == ("m-fb", True)


def test_sdk_adapter_wraps_session_id_and_handles_strict_int_valence_arousal() -> None:
    class _FakeSessionId:
        def __init__(self, value: str | int) -> None:
            self.actual_instance = value

    @dataclass
    class _FieldInfo:
        annotation: Any

    class _FakeRememberModel:
        model_fields: dict[str, Any] = {
            "valence": _FieldInfo(annotation=Annotated[int, "strict"] | None),
            "arousal": _FieldInfo(annotation=Annotated[int, "strict"] | None),
        }

        def __init__(
            self,
            *,
            content: str,
            tier: str = "semantic",
            session_id: Any = None,
            valence: int | None = None,
            arousal: int | None = None,
            **_extra: Any,
        ) -> None:
            if session_id is not None and not isinstance(session_id, _FakeSessionId):
                raise ValueError("session_id must be SessionId")
            if valence is not None and not isinstance(valence, int):
                raise ValueError("valence must be int")
            if arousal is not None and not isinstance(arousal, int):
                raise ValueError("arousal must be int")
            self.content = content
            self.tier = tier
            self.session_id = session_id
            self.valence = valence
            self.arousal = arousal

        def to_dict(self) -> dict[str, Any]:
            return {
                "content": self.content,
                "tier": self.tier,
                "session_id": self.session_id.actual_instance if self.session_id else None,
                "valence": self.valence,
                "arousal": self.arousal,
            }

    class _ModelRawMemory(_FakeRawMemory):
        def remember(self, body: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("remember", body, kwargs))
            return body.to_dict()

    sdk = _FakeSyncSdk()
    sdk.raw.memory = _ModelRawMemory()  # type: ignore[assignment]
    adapter = EbbmemClientSdkAdapter(sdk)
    adapter._models = {
        "RememberRequestModel": _FakeRememberModel,
        "SessionId": _FakeSessionId,
    }

    res = adapter.remember(content="hello", session_id="sess-wrapped", valence=12, arousal=200)
    assert res["session_id"] == "sess-wrapped"
    assert res["valence"] == 12
    assert res["arousal"] == 200
    recorded_model = sdk.raw.memory.calls[-1][1]
    assert isinstance(recorded_model, _FakeRememberModel)
    assert isinstance(recorded_model.session_id, _FakeSessionId)

    with pytest.raises(UnsupportedCapabilityError):
        adapter.remember(content="bad-valence", valence=0.5)
    with pytest.raises(UnsupportedCapabilityError):
        adapter.remember(content="bad-arousal", arousal=0.3)
    with pytest.raises(ValidationError):
        adapter.remember(content="bad-bool", valence=True)  # type: ignore[arg-type]


def test_ebb_100_023_maps_to_api_validation_error_across_http_and_sdk() -> None:
    err_023 = _http_error_from_response(
        HttpResponse(
            status=400,
            headers={},
            body={"code": "EBB-100-023", "detail": "reserved_metadata_prefix"},
        )
    )
    assert isinstance(err_023, ApiValidationError)
    assert not isinstance(err_023, IdempotencyConflictError)
    assert err_023.code == "EBB-100-023"

    for code, status in [("EBB-100-021", 400), ("EBB-100-024", 422), ("EBB-100-029", 422)]:
        idem_err = _http_error_from_response(
            HttpResponse(status=status, headers={}, body={"code": code, "detail": "idem"})
        )
        assert isinstance(idem_err, IdempotencyConflictError)
        assert idem_err.code == code

    sdk_023 = _translate_sdk_exception(_FakeSdkHttpError(400, "EBB-100-023", "reserved"))
    assert isinstance(sdk_023, ApiValidationError)
    assert not isinstance(sdk_023, IdempotencyConflictError)
    assert sdk_023.code == "EBB-100-023"

    sdk_024 = _translate_sdk_exception(_FakeSdkHttpError(422, "EBB-100-024", "mismatch"))
    assert isinstance(sdk_024, IdempotencyConflictError)
    assert sdk_024.code == "EBB-100-024"


def test_translate_sdk_exception_parses_body_causes_redaction_and_non_str_code() -> None:
    @dataclass
    class _ApiExc(Exception):
        status: int | None = None
        code: Any = None
        body: Any = None
        headers: dict[str, str] = field(default_factory=dict)
        msg: str = ""

        def __str__(self) -> str:
            return self.msg

    # JSON str .body
    str_exc = _ApiExc(
        status=422,
        body='{"code":"EBB-100-024","detail":"mismatch","api_key":"ebb_secret_123"}',
        msg="422",
    )
    t_str = _translate_sdk_exception(str_exc)
    assert isinstance(t_str, IdempotencyConflictError)
    assert t_str.code == "EBB-100-024"
    assert isinstance(t_str.body, dict)
    assert t_str.body["api_key"] == "[REDACTED]"

    # bytes JSON .body
    bytes_exc = _ApiExc(
        status=400,
        body=b'{"code":"EBB-100-023","detail":"reserved prefix","token":"sk-abcdefgh12345678"}',
        msg="400",
    )
    t_bytes = _translate_sdk_exception(bytes_exc)
    assert isinstance(t_bytes, ApiValidationError)
    assert t_bytes.code == "EBB-100-023"
    assert t_bytes.body["token"] == "[REDACTED]"

    # dict .body with nested error.code
    dict_exc = _ApiExc(
        status=422,
        body={"error": {"code": "EBB-100-024", "message": "nested mismatch"}},
        msg="",
    )
    t_dict = _translate_sdk_exception(dict_exc)
    assert isinstance(t_dict, IdempotencyConflictError)
    assert t_dict.code == "EBB-100-024"
    assert "nested mismatch" in str(t_dict)

    # __cause__ walking when outer lacks status/code/body
    outer = RuntimeError("wrapper failure")
    outer.__cause__ = _ApiExc(
        status=422,
        body='{"error":{"code":"EBB-100-024","message":"cause mismatch"}}',
        msg="inner",
    )
    t_cause = _translate_sdk_exception(outer)
    assert isinstance(t_cause, IdempotencyConflictError)
    assert t_cause.status == 422
    assert t_cause.code == "EBB-100-024"

    # Non-string .code normalization
    non_str_code_exc = _ApiExc(status=400, code=100021, msg="bad")
    t_nonstr = _translate_sdk_exception(non_str_code_exc)
    assert isinstance(t_nonstr, ApiValidationError)
    assert t_nonstr.code is None
    assert isinstance(t_nonstr, EbbmemApiError)


def test_python_sdk_adapters_forward_header_idempotency_key_to_invoke() -> None:
    seen_sync: list[tuple[str, str | None]] = []

    class _CaptureSyncSdk(_FakeSyncSdk):
        def _invoke(self, name: str, op: Any, *, idempotency_key: str | None = None) -> Any:
            seen_sync.append((name, idempotency_key))
            return super()._invoke(name, op, idempotency_key=idempotency_key)

    sync_adapter = EbbmemClientSdkAdapter(_CaptureSyncSdk())
    sync_adapter.remember(content="hi", headers={"idempotency-key": "ebb_hdr_sync_1"})
    assert seen_sync == [("remember", "ebb_hdr_sync_1")]

    seen_async: list[tuple[str, str | None, dict[str, str] | None]] = []

    class _CaptureAsyncSdk(_FakeAsyncInvokeWithHeadersSdk):
        async def _invoke(
            self,
            name: str,
            method: str,
            path: str,
            *,
            json_body: Any = None,
            params: dict[str, Any] | None = None,
            idempotency_key: str | None = None,
            headers: dict[str, str] | None = None,
            timeout_s: float | None = None,
        ) -> dict[str, Any]:
            seen_async.append((name, idempotency_key, headers))
            return await super()._invoke(
                name,
                method,
                path,
                json_body=json_body,
                params=params,
                idempotency_key=idempotency_key,
                headers=headers,
                timeout_s=timeout_s,
            )

    async def run() -> None:
        async_adapter = AsyncEbbmemClientSdkAdapter(_CaptureAsyncSdk())
        await async_adapter.remember(content="hi", headers={"Idempotency-Key": "ebb_hdr_async_1"})
        assert seen_async == [
            ("remember", "ebb_hdr_async_1", {"Idempotency-Key": "ebb_hdr_async_1"})
        ]

    asyncio.run(run())


def test_sdk_adapter_reinforce_omits_none_fields_from_model_fields_set() -> None:
    class _FakeMemoryId:
        def __init__(self, value: str) -> None:
            self.actual_instance = value

        def to_dict(self) -> str:
            return self.actual_instance

    class _FakeUsedIdsInner:
        def __init__(self, value: str) -> None:
            self.actual_instance = value

        def to_dict(self) -> str:
            return self.actual_instance

    class _FakeReinforceRequestModel:
        def __init__(self, **kwargs: Any) -> None:
            self.model_fields_set = set(kwargs.keys())
            self.memory_id = kwargs["memory_id"]
            self.delta = kwargs.get("delta", 0.1)
            self.used_ids = kwargs.get("used_ids")
            self.recall_id = kwargs.get("recall_id")
            self.namespace = kwargs.get("namespace")

        def to_dict(self) -> dict[str, Any]:
            out: dict[str, Any] = {
                "memory_id": self.memory_id.to_dict(),
                "delta": self.delta,
            }
            if self.used_ids is not None:
                out["used_ids"] = [u.to_dict() for u in self.used_ids]
            elif "used_ids" in self.model_fields_set:
                out["used_ids"] = None
            if self.recall_id is not None:
                out["recall_id"] = self.recall_id
            elif "recall_id" in self.model_fields_set:
                out["recall_id"] = None
            if self.namespace is not None:
                out["namespace"] = self.namespace
            elif "namespace" in self.model_fields_set:
                out["namespace"] = None
            return out

    class _ModelRawMemory(_FakeRawMemory):
        def reinforce(self, body: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(("reinforce", body, kwargs))
            payload = body.to_dict()
            return {"status": "reinforced", "outcome": "counted", **payload}

    sdk = _FakeSyncSdk()
    sdk.raw.memory = _ModelRawMemory()  # type: ignore[assignment]
    adapter = EbbmemClientSdkAdapter(sdk)
    adapter._models = {
        "ReinforceRequestModel": _FakeReinforceRequestModel,
        "MemoryId": _FakeMemoryId,
        "UsedIdsInner": _FakeUsedIdsInner,
    }

    single = adapter.reinforce(memory_id="m-1")
    assert single["status"] == "reinforced"
    recorded_single = sdk.raw.memory.calls[-1][1]
    assert isinstance(recorded_single, _FakeReinforceRequestModel)
    assert recorded_single.model_fields_set == {"memory_id", "delta"}
    dumped_single = recorded_single.to_dict()
    assert "used_ids" not in dumped_single
    assert "recall_id" not in dumped_single
    assert "namespace" not in dumped_single

    multi = adapter.reinforce(
        memory_id="m-1",
        used_ids=["m-2"],
        recall_id="rec-1",
        namespace="acme:u:alice",
    )
    assert multi["used_ids"] == ["m-2"]
    assert multi["recall_id"] == "rec-1"
    assert multi["namespace"] == "acme:u:alice"


def test_async_sdk_adapter_purge_normalizes_status_and_irreversible() -> None:
    class _PurgeFoundAsyncSdk(_FakeAsyncSdk):
        def __init__(self, found: bool) -> None:
            super().__init__()
            self._found = found

        async def _invoke(
            self,
            name: str,
            method: str,
            path: str,
            *,
            json_body: Any = None,
            params: dict[str, Any] | None = None,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            del json_body, idempotency_key
            self.calls.append((name, method, path, None, params))
            return {"found": self._found, "memory_id": "m-1"}

    class _PurgeFoundFacadeMemory(_FakeFacadeAsyncMemory):
        async def forget(self, memory_id: str, *, purge: bool = False) -> dict[str, Any]:
            assert purge is True
            return {"found": True, "memory_id": memory_id}

    class _PurgeFoundFacadeSdk(_FakeFacadeAsyncSdk):
        def __init__(self) -> None:
            super().__init__()
            self.memory = _PurgeFoundFacadeMemory()

    async def run() -> None:
        found_adapter = AsyncEbbmemClientSdkAdapter(_PurgeFoundAsyncSdk(found=True))
        res_found = await found_adapter.purge("m-1", namespace="acme:u:alice")
        assert res_found == {
            "found": True,
            "memory_id": "m-1",
            "status": "purged",
            "irreversible": True,
        }

        missing_adapter = AsyncEbbmemClientSdkAdapter(_PurgeFoundAsyncSdk(found=False))
        res_missing = await missing_adapter.purge("m-1")
        assert res_missing == {"found": False, "memory_id": "m-1"}

        facade_adapter = AsyncEbbmemClientSdkAdapter(_PurgeFoundFacadeSdk())
        res_facade = await facade_adapter.purge("m-2")
        assert res_facade == {
            "found": True,
            "memory_id": "m-2",
            "status": "purged",
            "irreversible": True,
        }

    asyncio.run(run())
