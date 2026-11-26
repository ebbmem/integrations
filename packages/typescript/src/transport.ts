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
  ValidationError,
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
  listMemories?: boolean;
  episodicRemember?: boolean;
  episodicTail?: boolean;
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

export interface ListMemoriesParams {
  namespace?: string;
  tier?: string;
  tags?: readonly string[];
  tombstoned?: boolean;
  search?: string;
  sessionId?: string;
  limit?: number;
  cursor?: string;
  page?: number;
  pageSize?: number;
  minImportance?: number;
  minValence?: number;
  maxValence?: number;
  metadataFilter?: Record<string, unknown>;
  timeoutMs?: number;
  headers?: Record<string, string>;
}

export interface EpisodicRememberParams {
  threadKey?: string;
  sessionId?: string;
  content: string;
  role?: string;
  tags?: readonly string[];
  namespace?: string;
  idempotencyKey?: string;
  mcpProfile?: string;
  headers?: Record<string, string>;
}

export interface EpisodicTailParams {
  threadKey?: string;
  sessionId?: string;
  limit?: number;
  namespace?: string;
  mcpProfile?: string;
  timeoutMs?: number;
  headers?: Record<string, string>;
}

export interface EbbmemTransport {
  readonly capabilities: SdkCapabilities;
  remember(params: RememberParams): Promise<Record<string, unknown>>;
  recall(params: RecallParams): Promise<Record<string, unknown>>;
  listMemories?(params?: ListMemoriesParams): Promise<Record<string, unknown>>;
  episodicRemember?(params: EpisodicRememberParams): Promise<Record<string, unknown>>;
  episodicTail?(params?: EpisodicTailParams): Promise<Record<string, unknown>>;
  reinforce(params: ReinforceParams): Promise<Record<string, unknown>>;
  get(memoryId: string, params?: MemoryScopedParams): Promise<Record<string, unknown>>;
  forget(memoryId: string, params?: MemoryScopedParams): Promise<Record<string, unknown>>;
  purge(memoryId: string, params?: MemoryScopedParams): Promise<Record<string, unknown>>;
  status(params?: MemoryScopedParams): Promise<Record<string, unknown>>;
  health(): Promise<Record<string, unknown>>;
}

export function hasHeaderCaseInsensitive(headers: Record<string, string>, name: string): boolean {
  const target = name.toLowerCase();
  return Object.keys(headers).some((k) => k.toLowerCase() === target);
}

export function getHeaderCaseInsensitive(
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

export function setHeaderCaseInsensitive(
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

export function setHeaderIfAbsentCaseInsensitive(
  headers: Record<string, string>,
  name: string,
  value: string,
): void {
  if (!hasHeaderCaseInsensitive(headers, name)) {
    headers[name] = value;
  }
}

export function httpErrorFromResponse(resp: HttpResponse): EbbmemApiError {
  const bodyObj =
    resp.body && typeof resp.body === "object" && !Array.isArray(resp.body)
      ? (resp.body as Record<string, unknown>)
      : {};
  const errObj =
    bodyObj.error && typeof bodyObj.error === "object" && !Array.isArray(bodyObj.error)
      ? (bodyObj.error as Record<string, unknown>)
      : undefined;
  let code = typeof bodyObj.code === "string" ? bodyObj.code : undefined;
  if (!code && errObj) {
    if (typeof errObj.code === "string") {
      code = errObj.code;
    } else if (errObj.data && typeof errObj.data === "object" && !Array.isArray(errObj.data)) {
      const dataCode = (errObj.data as Record<string, unknown>).code;
      if (typeof dataCode === "string") code = dataCode;
    }
  }
  let detail = typeof bodyObj.detail === "string" ? bodyObj.detail : undefined;
  if (!detail && errObj && typeof errObj.message === "string") {
    detail = errObj.message;
  }
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

export function mcpErrorFromPayload(
  status: number,
  headers: Record<string, string>,
  payload: Record<string, unknown>,
): EbbmemApiError {
  const errObj =
    payload.error && typeof payload.error === "object" && !Array.isArray(payload.error)
      ? (payload.error as Record<string, unknown>)
      : {};
  const rpcCode = errObj.code;
  let effectiveStatus = status;
  if (effectiveStatus < 400) {
    if (rpcCode === -32003) effectiveStatus = 403;
    else if (rpcCode === -32004 || rpcCode === -32601) effectiveStatus = 404;
    else if (rpcCode === -32602) effectiveStatus = 422;
    else effectiveStatus = 400;
  }
  return httpErrorFromResponse({ status: effectiveStatus, headers, body: payload });
}

export function extractMcpResultDict(payload: unknown): Record<string, unknown> {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return {};
  }
  const top = payload as Record<string, unknown>;
  const result =
    top.result && typeof top.result === "object" && !Array.isArray(top.result)
      ? (top.result as Record<string, unknown>)
      : top;
  for (const key of ["structuredContent", "structured_content"]) {
    const structured = result[key];
    if (structured && typeof structured === "object" && !Array.isArray(structured)) {
      return { ...(structured as Record<string, unknown>) };
    }
  }
  if (Array.isArray(result.content)) {
    for (const item of result.content) {
      if (item && typeof item === "object" && !Array.isArray(item)) {
        const text = (item as Record<string, unknown>).text;
        if (typeof text === "string" && text.trim()) {
          try {
            const parsed = JSON.parse(text);
            if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
              return parsed as Record<string, unknown>;
            }
          } catch {
            continue;
          }
        }
      }
    }
  }
  return { ...result };
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
      listMemories: true,
      episodicRemember: true,
      episodicTail: true,
      notes: [
        "Built-in fetch transport supports full IC-1 REST verbs, listMemories, and MCP " +
          "episodicRemember/episodicTail; P0-09 exact-key lookup/delete and namespace listing " +
          "are not yet enabled on the base transport.",
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

  async listMemories(params: ListMemoriesParams = {}): Promise<Record<string, unknown>> {
    const reqHeaders: Record<string, string> = { ...(params.headers ?? {}) };
    const query: Record<string, string> = {
      tombstoned: params.tombstoned ? "true" : "false",
    };
    if (params.limit !== undefined) query.limit = String(params.limit);
    if (params.namespace !== undefined) {
      setHeaderIfAbsentCaseInsensitive(reqHeaders, "X-Ebb-Namespace", params.namespace);
      query.namespace = params.namespace;
    }
    if (params.tier !== undefined) query.tier = params.tier;
    if (params.tags && params.tags.length > 0) query.tags = params.tags.join(",");
    if (params.search !== undefined) query.search = params.search;
    if (params.sessionId !== undefined) query.session_id = params.sessionId;
    if (params.cursor !== undefined) query.cursor = params.cursor;
    if (params.page !== undefined) query.page = String(params.page);
    if (params.pageSize !== undefined) query.page_size = String(params.pageSize);
    if (params.minImportance !== undefined) query.min_importance = String(params.minImportance);
    if (params.minValence !== undefined) query.min_valence = String(params.minValence);
    if (params.maxValence !== undefined) query.max_valence = String(params.maxValence);
    if (params.metadataFilter !== undefined) {
      query.metadata_filter = JSON.stringify(params.metadataFilter);
    }
    return this.callJson("GET", "/api/v1/memory", {
      headers: reqHeaders,
      query,
      timeoutMs: params.timeoutMs,
    });
  }

  private async callMcpTool(
    toolName: string,
    args: Record<string, unknown>,
    options: {
      namespace?: string;
      mcpProfile?: string;
      idempotencyKey?: string;
      timeoutMs?: number;
      headers?: Record<string, string>;
    } = {},
  ): Promise<Record<string, unknown>> {
    const profile = options.mcpProfile ?? "full";
    if (profile !== "core" && profile !== "full" && profile !== "admin") {
      throw new ValidationError(`Invalid mcpProfile: ${profile}`);
    }
    const reqHeaders: Record<string, string> = { ...(options.headers ?? {}) };
    setHeaderCaseInsensitive(reqHeaders, "Accept", "application/json, text/event-stream");
    setHeaderIfAbsentCaseInsensitive(reqHeaders, "MCP-Protocol-Version", "2026-07-28");
    setHeaderCaseInsensitive(reqHeaders, "Mcp-Method", "tools/call");
    setHeaderCaseInsensitive(reqHeaders, "Mcp-Name", toolName);
    setHeaderIfAbsentCaseInsensitive(reqHeaders, "X-Ebb-MCP-Profile", profile);
    if (options.namespace !== undefined) {
      setHeaderIfAbsentCaseInsensitive(reqHeaders, "X-Ebb-Namespace", options.namespace);
    }
    if (options.idempotencyKey !== undefined) {
      validateIdempotencyKey(options.idempotencyKey);
      setHeaderIfAbsentCaseInsensitive(reqHeaders, "Idempotency-Key", options.idempotencyKey);
    }
    const rpcBody = {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: { name: toolName, arguments: args },
    };
    const resp = await this.requestWithRetry("POST", `/mcp/${profile}`, {
      jsonBody: rpcBody,
      headers: reqHeaders,
      timeoutMs: options.timeoutMs,
    });
    if (resp.status >= 400) {
      throw httpErrorFromResponse(resp);
    }
    if (resp.body && typeof resp.body === "object" && !Array.isArray(resp.body)) {
      const bodyObj = resp.body as Record<string, unknown>;
      if (bodyObj.error !== undefined && bodyObj.error !== null) {
        throw mcpErrorFromPayload(resp.status, resp.headers, bodyObj);
      }
    }
    return extractMcpResultDict(resp.body);
  }

  async episodicRemember(params: EpisodicRememberParams): Promise<Record<string, unknown>> {
    const args: Record<string, unknown> = {
      content: params.content,
      role: params.role ?? "user",
    };
    if (params.threadKey !== undefined) args.thread_key = params.threadKey;
    if (params.sessionId !== undefined) args.session_id = params.sessionId;
    if (params.tags !== undefined) args.tags = [...params.tags];
    if (params.namespace !== undefined) args.namespace = params.namespace;
    if (params.idempotencyKey !== undefined) {
      validateIdempotencyKey(params.idempotencyKey);
      args.idempotency_key = params.idempotencyKey;
    }
    return this.callMcpTool("memory_episodic_remember", args, {
      namespace: params.namespace,
      mcpProfile: params.mcpProfile,
      idempotencyKey: params.idempotencyKey,
      headers: params.headers,
    });
  }

  async episodicTail(params: EpisodicTailParams = {}): Promise<Record<string, unknown>> {
    const args: Record<string, unknown> = {
      limit: params.limit ?? 10,
    };
    if (params.threadKey !== undefined) args.thread_key = params.threadKey;
    if (params.sessionId !== undefined) args.session_id = params.sessionId;
    if (params.namespace !== undefined) args.namespace = params.namespace;
    return this.callMcpTool("memory_episodic_tail", args, {
      namespace: params.namespace,
      mcpProfile: params.mcpProfile,
      timeoutMs: params.timeoutMs,
      headers: params.headers,
    });
  }
}

export * from "./sdk_adapter";
