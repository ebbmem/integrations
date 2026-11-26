"""HTTP stdlib transport (sync & async) and released-SDK adapter for ebbmem-client 0.2.0."""

from __future__ import annotations

from ebbmem_integration_core.http_async_transport import AsyncStdlibHttpTransport
from ebbmem_integration_core.http_transport import StdlibHttpTransport
from ebbmem_integration_core.sdk_adapter import EbbmemClientSdkAdapter
from ebbmem_integration_core.sdk_async_adapter import AsyncEbbmemClientSdkAdapter
from ebbmem_integration_core.sdk_common import (
    _annotation_accepts_float,
    _detect_sdk_capabilities,
    _load_sdk_models,
    _model_field_accepts_float,
    _parse_sdk_error_body,
    _to_plain_dict,
    _translate_sdk_exception,
    _validate_sdk_config,
)
from ebbmem_integration_core.transport_types import (
    AsyncMemoryTransport,
    HttpResponse,
    MemoryTransport,
    SdkCapabilities,
    _get_header_ci,
    _has_header,
    _http_error_from_response,
    _merge_headers_ci,
    _set_header_ci,
    _setdefault_header_ci,
)

__all__ = [
    "AsyncEbbmemClientSdkAdapter",
    "AsyncMemoryTransport",
    "AsyncStdlibHttpTransport",
    "EbbmemClientSdkAdapter",
    "HttpResponse",
    "MemoryTransport",
    "SdkCapabilities",
    "StdlibHttpTransport",
    "_annotation_accepts_float",
    "_detect_sdk_capabilities",
    "_get_header_ci",
    "_has_header",
    "_http_error_from_response",
    "_load_sdk_models",
    "_merge_headers_ci",
    "_model_field_accepts_float",
    "_parse_sdk_error_body",
    "_set_header_ci",
    "_setdefault_header_ci",
    "_to_plain_dict",
    "_translate_sdk_exception",
    "_validate_sdk_config",
]
