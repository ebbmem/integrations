import { NamespaceError, ReservedMetadataError, ValidationError } from "./errors";

export const NAMESPACE_RE = /^[A-Za-z0-9._:/-]{1,256}$/;
export const DEFAULT_TENANT = "default";

export function validateNamespace(namespace: string): string {
  if (typeof namespace !== "string" || !NAMESPACE_RE.test(namespace)) {
    throw new NamespaceError(
      "namespace_invalid: expected 1..256 characters matching [A-Za-z0-9._:/-]",
    );
  }
  return namespace;
}

export function checkNamespaceConflict(
  headerNamespace?: string | null,
  bodyNamespace?: string | null,
): string | undefined {
  let hdr: string | undefined;
  if (headerNamespace !== undefined && headerNamespace !== null) {
    if (typeof headerNamespace !== "string") {
      throw new NamespaceError("namespace_invalid: header namespace must be a string");
    }
    hdr = validateNamespace(headerNamespace.trim());
  }
  let bdy: string | undefined;
  if (bodyNamespace !== undefined && bodyNamespace !== null) {
    if (typeof bodyNamespace !== "string") {
      throw new NamespaceError("namespace_invalid: body namespace must be a string");
    }
    bdy = validateNamespace(bodyNamespace.trim());
  }
  if (hdr !== undefined && bdy !== undefined && hdr !== bdy) {
    throw new NamespaceError("namespace_conflict: X-Ebb-Namespace disagrees with body namespace");
  }
  return hdr !== undefined ? hdr : bdy;
}

export function validateMetadata(
  metadata?: Record<string, unknown> | null,
): Record<string, unknown> {
  if (metadata === undefined || metadata === null) {
    return {};
  }
  if (typeof metadata !== "object" || Array.isArray(metadata)) {
    throw new ValidationError("metadata must be a plain object");
  }
  const cleaned: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(metadata)) {
    if (key.trim().toLowerCase().startsWith("ebb.")) {
      throw new ReservedMetadataError(
        `reserved_metadata_prefix: metadata key '${key}' uses reserved 'ebb.' prefix`,
      );
    }
    cleaned[key] = value;
  }
  return cleaned;
}

function normalizeSegment(raw: string): string {
  return raw.trim().replace(/^#+/, "").trim();
}

export interface NormalizedTagsResult {
  tags: string[];
  domain?: string;
  sessionId?: string;
}

export function normalizeTagsAndSession(
  tags?: readonly string[],
  options: {
    tupleSegments?: readonly string[];
    domain?: string;
    sessionId?: string;
  } = {},
): NormalizedTagsResult {
  let resolvedSession =
    typeof options.sessionId === "string" && options.sessionId.trim()
      ? options.sessionId.trim()
      : undefined;

  let resolvedDomain: string | undefined;
  if (typeof options.domain === "string") {
    const norm = normalizeSegment(options.domain);
    if (norm) {
      resolvedDomain = norm;
    }
  }

  const seen = new Set<string>();
  const normalizedTags: string[] = [];
  const combined: string[] = [...(tags ?? []), ...(options.tupleSegments ?? [])];

  for (const raw of combined) {
    if (typeof raw !== "string") {
      throw new ValidationError("tags and tupleSegments must be strings");
    }
    const cleaned = normalizeSegment(raw);
    if (!cleaned) continue;
    if (cleaned.toLowerCase().startsWith("session:")) {
      const extracted = cleaned.slice("session:".length).trim();
      if (!resolvedSession && extracted) {
        resolvedSession = extracted;
      }
      continue;
    }
    if (!seen.has(cleaned)) {
      seen.add(cleaned);
      normalizedTags.push(cleaned);
    }
  }

  return {
    tags: normalizedTags,
    domain: resolvedDomain,
    sessionId: resolvedSession,
  };
}

export interface ResolvedScope {
  namespace: string;
  tenant: string;
  userId?: string;
  soulScope?: "agent";
  soulId?: string;
  soulPath?: string;
  sessionId?: string;
  threadKey?: string;
  tags: string[];
  domain?: string;
}

export interface ScopingResolverOptions {
  tenant?: string;
  userId?: string;
  appNamespace?: string;
  agentId?: string;
  defaultSessionId?: string;
}

export interface ResolveScopeInput {
  userId?: string;
  appNamespace?: string;
  headerNamespace?: string;
  bodyNamespace?: string;
  agentId?: string;
  sessionId?: string;
  threadId?: string;
  tupleSegments?: readonly string[];
  tags?: readonly string[];
  domain?: string;
}

export class ScopingResolver {
  readonly tenant: string;
  readonly userId?: string;
  readonly appNamespace?: string;
  readonly agentId?: string;
  readonly defaultSessionId?: string;

  constructor(options: ScopingResolverOptions = {}) {
    const tenant = options.tenant ?? DEFAULT_TENANT;
    if (typeof tenant !== "string" || !tenant.trim()) {
      throw new NamespaceError("tenant must be a non-empty string");
    }
    this.tenant = tenant.trim();
    this.userId =
      typeof options.userId === "string" && options.userId.trim()
        ? options.userId.trim()
        : undefined;
    this.appNamespace =
      options.appNamespace !== undefined ? validateNamespace(options.appNamespace.trim()) : undefined;
    this.agentId =
      typeof options.agentId === "string" && options.agentId.trim()
        ? options.agentId.trim()
        : undefined;
    this.defaultSessionId =
      typeof options.defaultSessionId === "string" && options.defaultSessionId.trim()
        ? options.defaultSessionId.trim()
        : undefined;

    if (!this.appNamespace && this.userId) {
      validateNamespace(`${this.tenant}:u:${this.userId}`);
    }
  }

  resolveNamespace(options: {
    userId?: string;
    appNamespace?: string;
    headerNamespace?: string;
    bodyNamespace?: string;
  } = {}): string {
    const explicit = checkNamespaceConflict(options.headerNamespace, options.bodyNamespace);
    if (explicit !== undefined) {
      return explicit;
    }
    if (options.appNamespace !== undefined) {
      return validateNamespace(options.appNamespace.trim());
    }
    if (this.appNamespace !== undefined) {
      return this.appNamespace;
    }
    const effectiveUser =
      typeof options.userId === "string" && options.userId.trim()
        ? options.userId.trim()
        : this.userId;
    if (!effectiveUser) {
      throw new NamespaceError("Cannot resolve namespace: provide appNamespace or userId");
    }
    return validateNamespace(`${this.tenant}:u:${effectiveUser}`);
  }

  resolve(input: ResolveScopeInput = {}): ResolvedScope {
    const namespace = this.resolveNamespace(input);
    const effectiveAgent =
      typeof input.agentId === "string" && input.agentId.trim()
        ? input.agentId.trim()
        : this.agentId;

    const soulScope = effectiveAgent ? "agent" : undefined;
    const soulPath = effectiveAgent
      ? `/api/v1/soul/agent/${encodeURIComponent(effectiveAgent)}`
      : undefined;

    const rawThread =
      typeof input.threadId === "string" && input.threadId.trim()
        ? input.threadId.trim()
        : undefined;
    const initialSession =
      typeof input.sessionId === "string" && input.sessionId.trim()
        ? input.sessionId.trim()
        : rawThread;

    const normalized = normalizeTagsAndSession(input.tags, {
      tupleSegments: input.tupleSegments,
      domain: input.domain,
      sessionId: initialSession,
    });

    const finalSession = normalized.sessionId ?? this.defaultSessionId;
    const threadKey = rawThread ?? finalSession;
    const effectiveUser =
      typeof input.userId === "string" && input.userId.trim() ? input.userId.trim() : this.userId;

    return {
      namespace,
      tenant: this.tenant,
      userId: effectiveUser,
      soulScope,
      soulId: effectiveAgent,
      soulPath,
      sessionId: finalSession,
      threadKey,
      tags: normalized.tags,
      domain: normalized.domain,
    };
  }
}

export function rememberTagsWithDomain(scope: ResolvedScope): string[] {
  const out = [...scope.tags];
  if (scope.domain && !out.includes(scope.domain)) {
    out.push(scope.domain);
  }
  return out;
}
