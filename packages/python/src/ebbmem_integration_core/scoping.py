"""IC-1 tenant, namespace, soul, session, tag, and metadata scoping resolver."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ebbmem_integration_core.errors import NamespaceError, ReservedMetadataError, ValidationError

NAMESPACE_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")
DEFAULT_TENANT = "default"


def validate_namespace(namespace: str) -> str:
    """Validate namespace format against IC-1 ^[A-Za-z0-9._:/-]{1,256}$."""
    if not isinstance(namespace, str) or not NAMESPACE_RE.fullmatch(namespace):
        raise NamespaceError(
            "namespace_invalid: expected 1..256 characters matching [A-Za-z0-9._:/-]"
        )
    return namespace


def check_namespace_conflict(
    header_namespace: str | None,
    body_namespace: str | None,
) -> str | None:
    """Ensure X-Ebb-Namespace header and request body namespace do not disagree."""
    hdr: str | None = None
    if header_namespace is not None:
        if not isinstance(header_namespace, str):
            raise NamespaceError("namespace_invalid: header namespace must be a string")
        hdr = validate_namespace(header_namespace.strip())
    bdy: str | None = None
    if body_namespace is not None:
        if not isinstance(body_namespace, str):
            raise NamespaceError("namespace_invalid: body namespace must be a string")
        bdy = validate_namespace(body_namespace.strip())
    if hdr is not None and bdy is not None and hdr != bdy:
        raise NamespaceError("namespace_conflict: X-Ebb-Namespace disagrees with body namespace")
    return hdr if hdr is not None else bdy


def validate_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate user metadata and reject reserved 'ebb.*' keys."""
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise ValidationError("metadata must be a mapping")
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        key_str = str(key)
        if key_str.strip().lower().startswith("ebb."):
            raise ReservedMetadataError(
                f"reserved_metadata_prefix: metadata key {key_str!r} uses reserved 'ebb.' prefix"
            )
        cleaned[key_str] = value
    return cleaned


def _normalize_segment(raw: str) -> str:
    return raw.strip().lstrip("#").strip()


def normalize_tags_and_session(
    tags: Sequence[str] | None = None,
    *,
    tuple_segments: Sequence[str] | None = None,
    domain: str | None = None,
    session_id: str | None = None,
) -> tuple[list[str], str | None, str | None]:
    """Normalize tags, tuple segments, domain, and session:<id> extraction.

    - Trims whitespace and strips leading '#'.
    - Extracts 'session:<id>' into session_id if session_id is unset.
    - Strips all 'session:*' entries from regular tags.
    - Deduplicates preserving insertion order.
    """
    resolved_session = (
        session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
    )
    resolved_domain: str | None = None
    if isinstance(domain, str):
        norm_domain = _normalize_segment(domain)
        if norm_domain:
            resolved_domain = norm_domain

    seen: set[str] = set()
    normalized_tags: list[str] = []

    combined: list[str] = []
    if tags:
        combined.extend(tags)
    if tuple_segments:
        combined.extend(tuple_segments)

    for raw in combined:
        if not isinstance(raw, str):
            raise ValidationError("tags and tuple segments must be strings")
        cleaned = _normalize_segment(raw)
        if not cleaned:
            continue
        if cleaned.lower().startswith("session:"):
            extracted = cleaned[len("session:") :].strip()
            if resolved_session is None and extracted:
                resolved_session = extracted
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            normalized_tags.append(cleaned)

    return normalized_tags, resolved_domain, resolved_session


@dataclass(frozen=True)
class ResolvedScope:
    """Resolved IC-1 scoping parameters for EbbMem requests."""

    namespace: str
    tenant: str
    user_id: str | None = None
    soul_scope: str | None = None
    soul_id: str | None = None
    soul_path: str | None = None
    session_id: str | None = None
    thread_key: str | None = None
    tags: tuple[str, ...] = ()
    domain: str | None = None

    def remember_tags(self, *, include_domain: bool = True) -> list[str]:
        """Return tags list for remember, optionally folding domain into tags."""
        out = list(self.tags)
        if include_domain and self.domain and self.domain not in out:
            out.append(self.domain)
        return out


class ScopeResolver:
    """Resolve IC-1 tenant/namespace/soul/session/tags without namespace explosion."""

    def __init__(
        self,
        *,
        tenant: str = DEFAULT_TENANT,
        user_id: str | None = None,
        app_namespace: str | None = None,
        agent_id: str | None = None,
        default_session_id: str | None = None,
    ) -> None:
        if not isinstance(tenant, str) or not tenant.strip():
            raise NamespaceError("tenant must be a non-empty string")
        self.tenant = tenant.strip()
        self.user_id = user_id.strip() if isinstance(user_id, str) and user_id.strip() else None
        self.app_namespace = (
            validate_namespace(app_namespace.strip()) if app_namespace is not None else None
        )
        self.agent_id = agent_id.strip() if isinstance(agent_id, str) and agent_id.strip() else None
        self.default_session_id = (
            default_session_id.strip()
            if isinstance(default_session_id, str) and default_session_id.strip()
            else None
        )
        if self.app_namespace is None and self.user_id is not None:
            # Validate default per-user namespace upfront.
            validate_namespace(f"{self.tenant}:u:{self.user_id}")

    def resolve_namespace(
        self,
        *,
        user_id: str | None = None,
        app_namespace: str | None = None,
        header_namespace: str | None = None,
        body_namespace: str | None = None,
    ) -> str:
        """Resolve a namespace from explicit override, app_namespace, or {tenant}:u:{user_id}."""
        explicit = check_namespace_conflict(header_namespace, body_namespace)
        if explicit is not None:
            return explicit
        if app_namespace is not None:
            return validate_namespace(app_namespace.strip())
        if self.app_namespace is not None:
            return self.app_namespace
        effective_user = (
            user_id.strip() if isinstance(user_id, str) and user_id.strip() else self.user_id
        )
        if not effective_user:
            raise NamespaceError("Cannot resolve namespace: provide app_namespace or user_id")
        return validate_namespace(f"{self.tenant}:u:{effective_user}")

    def resolve(
        self,
        *,
        user_id: str | None = None,
        app_namespace: str | None = None,
        header_namespace: str | None = None,
        body_namespace: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        thread_id: str | None = None,
        tuple_segments: Sequence[str] | None = None,
        tags: Sequence[str] | None = None,
        domain: str | None = None,
    ) -> ResolvedScope:
        """Resolve full IC-1 sub-scoping without ever minting per-thread/tuple namespaces."""
        namespace = self.resolve_namespace(
            user_id=user_id,
            app_namespace=app_namespace,
            header_namespace=header_namespace,
            body_namespace=body_namespace,
        )

        effective_agent = (
            agent_id.strip() if isinstance(agent_id, str) and agent_id.strip() else self.agent_id
        )
        soul_scope = "agent" if effective_agent else None
        soul_path = (
            f"/api/v1/soul/agent/{urllib.parse.quote(effective_agent, safe='')}"
            if effective_agent
            else None
        )

        raw_thread = thread_id.strip() if isinstance(thread_id, str) and thread_id.strip() else None
        initial_session = (
            session_id.strip() if isinstance(session_id, str) and session_id.strip() else raw_thread
        )

        norm_tags, norm_domain, extracted_session = normalize_tags_and_session(
            tags,
            tuple_segments=tuple_segments,
            domain=domain,
            session_id=initial_session,
        )
        final_session = extracted_session or self.default_session_id
        thread_key = raw_thread or final_session

        effective_user = (
            user_id.strip() if isinstance(user_id, str) and user_id.strip() else self.user_id
        )

        return ResolvedScope(
            namespace=namespace,
            tenant=self.tenant,
            user_id=effective_user,
            soul_scope=soul_scope,
            soul_id=effective_agent,
            soul_path=soul_path,
            session_id=final_session,
            thread_key=thread_key,
            tags=tuple(norm_tags),
            domain=norm_domain,
        )
