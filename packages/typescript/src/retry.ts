import { createHash } from "node:crypto";
import { ValidationError } from "./errors";

export const DEFAULT_TIMEOUT_MS = 30_000;
export const DEFAULT_MAX_RETRIES = 2;
export const DEFAULT_BACKOFF_BASE_MS = 200;
export const DEFAULT_BACKOFF_CAP_MS = 8_000;

export const RETRYABLE_STATUS_CODES: ReadonlySet<number> = new Set([429, 502, 503, 504]);

const ALWAYS_IDEMPOTENT_POST_PATHS = new Set([
  "/api/v1/memory/recall",
  "/api/v1/memory/recall/federated",
  "/api/v1/memory/why-not",
]);

const KEYED_REMEMBER_PATHS = new Set([
  "/api/v1/memory/remember",
  "/api/v1/memory/remember/batch",
]);

const MCP_PATHS = new Set(["/mcp", "/mcp/core", "/mcp/full", "/mcp/admin"]);
const MCP_READ_ONLY_METHODS = new Set([
  "ping",
  "initialize",
  "notifications/initialized",
  "server/discover",
  "tools/list",
]);
const MCP_READ_ONLY_TOOLS = new Set([
  "memory_episodic_tail",
  "memory_recall",
  "memory_search",
  "memory_get",
  "memory_status",
  "memory_context_pack",
  "memory_why_not",
  "memory_introspect",
  "memory_federated_recall",
]);
const MCP_KEYED_WRITE_TOOLS = new Set(["memory_episodic_remember", "memory_remember"]);

export function isValidIdempotencyKey(value: unknown): value is string {
  if (typeof value !== "string" || value.length < 1 || value.length > 256) {
    return false;
  }
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code < 0x21 || code > 0x7e) {
      return false;
    }
  }
  return true;
}

export function validateIdempotencyKey(key: string): string {
  if (!isValidIdempotencyKey(key)) {
    throw new ValidationError(
      "invalid_idempotency_key: must be 1..256 printable ASCII VCHAR characters (0x21..0x7E)",
    );
  }
  return key;
}

function compareCodePoints(a: string, b: string): number {
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    const ca = a.codePointAt(i)!;
    const cb = b.codePointAt(j)!;
    if (ca !== cb) return ca - cb;
    i += ca > 0xffff ? 2 : 1;
    j += cb > 0xffff ? 2 : 1;
  }
  return a.length - b.length;
}

function stableStringify(value: unknown): string | undefined {
  if (value === null) {
    return "null";
  }
  if (value === undefined || typeof value === "function" || typeof value === "symbol") {
    return undefined;
  }
  if (typeof value === "object") {
    const maybeToJson = (value as { toJSON?: unknown }).toJSON;
    if (typeof maybeToJson === "function") {
      return stableStringify(maybeToJson.call(value));
    }
    if (Array.isArray(value)) {
      return `[${value.map((item) => stableStringify(item) ?? "null").join(",")}]`;
    }
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) =>
      compareCodePoints(a, b),
    );
    const pairs: string[] = [];
    for (const [k, v] of entries) {
      const serialized = stableStringify(v);
      if (serialized !== undefined) {
        pairs.push(`${JSON.stringify(k)}:${serialized}`);
      }
    }
    return `{${pairs.join(",")}}`;
  }
  if (typeof value === "bigint") {
    return JSON.stringify(value.toString());
  }
  if (typeof value === "number" && !Number.isFinite(value)) {
    return "null";
  }
  return JSON.stringify(value);
}

export function makeIdempotencyKey(parts: readonly unknown[], prefix = "ebb_"): string {
  if (!prefix) {
    throw new ValidationError("prefix must be non-empty");
  }
  for (let i = 0; i < prefix.length; i++) {
    const code = prefix.charCodeAt(i);
    if (code < 0x21 || code > 0x7e) {
      throw new ValidationError("prefix must contain printable ASCII VCHAR characters");
    }
  }
  const canonical = stableStringify(parts) ?? "[]";
  const digest = createHash("sha256").update(canonical, "utf8").digest("hex");
  const key = `${prefix}${digest}`;
  if (key.length > 256) {
    throw new ValidationError("Generated idempotency key exceeds 256 characters");
  }
  return key;
}

export function extractIdempotencyKey(options: {
  headers?: Record<string, string>;
  jsonBody?: unknown;
}): string | undefined {
  if (options.headers) {
    for (const [k, v] of Object.entries(options.headers)) {
      if (k.toLowerCase() === "idempotency-key" && typeof v === "string" && v.length > 0) {
        return v;
      }
    }
  }
  if (options.jsonBody && typeof options.jsonBody === "object" && !Array.isArray(options.jsonBody)) {
    const bodyKey = (options.jsonBody as Record<string, unknown>).idempotency_key;
    if (typeof bodyKey === "string" && bodyKey.length > 0) {
      return bodyKey;
    }
  }
  return undefined;
}

export function isIdempotentRequest(
  method: string,
  path: string,
  options: {
    headers?: Record<string, string>;
    jsonBody?: unknown;
  } = {},
): boolean {
  const methodUpper = method.toUpperCase();
  const pathOnly = path.split("?", 1)[0].split("#", 1)[0];
  let normalizedPath = pathOnly;
  if (/^https?:\/\//i.test(pathOnly)) {
    normalizedPath = new URL(pathOnly).pathname;
  }
  normalizedPath = normalizedPath.replace(/\/+$/, "");

  if (methodUpper === "GET" || methodUpper === "HEAD") {
    return true;
  }
  if (methodUpper === "DELETE") {
    return normalizedPath.startsWith("/api/v1/memory/") && !normalizedPath.endsWith("/purge");
  }
  if (methodUpper === "POST") {
    if (normalizedPath === "/api/v1/memory/reinforce") {
      return false;
    }
    if (ALWAYS_IDEMPOTENT_POST_PATHS.has(normalizedPath)) {
      return true;
    }
    if (KEYED_REMEMBER_PATHS.has(normalizedPath)) {
      return extractIdempotencyKey(options) !== undefined;
    }
    if (
      MCP_PATHS.has(normalizedPath) &&
      options.jsonBody &&
      typeof options.jsonBody === "object" &&
      !Array.isArray(options.jsonBody)
    ) {
      const rpcBody = options.jsonBody as Record<string, unknown>;
      const rpcMethod = typeof rpcBody.method === "string" ? rpcBody.method : "";
      if (MCP_READ_ONLY_METHODS.has(rpcMethod)) {
        return true;
      }
      if (
        rpcMethod === "tools/call" &&
        rpcBody.params &&
        typeof rpcBody.params === "object" &&
        !Array.isArray(rpcBody.params)
      ) {
        const params = rpcBody.params as Record<string, unknown>;
        const toolName = typeof params.name === "string" ? params.name : "";
        if (MCP_READ_ONLY_TOOLS.has(toolName)) {
          return true;
        }
        if (MCP_KEYED_WRITE_TOOLS.has(toolName)) {
          return (
            extractIdempotencyKey({
              headers: options.headers,
              jsonBody: params.arguments,
            }) !== undefined
          );
        }
      }
      return false;
    }
    return false;
  }
  return false;
}

export function parseRetryAfterMs(headers?: Record<string, string>): number | undefined {
  if (!headers) return undefined;
  for (const [k, v] of Object.entries(headers)) {
    if (k.toLowerCase() === "retry-after") {
      const parsed = Number(String(v).trim());
      if (Number.isFinite(parsed) && parsed >= 0) {
        return parsed * 1000;
      }
      return undefined;
    }
  }
  return undefined;
}

export function computeBackoffMs(
  attempt: number,
  options: {
    baseMs?: number;
    capMs?: number;
    jitterFn?: (min: number, max: number) => number;
    retryAfterMs?: number;
  } = {},
): number {
  if (
    options.retryAfterMs !== undefined &&
    Number.isFinite(options.retryAfterMs) &&
    options.retryAfterMs >= 0
  ) {
    return options.retryAfterMs;
  }
  const baseMs = options.baseMs ?? DEFAULT_BACKOFF_BASE_MS;
  const capMs = options.capMs ?? DEFAULT_BACKOFF_CAP_MS;
  const bounded = Math.min(capMs, baseMs * 2 ** attempt);
  const rand = options.jitterFn ?? ((min, max) => min + Math.random() * (max - min));
  return bounded + rand(0, bounded / 2);
}

export interface RetryPolicyOptions {
  timeoutMs?: number;
  maxRetries?: number;
  backoffBaseMs?: number;
  backoffCapMs?: number;
  sleepFn?: (ms: number) => Promise<void>;
  jitterFn?: (min: number, max: number) => number;
}

export class RetryPolicy {
  readonly timeoutMs: number;
  readonly maxRetries: number;
  readonly backoffBaseMs: number;
  readonly backoffCapMs: number;
  readonly sleepFn: (ms: number) => Promise<void>;
  readonly jitterFn?: (min: number, max: number) => number;

  constructor(options: RetryPolicyOptions = {}) {
    const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    const maxRetries = options.maxRetries ?? DEFAULT_MAX_RETRIES;
    const backoffBaseMs = options.backoffBaseMs ?? DEFAULT_BACKOFF_BASE_MS;
    const backoffCapMs = options.backoffCapMs ?? DEFAULT_BACKOFF_CAP_MS;

    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      throw new ValidationError("timeoutMs must be a positive finite number");
    }
    if (!Number.isInteger(maxRetries) || maxRetries < 0) {
      throw new ValidationError("maxRetries must be an integer >= 0");
    }
    if (!Number.isFinite(backoffBaseMs) || backoffBaseMs < 0) {
      throw new ValidationError("backoffBaseMs must be >= 0");
    }
    if (!Number.isFinite(backoffCapMs) || backoffCapMs < 0) {
      throw new ValidationError("backoffCapMs must be >= 0");
    }

    this.timeoutMs = timeoutMs;
    this.maxRetries = maxRetries;
    this.backoffBaseMs = backoffBaseMs;
    this.backoffCapMs = backoffCapMs;
    this.sleepFn =
      options.sleepFn ?? ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
    this.jitterFn = options.jitterFn;
  }

  delayForAttemptMs(attempt: number, retryAfterMs?: number): number {
    return computeBackoffMs(attempt, {
      baseMs: this.backoffBaseMs,
      capMs: this.backoffCapMs,
      jitterFn: this.jitterFn,
      retryAfterMs,
    });
  }

  async sleep(ms: number): Promise<void> {
    if (ms <= 0) return;
    await this.sleepFn(ms);
  }
}
