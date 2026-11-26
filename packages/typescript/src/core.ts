import {
  RateLimitError,
  RequestTimeoutError,
  ServerError,
  TransportError,
  UnsupportedCapabilityError,
  ValidationError,
} from "./errors";
import {
  DEFAULT_REINFORCE_DELTA,
  ExposureBookkeeper,
  RecallResponseLike,
  ReinforceMode,
  ReinforcePolicy,
  ReinforcementTracker,
  validateDelta,
} from "./reinforcement";
import { DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT_MS, RetryPolicy } from "./retry";
import {
  ScopingResolver,
  rememberTagsWithDomain,
  validateMetadata,
} from "./scoping";
import {
  FrameableMemoryItem,
  TELEMETRY_ENABLED_DEFAULT,
  frameRecalledMemories,
  redactPayload,
  redactSecrets,
} from "./security";
import { EbbmemTransport, FetchTransport } from "./transport";

function isFailOpenError(err: unknown): boolean {
  return (
    err instanceof RequestTimeoutError ||
    err instanceof TransportError ||
    err instanceof RateLimitError ||
    err instanceof ServerError
  );
}

function degradedRecall(query: string): Record<string, unknown> {
  return { query, count: 0, results: [], degraded: true };
}

function degradedList(): Record<string, unknown> {
  return {
    items: [],
    rows: [],
    total: 0,
    total_count: 0,
    has_more: false,
    next_cursor: null,
    degraded: true,
  };
}

function degradedEpisodicTail(threadKey: string): Record<string, unknown> {
  return {
    status: "ok",
    thread_key: threadKey,
    count: 0,
    turns: [],
    degraded: true,
  };
}

export interface ListMemoriesOptions {
  tier?: string;
  userId?: string;
  appNamespace?: string;
  headerNamespace?: string;
  bodyNamespace?: string;
  sessionId?: string;
  threadId?: string;
  tupleSegments?: readonly string[];
  tags?: readonly string[];
  domain?: string;
  tombstoned?: boolean;
  search?: string;
  limit?: number;
  cursor?: string;
  page?: number;
  pageSize?: number;
  minImportance?: number;
  minValence?: number;
  maxValence?: number;
  metadataFilter?: Record<string, unknown>;
  timeoutMs?: number;
  failOpen?: boolean;
  headers?: Record<string, string>;
}

export interface EpisodicRememberOptions {
  threadKey?: string;
  role?: string;
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
  idempotencyKey?: string;
  mcpProfile?: string;
  headers?: Record<string, string>;
}

export interface EpisodicTailOptions {
  threadKey?: string;
  limit?: number;
  userId?: string;
  appNamespace?: string;
  headerNamespace?: string;
  bodyNamespace?: string;
  sessionId?: string;
  threadId?: string;
  mcpProfile?: string;
  timeoutMs?: number;
  failOpen?: boolean;
  headers?: Record<string, string>;
}

export interface EbbmemIntegrationCoreOptions {
  baseUrl?: string;
  bearerToken?: string;
  apiKey?: string;
  scope?: ScopingResolver;
  transport?: EbbmemTransport;
  failOpen?: boolean;
  timeoutMs?: number;
  maxRetries?: number;
  retryPolicy?: RetryPolicy;
  reinforcePolicy?: ReinforcePolicy;
  exposureBookkeeper?: ExposureBookkeeper;
  telemetryEnabled?: boolean;
  redactBeforeRemember?: boolean;
  logSink?: string[];
}

export class EbbmemIntegrationCore {
  readonly transport: EbbmemTransport;
  readonly scope: ScopingResolver;
  readonly failOpen: boolean;
  readonly telemetryEnabled: boolean;
  readonly redactBeforeRemember: boolean;
  readonly logSink: string[];
  readonly reinforcePolicy: ReinforcePolicy;
  readonly exposureBookkeeper: ExposureBookkeeper;
  readonly tracker: ReinforcementTracker;

  constructor(options: EbbmemIntegrationCoreOptions = {}) {
    this.logSink = options.logSink ?? [];
    if (options.transport) {
      this.transport = options.transport;
    } else {
      if (!options.baseUrl) {
        throw new ValidationError("Provide either baseUrl or a custom transport");
      }
      const policy =
        options.retryPolicy ??
        new RetryPolicy({
          timeoutMs: options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
          maxRetries: options.maxRetries ?? DEFAULT_MAX_RETRIES,
        });
      this.transport = new FetchTransport({
        baseUrl: options.baseUrl,
        bearerToken: options.bearerToken,
        apiKey: options.apiKey,
        retryPolicy: policy,
        telemetryEnabled: options.telemetryEnabled ?? TELEMETRY_ENABLED_DEFAULT,
        logSink: this.logSink,
      });
    }
    this.scope = options.scope ?? new ScopingResolver({ appNamespace: "default:u:default" });
    this.failOpen = options.failOpen ?? true;
    this.telemetryEnabled = Boolean(options.telemetryEnabled ?? TELEMETRY_ENABLED_DEFAULT);
    this.redactBeforeRemember = options.redactBeforeRemember ?? true;
    this.reinforcePolicy = options.reinforcePolicy ?? new ReinforcePolicy();
    this.exposureBookkeeper = options.exposureBookkeeper ?? new ExposureBookkeeper();
    this.tracker = new ReinforcementTracker(this.reinforcePolicy, this.exposureBookkeeper);
  }

  toString(): string {
    return `EbbmemIntegrationCore(transport=${String(this.transport)}, failOpen=${this.failOpen})`;
  }

  private logWarning(message: string): void {
    this.logSink.push(redactSecrets(message));
  }

  async remember(
    content: string,
    options: {
      tier?: string;
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
      importance?: number;
      valence?: number;
      arousal?: number;
      timestampMs?: number;
      metadata?: Record<string, unknown>;
      idempotencyKey?: string;
      headers?: Record<string, string>;
    } = {},
  ): Promise<Record<string, unknown>> {
    const resolved = this.scope.resolve({
      userId: options.userId,
      appNamespace: options.appNamespace,
      headerNamespace: options.headerNamespace,
      bodyNamespace: options.bodyNamespace,
      agentId: options.agentId,
      sessionId: options.sessionId,
      threadId: options.threadId,
      tupleSegments: options.tupleSegments,
      tags: options.tags,
      domain: options.domain,
    });

    let cleanMetadata = validateMetadata(options.metadata);
    const effectiveContent = this.redactBeforeRemember ? redactSecrets(content) : content;
    if (this.redactBeforeRemember && Object.keys(cleanMetadata).length > 0) {
      cleanMetadata = redactPayload(cleanMetadata) as Record<string, unknown>;
    }
    let finalTags = rememberTagsWithDomain(resolved);
    if (this.redactBeforeRemember && finalTags.length > 0) {
      finalTags = finalTags.map((t) => redactSecrets(t));
    }

    return this.transport.remember({
      content: effectiveContent,
      namespace: resolved.namespace,
      tier: options.tier ?? "semantic",
      tags: finalTags,
      importance: options.importance,
      valence: options.valence,
      arousal: options.arousal,
      sessionId: resolved.sessionId,
      timestampMs: options.timestampMs,
      metadata: Object.keys(cleanMetadata).length > 0 ? cleanMetadata : undefined,
      idempotencyKey: options.idempotencyKey,
      headers: options.headers,
    });
  }

  async recall(
    query: string,
    options: {
      limit?: number;
      tier?: string;
      userId?: string;
      appNamespace?: string;
      headerNamespace?: string;
      bodyNamespace?: string;
      sessionId?: string;
      threadId?: string;
      tupleSegments?: readonly string[];
      tags?: readonly string[];
      domain?: string;
      profile?: string;
      minScore?: number;
      turnId?: string;
      timeoutMs?: number;
      failOpen?: boolean;
      headers?: Record<string, string>;
    } = {},
  ): Promise<Record<string, unknown>> {
    const resolved = this.scope.resolve({
      userId: options.userId,
      appNamespace: options.appNamespace,
      headerNamespace: options.headerNamespace,
      bodyNamespace: options.bodyNamespace,
      sessionId: options.sessionId,
      threadId: options.threadId,
      tupleSegments: options.tupleSegments,
      tags: options.tags,
      domain: options.domain,
    });

    const effectiveFailOpen = options.failOpen ?? this.failOpen;
    try {
      const resp = await this.transport.recall({
        query,
        namespace: resolved.namespace,
        limit: options.limit ?? 10,
        tier: options.tier,
        tags: resolved.tags.length > 0 ? resolved.tags : undefined,
        sessionId: resolved.sessionId,
        profile: options.profile,
        minScore: options.minScore,
        domain: resolved.domain,
        timeoutMs: options.timeoutMs,
        headers: options.headers,
      });
      this.tracker.recordExposure(resolved.namespace, resp as RecallResponseLike, {
        turnId: options.turnId,
      });
      return resp;
    } catch (err) {
      if (effectiveFailOpen && isFailOpenError(err)) {
        const errName = err instanceof Error ? err.name : "Error";
        const errMsg = err instanceof Error ? err.message : String(err);
        this.logWarning(`recall degraded error=${errName}: ${errMsg}`);
        return degradedRecall(query);
      }
      throw err;
    }
  }

  async listMemories(options: ListMemoriesOptions = {}): Promise<Record<string, unknown>> {
    if (typeof this.transport.listMemories !== "function") {
      throw new UnsupportedCapabilityError("Configured transport does not implement listMemories");
    }
    const resolved = this.scope.resolve({
      userId: options.userId,
      appNamespace: options.appNamespace,
      headerNamespace: options.headerNamespace,
      bodyNamespace: options.bodyNamespace,
      sessionId: options.sessionId,
      threadId: options.threadId,
      tupleSegments: options.tupleSegments,
      tags: options.tags,
      domain: options.domain,
    });
    const effectiveFailOpen = options.failOpen ?? this.failOpen;
    try {
      return await this.transport.listMemories({
        namespace: resolved.namespace,
        tier: options.tier,
        tags: resolved.tags.length > 0 ? resolved.tags : undefined,
        tombstoned: options.tombstoned ?? false,
        search: options.search,
        sessionId: resolved.sessionId,
        limit: options.limit,
        cursor: options.cursor,
        page: options.page,
        pageSize: options.pageSize,
        minImportance: options.minImportance,
        minValence: options.minValence,
        maxValence: options.maxValence,
        metadataFilter: options.metadataFilter,
        timeoutMs: options.timeoutMs,
        headers: options.headers,
      });
    } catch (err) {
      if (effectiveFailOpen && isFailOpenError(err)) {
        const errName = err instanceof Error ? err.name : "Error";
        const errMsg = err instanceof Error ? err.message : String(err);
        this.logWarning(`listMemories degraded error=${errName}: ${errMsg}`);
        return degradedList();
      }
      throw err;
    }
  }

  async episodicRemember(
    content: string,
    options: EpisodicRememberOptions = {},
  ): Promise<Record<string, unknown>> {
    if (typeof this.transport.episodicRemember !== "function") {
      throw new UnsupportedCapabilityError(
        "Configured transport does not implement episodicRemember",
      );
    }
    const resolved = this.scope.resolve({
      userId: options.userId,
      appNamespace: options.appNamespace,
      headerNamespace: options.headerNamespace,
      bodyNamespace: options.bodyNamespace,
      agentId: options.agentId,
      sessionId: options.sessionId,
      threadId: options.threadId,
      tupleSegments: options.tupleSegments,
      tags: options.tags,
      domain: options.domain,
    });
    const effectiveThreadKey = options.threadKey ?? resolved.threadKey;
    if (typeof effectiveThreadKey !== "string" || !effectiveThreadKey.trim()) {
      throw new ValidationError("threadKey is required for episodicRemember");
    }
    const effectiveContent = this.redactBeforeRemember ? redactSecrets(content) : content;
    let finalTags = rememberTagsWithDomain(resolved);
    if (this.redactBeforeRemember && finalTags.length > 0) {
      finalTags = finalTags.map((t) => redactSecrets(t));
    }
    return this.transport.episodicRemember({
      threadKey: effectiveThreadKey.trim(),
      sessionId: resolved.sessionId,
      content: effectiveContent,
      role: options.role ?? "user",
      tags: finalTags,
      namespace: resolved.namespace,
      idempotencyKey: options.idempotencyKey,
      mcpProfile: options.mcpProfile ?? "full",
      headers: options.headers,
    });
  }

  async episodicTail(options: EpisodicTailOptions = {}): Promise<Record<string, unknown>> {
    if (typeof this.transport.episodicTail !== "function") {
      throw new UnsupportedCapabilityError("Configured transport does not implement episodicTail");
    }
    const resolved = this.scope.resolve({
      userId: options.userId,
      appNamespace: options.appNamespace,
      headerNamespace: options.headerNamespace,
      bodyNamespace: options.bodyNamespace,
      sessionId: options.sessionId,
      threadId: options.threadId,
    });
    const effectiveThreadKey = options.threadKey ?? resolved.threadKey;
    if (typeof effectiveThreadKey !== "string" || !effectiveThreadKey.trim()) {
      throw new ValidationError("threadKey is required for episodicTail");
    }
    const cleanThreadKey = effectiveThreadKey.trim();
    const effectiveFailOpen = options.failOpen ?? this.failOpen;
    try {
      return await this.transport.episodicTail({
        threadKey: cleanThreadKey,
        sessionId: resolved.sessionId,
        limit: options.limit ?? 10,
        namespace: resolved.namespace,
        mcpProfile: options.mcpProfile ?? "full",
        timeoutMs: options.timeoutMs,
        headers: options.headers,
      });
    } catch (err) {
      if (effectiveFailOpen && isFailOpenError(err)) {
        const errName = err instanceof Error ? err.name : "Error";
        const errMsg = err instanceof Error ? err.message : String(err);
        this.logWarning(`episodicTail degraded error=${errName}: ${errMsg}`);
        return degradedEpisodicTail(cleanThreadKey);
      }
      throw err;
    }
  }

  frameMemories(
    resultsOrResponse: readonly FrameableMemoryItem[] | { results?: readonly FrameableMemoryItem[] },
  ): string {
    if (Array.isArray(resultsOrResponse)) {
      return frameRecalledMemories(resultsOrResponse);
    }
    const items = (resultsOrResponse as { results?: readonly FrameableMemoryItem[] }).results ?? [];
    return frameRecalledMemories(items);
  }

  async reinforce(
    memoryId: string,
    options: {
      usedIds?: readonly string[];
      recallId?: string;
      delta?: number;
      clampDelta?: boolean;
      userId?: string;
      appNamespace?: string;
      headerNamespace?: string;
      bodyNamespace?: string;
      headers?: Record<string, string>;
    } = {},
  ): Promise<Record<string, unknown>> {
    const resolved = this.scope.resolve({
      userId: options.userId,
      appNamespace: options.appNamespace,
      headerNamespace: options.headerNamespace,
      bodyNamespace: options.bodyNamespace,
    });
    const validatedDelta = validateDelta(options.delta ?? DEFAULT_REINFORCE_DELTA, {
      clamp: options.clampDelta,
    });
    const seen = new Set<string>();
    const dedupUsed: string[] = [];
    for (const uid of options.usedIds ?? []) {
      if (uid && uid !== memoryId && !seen.has(uid)) {
        seen.add(uid);
        dedupUsed.push(uid);
      }
    }
    return this.transport.reinforce({
      memoryId,
      usedIds: dedupUsed,
      recallId: options.recallId,
      delta: validatedDelta,
      namespace: resolved.namespace,
      headers: options.headers,
    });
  }

  async reinforceFromExposure(
    options: {
      usedIds?: readonly string[];
      responseText?: string;
      turnId?: string;
      recallId?: string;
      delta?: number;
      clampDelta?: boolean;
      mode?: ReinforceMode;
      injected?: boolean;
      userId?: string;
      appNamespace?: string;
      headerNamespace?: string;
      bodyNamespace?: string;
      headers?: Record<string, string>;
    } = {},
  ): Promise<Record<string, unknown> | undefined> {
    const resolved = this.scope.resolve({
      userId: options.userId,
      appNamespace: options.appNamespace,
      headerNamespace: options.headerNamespace,
      bodyNamespace: options.bodyNamespace,
    });
    const plan = this.tracker.planReinforcement(resolved.namespace, {
      usedIds: options.usedIds,
      responseText: options.responseText,
      turnId: options.turnId,
      recallId: options.recallId,
      delta: options.delta,
      clampDelta: options.clampDelta,
      mode: options.mode,
      injected: options.injected,
    });
    if (!plan) {
      return undefined;
    }
    return this.transport.reinforce({
      memoryId: plan.memoryId,
      usedIds: plan.usedIds,
      recallId: plan.recallId,
      delta: plan.delta,
      namespace: plan.namespace,
      headers: options.headers,
    });
  }

  async get(
    memoryId: string,
    options: { userId?: string; appNamespace?: string; headers?: Record<string, string> } = {},
  ): Promise<Record<string, unknown>> {
    const namespace = this.scope.resolveNamespace(options);
    return this.transport.get(memoryId, { namespace, headers: options.headers });
  }

  async forget(
    memoryId: string,
    options: { userId?: string; appNamespace?: string; headers?: Record<string, string> } = {},
  ): Promise<Record<string, unknown>> {
    const namespace = this.scope.resolveNamespace(options);
    return this.transport.forget(memoryId, { namespace, headers: options.headers });
  }

  /** Framework delete always maps strictly to soft-tombstone forget, never purge. */
  async delete(
    memoryId: string,
    options: { userId?: string; appNamespace?: string; headers?: Record<string, string> } = {},
  ): Promise<Record<string, unknown>> {
    return this.forget(memoryId, options);
  }

  /** Explicit admin-only physical erasure. */
  async purge(
    memoryId: string,
    options: { userId?: string; appNamespace?: string; headers?: Record<string, string> } = {},
  ): Promise<Record<string, unknown>> {
    const namespace = this.scope.resolveNamespace(options);
    return this.transport.purge(memoryId, { namespace, headers: options.headers });
  }

  async status(
    options: { userId?: string; appNamespace?: string; headers?: Record<string, string> } = {},
  ): Promise<Record<string, unknown>> {
    const namespace = this.scope.resolveNamespace(options);
    return this.transport.status({ namespace, headers: options.headers });
  }

  async health(): Promise<Record<string, unknown>> {
    return this.transport.health();
  }
}
