import {
  ApiValidationError,
  AuthError,
  EbbmemApiError,
  IdempotencyConflictError,
  NotFoundError,
  RateLimitError,
  RequestTimeoutError,
  ServerError,
  TransportError,
  UnsupportedCapabilityError,
} from "./errors";
import { DEFAULT_REINFORCE_DELTA, validateDelta } from "./reinforcement";
import {
  RETRYABLE_STATUS_CODES,
  RetryPolicy,
  isIdempotentRequest,
  parseRetryAfterMs,
  validateIdempotencyKey,
} from "./retry";
import {
  TELEMETRY_ENABLED_DEFAULT,
  redactSecrets,
  validateAuthCredentials,
  validateBaseUrl,
  validateRequestTarget,
} from "./security";

export interface SdkCapabilities {
  tierOnRemember: boolean;
  sessionIdOnRemember: boolean;
  metadataOnRemember: boolean;
  recallFilters: boolean;
  recallBreakdownAndId: boolean;
  reinforceUsedIdsAndRecallId: boolean;
  reinforceDefaultDelta01: boolean;
  deterministicIdempotencyKey: boolean;
  exactKeyGet: boolean;
  exactKeyDelete: boolean;
  metadataFilterOnListRecall: boolean;
  namespacePrefixList: boolean;
  notes: readonly string[];
}

export interface HttpResponse {
  status: number;
  headers: Record<string, string>;
  body: unknown;
}

export interface RememberParams {
  content: string;
  namespace?: string;
  tier?: string;
  tags?: readonly string[];
  importance?: number;
  valence?: number;
  arousal?: number;
  sessionId?: string;
  timestampMs?: number;
  metadata?: Record<string, unknown>;
  idempotencyKey?: string;
  headers?: Record<string, string>;
}

export interface RecallParams {
  query: string;
  namespace?: string;
  limit?: number;
  tier?: string;
  tags?: readonly string[];
  sessionId?: string;
  profile?: string;
  minScore?: number;
  domain?: string;
  timeoutMs?: number;
  headers?: Record<string, string>;
}

export interface ReinforceParams {
  memoryId: string;
  usedIds?: readonly string[];
  recallId?: string;
  delta?: number;
  namespace?: string;
  headers?: Record<string, string>;
}

export interface MemoryScopedParams {
  namespace?: string;
  headers?: Record<string, string>;
}

export interface EbbmemTransport {
  readonly capabilities: SdkCapabilities;
  remember(params: RememberParams): Promise<Record<string, unknown>>;
  recall(params: RecallParams): Promise<Record<string, unknown>>;
  reinforce(params: ReinforceParams): Promise<Record<string, unknown>>;
  get(memoryId: string, params?: MemoryScopedParams): Promise<Record<string, unknown>>;
  forget(memoryId: string, params?: MemoryScopedParams): Promise<Record<string, unknown>>;
  purge(memoryId: string, params?: MemoryScopedParams): Promise<Record<string, unknown>>;
  status(params?: MemoryScopedParams): Promise<Record<string, unknown>>;
  health(): Promise<Record<string, unknown>>;
}

function hasHeaderCaseInsensitive(headers: Record<string, string>, name: string): boolean {
  const target = name.toLowerCase();
  return Object.keys(headers).some((k) => k.toLowerCase() === target);
}

function getHeaderCaseInsensitive(
  headers: Record<string, string> | undefined,
  name: string,
): string | undefined {
  if (!headers) return undefined;
  const target = name.toLowerCase();
  for (const [k, v] of Object.entries(headers)) {
    if (k.toLowerCase() === target) {
      return v;
    }
  }
  return undefined;
}

function setHeaderCaseInsensitive(
  headers: Record<string, string>,
  name: string,
  value: string,
): void {
  const target = name.toLowerCase();
  for (const k of Object.keys(headers)) {
    if (k.toLowerCase() === target) {
      delete headers[k];
    }
  }
  headers[name] = value;
}

function setHeaderIfAbsentCaseInsensitive(
  headers: Record<string, string>,
  name: string,
  value: string,
): void {
  if (!hasHeaderCaseInsensitive(headers, name)) {
    headers[name] = value;
  }
}

function httpErrorFromResponse(resp: HttpResponse): EbbmemApiError {
  const bodyObj =
    resp.body && typeof resp.body === "object" && !Array.isArray(resp.body)
      ? (resp.body as Record<string, unknown>)
      : {};
  const code = typeof bodyObj.code === "string" ? bodyObj.code : undefined;
  const detail = typeof bodyObj.detail === "string" ? bodyObj.detail : undefined;
  const retryAfterMs = parseRetryAfterMs(resp.headers);
  const msg = redactSecrets(
    `EbbMem HTTP ${resp.status}${code ? ` [${code}]` : ""}${detail ? `: ${detail}` : ""}`,
  );
  const opts = { status: resp.status, code, detail, retryAfterMs, body: resp.body };
  if (resp.status === 401 || resp.status === 403) return new AuthError(msg, opts);
  if (resp.status === 404) return new NotFoundError(msg, opts);
  if (resp.status === 409 || resp.status === 410) return new IdempotencyConflictError(msg, opts);
  if (resp.status === 429) return new RateLimitError(msg, opts);
  if (resp.status === 400 || resp.status === 422) {
    if (code && code.startsWith("EBB-100-02") && code !== "EBB-100-023") {
      return new IdempotencyConflictError(msg, opts);
    }
    return new ApiValidationError(msg, opts);
  }
  if (resp.status >= 500) return new ServerError(msg, opts);
  return new EbbmemApiError(msg, opts);
}

export interface FetchTransportOptions {
  baseUrl: string;
  bearerToken?: string;
  apiKey?: string;
  retryPolicy?: RetryPolicy;
  telemetryEnabled?: boolean;
  logSink?: string[];
  userAgent?: string;
}

export class FetchTransport implements EbbmemTransport {
  readonly baseUrl: string;
  readonly retryPolicy: RetryPolicy;
  readonly telemetryEnabled: boolean;
  readonly logSink: string[];
  readonly userAgent: string;
  readonly capabilities: SdkCapabilities;
  private readonly authHeaders: Record<string, string>;

  constructor(options: FetchTransportOptions) {
    this.baseUrl = validateBaseUrl(options.baseUrl);
    this.authHeaders = validateAuthCredentials({
      bearerToken: options.bearerToken,
      apiKey: options.apiKey,
    });
    this.retryPolicy = options.retryPolicy ?? new RetryPolicy();
    this.telemetryEnabled = Boolean(options.telemetryEnabled ?? TELEMETRY_ENABLED_DEFAULT);
    this.logSink = options.logSink ?? [];
    this.userAgent = options.userAgent ?? "ebbmem-integration-core-ts/0.1.0";
    this.capabilities = {
      tierOnRemember: true,
      sessionIdOnRemember: true,
      metadataOnRemember: true,
      recallFilters: true,
      recallBreakdownAndId: true,
      reinforceUsedIdsAndRecallId: true,
      reinforceDefaultDelta01: true,
      deterministicIdempotencyKey: true,
      exactKeyGet: false,
      exactKeyDelete: false,
      metadataFilterOnListRecall: false,
      namespacePrefixList: false,
      notes: [
        "Built-in fetch transport supports full IC-1 REST verbs; P0-09 exact-key lookup/delete, " +
          "metadata filters, and namespace listing are not yet enabled on the base transport.",
      ],
    };
  }

  toString(): string {
    const authMode =
      "Authorization" in this.authHeaders
        ? "bearer"
        : "X-API-Key" in this.authHeaders
          ? "apiKey"
          : "none";
    return `FetchTransport(baseUrl=${JSON.stringify(this.baseUrl)}, auth=${authMode})`;
  }

  private log(message: string): void {
    this.logSink.push(redactSecrets(message));
  }

  async request(
    method: string,
    path: string,
    options: {
      jsonBody?: unknown;
      headers?: Record<string, string>;
      query?: Record<string, string>;
      timeoutMs?: number;
    } = {},
  ): Promise<HttpResponse> {
    validateRequestTarget(path, options.query);
    let url = `${this.baseUrl}${path}`;
    if (options.query && Object.keys(options.query).length > 0) {
      const qs = new URLSearchParams(options.query).toString();
      url = `${url}${path.includes("?") ? "&" : "?"}${qs}`;
    }

    const reqHeaders: Record<string, string> = {};
    setHeaderCaseInsensitive(reqHeaders, "User-Agent", this.userAgent);
    setHeaderCaseInsensitive(reqHeaders, "Accept", "application/json");
    for (const [k, v] of Object.entries(this.authHeaders)) {
      setHeaderCaseInsensitive(reqHeaders, k, v);
    }
    for (const [k, v] of Object.entries(options.headers ?? {})) {
      setHeaderCaseInsensitive(reqHeaders, k, v);
    }
    let bodyStr: string | undefined;
    if (options.jsonBody !== undefined) {
      bodyStr = JSON.stringify(options.jsonBody);
      setHeaderIfAbsentCaseInsensitive(reqHeaders, "Content-Type", "application/json");
    }

    const effectiveTimeoutMs = options.timeoutMs ?? this.retryPolicy.timeoutMs;
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, effectiveTimeoutMs);

    try {
      const resp = await fetch(url, {
        method: method.toUpperCase(),
        headers: reqHeaders,
        body: bodyStr,
        signal: controller.signal,
      });
      const rawText = await resp.text();
      let parsed: unknown = null;
      if (rawText) {
        try {
          parsed = JSON.parse(rawText);
        } catch {
          parsed = { raw: redactSecrets(rawText) };
        }
      }
      const respHeaders: Record<string, string> = {};
      resp.headers.forEach((v, k) => {
        respHeaders[k] = v;
      });
      this.log(`${method.toUpperCase()} ${path} -> ${resp.status}`);
      return { status: resp.status, headers: respHeaders, body: parsed };
    } catch (err) {
      const errName = err instanceof Error ? err.name : "";
      if (timedOut || errName === "AbortError" || errName === "TimeoutError") {
        this.log(`${method.toUpperCase()} ${path} -> timeout`);
        throw new RequestTimeoutError("Request timed out");
      }
      const errMsg = err instanceof Error ? err.message : String(err);
      this.log(`${method.toUpperCase()} ${path} -> connection_error`);
      throw new TransportError(`Transport connection error: ${errMsg}`);
    } finally {
      clearTimeout(timer);
    }
  }

  async requestWithRetry(
    method: string,
    path: string,
    options: {
      jsonBody?: unknown;
      headers?: Record<string, string>;
      query?: Record<string, string>;
      timeoutMs?: number;
      retryPolicy?: RetryPolicy;
    } = {},
  ): Promise<HttpResponse> {
    const policy = options.retryPolicy ?? this.retryPolicy;
    const canRetry = isIdempotentRequest(method, path, {
      headers: options.headers,
      jsonBody: options.jsonBody,
    });
    const maxAttempts = canRetry ? 1 + policy.maxRetries : 1;

    let lastResp: HttpResponse | undefined;
    for (let attempt = 0; attempt < maxAttempts; attempt++) {
      try {
        const resp = await this.request(method, path, {
          jsonBody: options.jsonBody,
          headers: options.headers,
          query: options.query,
          timeoutMs: options.timeoutMs,
        });
        lastResp = resp;
        if (!RETRYABLE_STATUS_CODES.has(resp.status) || attempt + 1 >= maxAttempts) {
          return resp;
        }
        const retryAfterMs = resp.status === 429 ? parseRetryAfterMs(resp.headers) : undefined;
        await policy.sleep(policy.delayForAttemptMs(attempt, retryAfterMs));
      } catch (err) {
        if (!(err instanceof RequestTimeoutError || err instanceof TransportError)) {
          throw err;
        }
        if (!canRetry || attempt + 1 >= maxAttempts) {
          throw err;
        }
        await policy.sleep(policy.delayForAttemptMs(attempt));
      }
    }
    return lastResp!;
  }

  private async callJson(
    method: string,
    path: string,
    options: {
      jsonBody?: unknown;
      headers?: Record<string, string>;
      query?: Record<string, string>;
      timeoutMs?: number;
    } = {},
  ): Promise<Record<string, unknown>> {
    const resp = await this.requestWithRetry(method, path, options);
    if (resp.status >= 400) {
      throw httpErrorFromResponse(resp);
    }
    if (resp.body && typeof resp.body === "object" && !Array.isArray(resp.body)) {
      return resp.body as Record<string, unknown>;
    }
    return {};
  }

  async remember(params: RememberParams): Promise<Record<string, unknown>> {
    const body: Record<string, unknown> = {
      content: params.content,
      tier: params.tier ?? "semantic",
    };
    if (params.namespace !== undefined) body.namespace = params.namespace;
    if (params.tags !== undefined) body.tags = [...params.tags];
    if (params.importance !== undefined) body.importance = params.importance;
    if (params.valence !== undefined) body.valence = params.valence;
    if (params.arousal !== undefined) body.arousal = params.arousal;
    if (params.sessionId !== undefined) body.session_id = params.sessionId;
    if (params.timestampMs !== undefined) body.timestamp_ms = params.timestampMs;
    if (params.metadata !== undefined) body.metadata = { ...params.metadata };

    const reqHeaders: Record<string, string> = { ...(params.headers ?? {}) };
    if (params.namespace !== undefined) {
      setHeaderIfAbsentCaseInsensitive(reqHeaders, "X-Ebb-Namespace", params.namespace);
    }
    if (params.idempotencyKey !== undefined) {
      validateIdempotencyKey(params.idempotencyKey);
      body.idempotency_key = params.idempotencyKey;
      setHeaderIfAbsentCaseInsensitive(reqHeaders, "Idempotency-Key", params.idempotencyKey);
    }

    return this.callJson("POST", "/api/v1/memory/remember", {
      jsonBody: body,
      headers: reqHeaders,
    });
  }

  async recall(params: RecallParams): Promise<Record<string, unknown>> {
    const body: Record<string, unknown> = {
      query: params.query,
      limit: params.limit ?? 10,
      reinforce: false,
    };
    if (params.namespace !== undefined) body.namespace = params.namespace;
    if (params.tier !== undefined) body.tier = params.tier;
    if (params.tags && params.tags.length > 0) body.tags = [...params.tags];
    if (params.sessionId !== undefined) body.session_id = params.sessionId;
    if (params.profile !== undefined) body.profile = params.profile;
    if (params.minScore !== undefined) body.min_score = params.minScore;
    if (params.domain !== undefined) body.domain = params.domain;

    const reqHeaders: Record<string, string> = { ...(params.headers ?? {}) };
    if (params.namespace !== undefined) {
      setHeaderIfAbsentCaseInsensitive(reqHeaders, "X-Ebb-Namespace", params.namespace);
    }

    return this.callJson("POST", "/api/v1/memory/recall", {
      jsonBody: body,
      headers: reqHeaders,
      timeoutMs: params.timeoutMs,
    });
  }

  async reinforce(params: ReinforceParams): Promise<Record<string, unknown>> {
    const validatedDelta = validateDelta(params.delta ?? DEFAULT_REINFORCE_DELTA);
    const body: Record<string, unknown> = {
      memory_id: params.memoryId,
      delta: validatedDelta,
    };
    if (params.usedIds && params.usedIds.length > 0) body.used_ids = [...params.usedIds];
    if (params.recallId !== undefined) body.recall_id = params.recallId;
    if (params.namespace !== undefined) body.namespace = params.namespace;

    const reqHeaders: Record<string, string> = { ...(params.headers ?? {}) };
    if (params.namespace !== undefined) {
      setHeaderIfAbsentCaseInsensitive(reqHeaders, "X-Ebb-Namespace", params.namespace);
    }

    return this.callJson("POST", "/api/v1/memory/reinforce", {
      jsonBody: body,
      headers: reqHeaders,
    });
  }

  async get(
    memoryId: string,
    params: MemoryScopedParams = {},
  ): Promise<Record<string, unknown>> {
    const reqHeaders: Record<string, string> = { ...(params.headers ?? {}) };
    const query: Record<string, string> = {};
    if (params.namespace !== undefined) {
      setHeaderIfAbsentCaseInsensitive(reqHeaders, "X-Ebb-Namespace", params.namespace);
      query.namespace = params.namespace;
    }
    return this.callJson("GET", `/api/v1/memory/${encodeURIComponent(memoryId)}`, {
      headers: reqHeaders,
      query,
    });
  }

  async forget(
    memoryId: string,
    params: MemoryScopedParams = {},
  ): Promise<Record<string, unknown>> {
    const reqHeaders: Record<string, string> = { ...(params.headers ?? {}) };
    const query: Record<string, string> = {};
    if (params.namespace !== undefined) {
      setHeaderIfAbsentCaseInsensitive(reqHeaders, "X-Ebb-Namespace", params.namespace);
      query.namespace = params.namespace;
    }
    return this.callJson("DELETE", `/api/v1/memory/${encodeURIComponent(memoryId)}`, {
      headers: reqHeaders,
      query,
    });
  }

  async purge(
    memoryId: string,
    params: MemoryScopedParams = {},
  ): Promise<Record<string, unknown>> {
    const reqHeaders: Record<string, string> = { ...(params.headers ?? {}) };
    const query: Record<string, string> = {};
    if (params.namespace !== undefined) {
      setHeaderIfAbsentCaseInsensitive(reqHeaders, "X-Ebb-Namespace", params.namespace);
      query.namespace = params.namespace;
    }
    return this.callJson("DELETE", `/api/v1/memory/${encodeURIComponent(memoryId)}/purge`, {
      headers: reqHeaders,
      query,
    });
  }

  async status(params: MemoryScopedParams = {}): Promise<Record<string, unknown>> {
    const reqHeaders: Record<string, string> = { ...(params.headers ?? {}) };
    const query: Record<string, string> = {};
    if (params.namespace !== undefined) {
      setHeaderIfAbsentCaseInsensitive(reqHeaders, "X-Ebb-Namespace", params.namespace);
      query.namespace = params.namespace;
    }
    return this.callJson("GET", "/api/v1/memory/status", {
      headers: reqHeaders,
      query,
    });
  }

  async health(): Promise<Record<string, unknown>> {
    return this.callJson("GET", "/healthz");
  }
}

export type SdkInitOverride =
  | RequestInit
  | ((requestContext: {
      init: { headers?: Record<string, string>; [key: string]: unknown };
      context: unknown;
    }) => Promise<RequestInit>);

/**
 * Structural contract matching released @ebbmem/client v0.2.0.
 */
export interface SdkRawMemoryApiLike {
  remember?(
    req: {
      rememberRequestModel: Record<string, unknown>;
      xEbbNamespace?: string;
      idempotencyKey?: string;
    },
    initOverrides?: SdkInitOverride,
  ): Promise<unknown>;
  recall?(
    req: { recallRequestModel: Record<string, unknown>; xEbbNamespace?: string },
    initOverrides?: SdkInitOverride,
  ): Promise<unknown>;
  reinforce?(
    req: { reinforceRequestModel: Record<string, unknown>; xEbbNamespace?: string },
    initOverrides?: SdkInitOverride,
  ): Promise<unknown>;
  getMemory?(
    req: { memoryId: string; xEbbNamespace?: string },
    initOverrides?: SdkInitOverride,
  ): Promise<unknown>;
  forgetMemory?(
    req: { memoryId: string; purge?: boolean; xEbbNamespace?: string },
    initOverrides?: SdkInitOverride,
  ): Promise<unknown>;
  purgeMemory?(
    req: { memoryId: string; xEbbNamespace?: string },
    initOverrides?: SdkInitOverride,
  ): Promise<unknown>;
  status?(
    req: { xEbbNamespace?: string },
    initOverrides?: SdkInitOverride,
  ): Promise<unknown>;
  getByKey?(
    req: { key: string; xEbbNamespace?: string },
    initOverrides?: SdkInitOverride,
  ): Promise<unknown>;
  deleteByKey?(
    req: { key: string; xEbbNamespace?: string },
    initOverrides?: SdkInitOverride,
  ): Promise<unknown>;
  listNamespaces?(
    req: { prefix?: string },
    initOverrides?: SdkInitOverride,
  ): Promise<unknown>;
}

export interface SdkClientLike {
  config?: {
    baseUrl?: string;
    token?: string;
    apiKey?: string;
    namespace?: string;
  };
  baseUrl?: string;
  token?: string;
  apiKey?: string;
  supportsRecallFilters?: boolean;
  supportsMetadataFilter?: boolean;
  invoke?<T>(name: string, op: () => Promise<T>, idempotencyKey?: string): Promise<T>;
  health?(): Promise<unknown>;
  memory: {
    remember(content: string, options?: Record<string, unknown>): Promise<unknown>;
    recall(query: string, options?: Record<string, unknown>): Promise<unknown>;
    get(memoryId: string): Promise<unknown>;
    forget(memoryId: string, purge?: boolean): Promise<unknown>;
    reinforce(memoryId: string, delta?: number): Promise<unknown>;
    status(): Promise<unknown>;
  };
  raw?: {
    memory?: SdkRawMemoryApiLike;
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

function buildSdkInitOverrides(
  headers?: Record<string, string>,
  timeoutMs?: number,
): SdkInitOverride | undefined {
  const hasHeaders = Boolean(headers && Object.keys(headers).length > 0);
  if (!hasHeaders && timeoutMs === undefined) {
    return undefined;
  }
  return async ({ init }) => {
    const next: RequestInit = { ...init };
    if (hasHeaders && headers) {
      const merged = new Headers(init.headers as HeadersInit | undefined);
      for (const [k, v] of Object.entries(headers)) {
        merged.set(k, v);
      }
      next.headers = merged;
    }
    if (
      timeoutMs !== undefined &&
      !next.signal &&
      typeof AbortSignal !== "undefined" &&
      typeof AbortSignal.timeout === "function"
    ) {
      next.signal = AbortSignal.timeout(timeoutMs);
    }
    return next;
  };
}

function translateSdkError(err: unknown): EbbmemApiError {
  if (err instanceof EbbmemApiError) return err;
  const anyErr = err as {
    name?: string;
    status?: number;
    code?: string;
    retryAfter?: number;
    message?: string;
    body?: unknown;
  };
  const status = typeof anyErr?.status === "number" ? anyErr.status : undefined;
  const code = typeof anyErr?.code === "string" ? anyErr.code : undefined;
  const msg = redactSecrets(anyErr?.message ?? String(err));
  const opts = {
    status,
    code,
    retryAfterMs: typeof anyErr?.retryAfter === "number" ? anyErr.retryAfter * 1000 : undefined,
    body: anyErr?.body,
  };
  if (status === 401 || status === 403) return new AuthError(msg, opts);
  if (status === 404) return new NotFoundError(msg, opts);
  if (status === 409 || status === 410) return new IdempotencyConflictError(msg, opts);
  if (status === 429) return new RateLimitError(msg, opts);
  if (status === 400 || status === 422) {
    if (code && code.startsWith("EBB-100-02") && code !== "EBB-100-023") {
      return new IdempotencyConflictError(msg, opts);
    }
    return new ApiValidationError(msg, opts);
  }
  if (status !== undefined && status >= 500) return new ServerError(msg, opts);
  if (err instanceof Error && (err.name === "TimeoutError" || err.name === "AbortError")) {
    return new RequestTimeoutError(msg, opts);
  }
  if ((err instanceof Error && err.name === "ConnectionError") || anyErr?.name === "ConnectionError") {
    return new TransportError(msg, opts);
  }
  return new EbbmemApiError(msg, opts);
}

export interface SdkTransportAdapterOptions {
  baseUrl?: string;
  bearerToken?: string;
  apiKey?: string;
}

/**
 * Capability-aware adapter wrapping @ebbmem/client v0.2.0.
 *
 * Bridges v0.2.0 facade gaps via client.raw.memory where RecallRequestModel / RememberRequestModel /
 * ReinforceRequestModel accept IC-1 fields, enforces delta=0.1 default (v0.2.0 facade defaulted to 1),
 * and throws UnsupportedCapabilityError for unmerged P0-09/P0-11 gaps.
 */
export class SdkTransportAdapter implements EbbmemTransport {
  readonly capabilities: SdkCapabilities;
  private readonly sdk: SdkClientLike;

  constructor(sdkClient: SdkClientLike, options: SdkTransportAdapterOptions = {}) {
    const baseUrl = options.baseUrl ?? sdkClient.config?.baseUrl ?? sdkClient.baseUrl;
    if (baseUrl) {
      validateBaseUrl(baseUrl);
    }
    validateAuthCredentials({
      bearerToken: options.bearerToken ?? sdkClient.config?.token ?? sdkClient.token,
      apiKey: options.apiKey ?? sdkClient.config?.apiKey ?? sdkClient.apiKey,
    });

    this.sdk = sdkClient;
    const rawMem = sdkClient.raw?.memory;
    const hasRaw = Boolean(rawMem);
    this.capabilities = {
      tierOnRemember: hasRaw && typeof rawMem?.remember === "function",
      sessionIdOnRemember: hasRaw && typeof rawMem?.remember === "function",
      metadataOnRemember: hasRaw && typeof rawMem?.remember === "function",
      recallFilters: Boolean(sdkClient.supportsRecallFilters),
      recallBreakdownAndId: true,
      reinforceUsedIdsAndRecallId: hasRaw && typeof rawMem?.reinforce === "function",
      reinforceDefaultDelta01: true,
      deterministicIdempotencyKey: true,
      exactKeyGet: typeof rawMem?.getByKey === "function",
      exactKeyDelete: typeof rawMem?.deleteByKey === "function",
      metadataFilterOnListRecall: Boolean(sdkClient.supportsMetadataFilter),
      namespacePrefixList: typeof rawMem?.listNamespaces === "function",
      notes: [
        "Released @ebbmem/client v0.2.0 facade omits tier/session_id/metadata on remember, " +
          "profile/min_score/domain on recall, and used_ids/recall_id on reinforce (where " +
          "facade delta defaults to 1). Adapter bridges these via raw.memory when available, " +
          "while P0-09 APIs (getByKey, deleteByKey, metadataFilter, listNamespaces) and " +
          "recall tier/tags/session_id filters raise UnsupportedCapabilityError when absent.",
      ],
    };
  }

  private async runOp<T>(
    name: string,
    op: () => Promise<T>,
    idempotencyKey?: string,
  ): Promise<T> {
    try {
      if (this.sdk.invoke) {
        return await this.sdk.invoke(name, op, idempotencyKey);
      }
      return await op();
    } catch (err) {
      throw translateSdkError(err);
    }
  }

  async remember(params: RememberParams): Promise<Record<string, unknown>> {
    if (params.idempotencyKey !== undefined) {
      validateIdempotencyKey(params.idempotencyKey);
    }
    const headerIdempotencyKey = getHeaderCaseInsensitive(params.headers, "idempotency-key");
    const invokeIdempotencyKey =
      params.idempotencyKey ?? (headerIdempotencyKey ? headerIdempotencyKey : undefined);
    const rawMem = this.sdk.raw?.memory;
    if (rawMem && typeof rawMem.remember === "function") {
      const model: Record<string, unknown> = {
        content: params.content,
        tier: params.tier ?? "semantic",
      };
      if (params.tags !== undefined) model.tags = [...params.tags];
      if (params.importance !== undefined) model.importance = params.importance;
      if (params.valence !== undefined) model.valence = params.valence;
      if (params.arousal !== undefined) model.arousal = params.arousal;
      if (params.sessionId !== undefined) model.session_id = params.sessionId;
      if (params.timestampMs !== undefined) model.timestamp_ms = params.timestampMs;
      if (params.namespace !== undefined) model.namespace = params.namespace;
      if (params.metadata !== undefined) model.metadata = { ...params.metadata };
      if (params.idempotencyKey !== undefined) model.idempotency_key = params.idempotencyKey;

      const initOverrides = buildSdkInitOverrides(params.headers);
      const res = await this.runOp(
        "remember",
        () =>
          initOverrides
            ? rawMem.remember!(
                {
                  rememberRequestModel: model,
                  xEbbNamespace: params.namespace,
                  idempotencyKey: params.idempotencyKey,
                },
                initOverrides,
              )
            : rawMem.remember!({
                rememberRequestModel: model,
                xEbbNamespace: params.namespace,
                idempotencyKey: params.idempotencyKey,
              }),
        invokeIdempotencyKey,
      );
      return asRecord(res);
    }

    if (
      (params.tier !== undefined && params.tier !== "semantic") ||
      params.namespace !== undefined ||
      params.sessionId !== undefined ||
      params.metadata !== undefined ||
      params.valence !== undefined ||
      params.arousal !== undefined ||
      params.timestampMs !== undefined ||
      (params.headers !== undefined && Object.keys(params.headers).length > 0)
    ) {
      throw new UnsupportedCapabilityError(
        "Wrapped @ebbmem/client lacks raw.memory.remember needed for IC-1 remember fields/headers",
      );
    }
    try {
      const res = await this.sdk.memory.remember(params.content, {
        tags: params.tags ? [...params.tags] : undefined,
        importance: params.importance,
        idempotencyKey: params.idempotencyKey,
      });
      return asRecord(res);
    } catch (err) {
      throw translateSdkError(err);
    }
  }

  async recall(params: RecallParams): Promise<Record<string, unknown>> {
    if (
      (params.tier !== undefined ||
        (params.tags && params.tags.length > 0) ||
        params.sessionId !== undefined) &&
      !this.capabilities.recallFilters
    ) {
      throw new UnsupportedCapabilityError(
        "Released @ebbmem/client v0.2.0 RecallRequestModel lacks tier/tags/session_id filters",
      );
    }
    const rawMem = this.sdk.raw?.memory;
    if (rawMem && typeof rawMem.recall === "function") {
      const model: Record<string, unknown> = {
        query: params.query,
        limit: params.limit ?? 10,
        reinforce: false,
      };
      if (params.profile !== undefined) model.profile = params.profile;
      if (params.minScore !== undefined) model.min_score = params.minScore;
      if (params.domain !== undefined) model.domain = params.domain;
      if (params.namespace !== undefined) model.namespace = params.namespace;
      if (this.capabilities.recallFilters) {
        if (params.tier !== undefined) model.tier = params.tier;
        if (params.tags && params.tags.length > 0) model.tags = [...params.tags];
        if (params.sessionId !== undefined) model.session_id = params.sessionId;
      }
      const initOverrides = buildSdkInitOverrides(params.headers, params.timeoutMs);
      const res = await this.runOp("recall", () =>
        initOverrides
          ? rawMem.recall!(
              {
                recallRequestModel: model,
                xEbbNamespace: params.namespace,
              },
              initOverrides,
            )
          : rawMem.recall!({
              recallRequestModel: model,
              xEbbNamespace: params.namespace,
            }),
      );
      return asRecord(res);
    }
    if (
      params.namespace !== undefined ||
      params.profile !== undefined ||
      params.minScore !== undefined ||
      params.domain !== undefined ||
      params.tier !== undefined ||
      (params.tags && params.tags.length > 0) ||
      params.sessionId !== undefined ||
      params.timeoutMs !== undefined ||
      (params.headers !== undefined && Object.keys(params.headers).length > 0)
    ) {
      throw new UnsupportedCapabilityError(
        "Wrapped @ebbmem/client lacks raw.memory.recall needed for IC-1 recall fields/headers",
      );
    }
    try {
      const res = await this.sdk.memory.recall(params.query, { limit: params.limit });
      return asRecord(res);
    } catch (err) {
      throw translateSdkError(err);
    }
  }

  async reinforce(params: ReinforceParams): Promise<Record<string, unknown>> {
    const validatedDelta = validateDelta(params.delta ?? DEFAULT_REINFORCE_DELTA);
    const rawMem = this.sdk.raw?.memory;
    if (rawMem && typeof rawMem.reinforce === "function") {
      const model: Record<string, unknown> = {
        memory_id: params.memoryId,
        delta: validatedDelta,
      };
      if (params.usedIds && params.usedIds.length > 0) model.used_ids = [...params.usedIds];
      if (params.recallId !== undefined) model.recall_id = params.recallId;
      if (params.namespace !== undefined) model.namespace = params.namespace;
      const initOverrides = buildSdkInitOverrides(params.headers);
      const res = await this.runOp("reinforce", () =>
        initOverrides
          ? rawMem.reinforce!(
              {
                reinforceRequestModel: model,
                xEbbNamespace: params.namespace,
              },
              initOverrides,
            )
          : rawMem.reinforce!({
              reinforceRequestModel: model,
              xEbbNamespace: params.namespace,
            }),
      );
      return asRecord(res);
    }
    if (
      (params.usedIds && params.usedIds.length > 0) ||
      params.recallId !== undefined ||
      params.namespace !== undefined ||
      (params.headers !== undefined && Object.keys(params.headers).length > 0)
    ) {
      throw new UnsupportedCapabilityError(
        "Released @ebbmem/client v0.2.0 facade lacks used_ids/recall_id/namespace/headers on reinforce",
      );
    }
    try {
      const res = await this.sdk.memory.reinforce(params.memoryId, validatedDelta);
      return asRecord(res);
    } catch (err) {
      throw translateSdkError(err);
    }
  }

  async get(memoryId: string, params: MemoryScopedParams = {}): Promise<Record<string, unknown>> {
    const rawMem = this.sdk.raw?.memory;
    if (rawMem && typeof rawMem.getMemory === "function") {
      const initOverrides = buildSdkInitOverrides(params.headers);
      const res = await this.runOp("get", () =>
        initOverrides
          ? rawMem.getMemory!({ memoryId, xEbbNamespace: params.namespace }, initOverrides)
          : rawMem.getMemory!({ memoryId, xEbbNamespace: params.namespace }),
      );
      return asRecord(res);
    }
    if (
      params.namespace !== undefined ||
      (params.headers !== undefined && Object.keys(params.headers).length > 0)
    ) {
      throw new UnsupportedCapabilityError(
        "Wrapped @ebbmem/client lacks raw.memory.getMemory needed for namespace/headers",
      );
    }
    try {
      return asRecord(await this.sdk.memory.get(memoryId));
    } catch (err) {
      throw translateSdkError(err);
    }
  }

  async forget(memoryId: string, params: MemoryScopedParams = {}): Promise<Record<string, unknown>> {
    const rawMem = this.sdk.raw?.memory;
    if (rawMem && typeof rawMem.forgetMemory === "function") {
      const initOverrides = buildSdkInitOverrides(params.headers);
      const res = await this.runOp("forget", () =>
        initOverrides
          ? rawMem.forgetMemory!(
              { memoryId, purge: false, xEbbNamespace: params.namespace },
              initOverrides,
            )
          : rawMem.forgetMemory!({ memoryId, purge: false, xEbbNamespace: params.namespace }),
      );
      return asRecord(res);
    }
    if (
      params.namespace !== undefined ||
      (params.headers !== undefined && Object.keys(params.headers).length > 0)
    ) {
      throw new UnsupportedCapabilityError(
        "Wrapped @ebbmem/client lacks raw.memory.forgetMemory needed for namespace/headers",
      );
    }
    try {
      return asRecord(await this.sdk.memory.forget(memoryId, false));
    } catch (err) {
      throw translateSdkError(err);
    }
  }

  async purge(memoryId: string, params: MemoryScopedParams = {}): Promise<Record<string, unknown>> {
    const rawMem = this.sdk.raw?.memory;
    if (rawMem && typeof rawMem.purgeMemory === "function") {
      const initOverrides = buildSdkInitOverrides(params.headers);
      const res = await this.runOp("purge", () =>
        initOverrides
          ? rawMem.purgeMemory!({ memoryId, xEbbNamespace: params.namespace }, initOverrides)
          : rawMem.purgeMemory!({ memoryId, xEbbNamespace: params.namespace }),
      );
      const out = asRecord(res);
      if (out.status === undefined && out.found !== false) {
        out.status = "purged";
      }
      if (out.irreversible === undefined && out.status === "purged") {
        out.irreversible = true;
      }
      return out;
    }
    if (rawMem && typeof rawMem.forgetMemory === "function") {
      const initOverrides = buildSdkInitOverrides(params.headers);
      const res = await this.runOp("purge", () =>
        initOverrides
          ? rawMem.forgetMemory!(
              { memoryId, purge: true, xEbbNamespace: params.namespace },
              initOverrides,
            )
          : rawMem.forgetMemory!({ memoryId, purge: true, xEbbNamespace: params.namespace }),
      );
      return asRecord(res);
    }
    if (
      params.namespace !== undefined ||
      (params.headers !== undefined && Object.keys(params.headers).length > 0)
    ) {
      throw new UnsupportedCapabilityError(
        "Wrapped @ebbmem/client lacks raw.memory.forgetMemory needed for namespace/headers",
      );
    }
    try {
      return asRecord(await this.sdk.memory.forget(memoryId, true));
    } catch (err) {
      throw translateSdkError(err);
    }
  }

  async status(params: MemoryScopedParams = {}): Promise<Record<string, unknown>> {
    const rawMem = this.sdk.raw?.memory;
    if (rawMem && typeof rawMem.status === "function") {
      const initOverrides = buildSdkInitOverrides(params.headers);
      const res = await this.runOp("status", () =>
        initOverrides
          ? rawMem.status!({ xEbbNamespace: params.namespace }, initOverrides)
          : rawMem.status!({ xEbbNamespace: params.namespace }),
      );
      return asRecord(res);
    }
    if (
      params.namespace !== undefined ||
      (params.headers !== undefined && Object.keys(params.headers).length > 0)
    ) {
      throw new UnsupportedCapabilityError(
        "Wrapped @ebbmem/client lacks raw.memory.status needed for namespace/headers",
      );
    }
    try {
      return asRecord(await this.sdk.memory.status());
    } catch (err) {
      throw translateSdkError(err);
    }
  }

  async health(): Promise<Record<string, unknown>> {
    if (!this.sdk.health) {
      throw new UnsupportedCapabilityError("Wrapped SDK client does not expose health()");
    }
    try {
      return asRecord(await this.sdk.health());
    } catch (err) {
      throw translateSdkError(err);
    }
  }

  async getByKey(key: string, namespace?: string): Promise<Record<string, unknown>> {
    const rawMem = this.sdk.raw?.memory;
    if (!this.capabilities.exactKeyGet || !rawMem || typeof rawMem.getByKey !== "function") {
      throw new UnsupportedCapabilityError(
        "getByKey requires P0-09 exact-key APIs not present in @ebbmem/client v0.2.0",
      );
    }
    return asRecord(
      await this.runOp("getByKey", () => rawMem.getByKey!({ key, xEbbNamespace: namespace })),
    );
  }

  async deleteByKey(key: string, namespace?: string): Promise<Record<string, unknown>> {
    const rawMem = this.sdk.raw?.memory;
    if (!this.capabilities.exactKeyDelete || !rawMem || typeof rawMem.deleteByKey !== "function") {
      throw new UnsupportedCapabilityError(
        "deleteByKey requires P0-09 exact-key APIs not present in @ebbmem/client v0.2.0",
      );
    }
    return asRecord(
      await this.runOp("deleteByKey", () => rawMem.deleteByKey!({ key, xEbbNamespace: namespace })),
    );
  }

  async listNamespaces(prefix?: string): Promise<string[]> {
    const rawMem = this.sdk.raw?.memory;
    if (
      !this.capabilities.namespacePrefixList ||
      !rawMem ||
      typeof rawMem.listNamespaces !== "function"
    ) {
      throw new UnsupportedCapabilityError(
        "listNamespaces requires P0-09 namespace-prefix API not in @ebbmem/client v0.2.0",
      );
    }
    const res = await this.runOp("listNamespaces", () => rawMem.listNamespaces!({ prefix }));
    return Array.isArray(res) ? res.map(String) : [];
  }
}
