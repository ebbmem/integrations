import {
  DEFAULT_FRAMEWORK_REINFORCE_MODE,
  DEFAULT_REINFORCE_DELTA,
  DEFAULT_TENANT,
  NamespaceError,
  ReinforceMode,
  TELEMETRY_ENABLED_DEFAULT,
  ValidationError,
  checkNamespaceConflict,
  redactPayload,
  redactSecrets,
  validateAuthCredentials,
  validateBaseUrl,
  validateDelta,
  validateNamespace,
} from "ebbmem-integration-core";

const AWS_ACCESS_KEY_ID_RE = /\b(?:AKIA|ASIA)[0-9A-Z]{16}\b/g;
const AWS_SECRET_ACCESS_KEY_RE =
  /((?:["']?aws_secret_access_key["']?)\s*[:=]\s*)(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\s,;&}"']+)/gi;
const PLUGIN_SENSITIVE_KEY_NAMES: ReadonlySet<string> = new Set([
  "aws_secret_access_key",
  "aws_access_key_id",
  "aws_session_token",
]);

export function redactPluginSecrets(text: string): string {
  if (!text) return text;
  let out = redactSecrets(text);
  out = out.replace(AWS_ACCESS_KEY_ID_RE, "[REDACTED]");
  out = out.replace(AWS_SECRET_ACCESS_KEY_RE, "$1[REDACTED]");
  return out;
}

function redactPluginPayloadTree(value: unknown): unknown {
  if (typeof value === "string") {
    return redactPluginSecrets(value);
  }
  if (value === null || value === undefined || typeof value !== "object") {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => redactPluginPayloadTree(item));
  }
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    if (PLUGIN_SENSITIVE_KEY_NAMES.has(k.toLowerCase()) && v !== null && v !== undefined) {
      out[k] = "[REDACTED]";
    } else {
      out[k] = redactPluginPayloadTree(v);
    }
  }
  return out;
}

export function redactPluginPayload(value: unknown): unknown {
  return redactPluginPayloadTree(redactPayload(value));
}

export type MemoryTier = "working" | "episodic" | "semantic" | "procedural";

const VALID_TIERS: ReadonlySet<string> = new Set([
  "working",
  "episodic",
  "semantic",
  "procedural",
]);

const VALID_REINFORCE_MODES: ReadonlySet<string> = new Set([
  "tool",
  "cited",
  "feedback",
  "injected",
]);

export interface EbbmemOpenClawConfig {
  baseUrl?: string;
  apiKey?: string;
  bearerToken?: string;
  tenant: string;
  userId?: string;
  appNamespace?: string;
  defaultAgentId?: string;
  agentNamespaces: Record<string, string>;
  autoRecall: boolean;
  autoCapture: boolean;
  autoReinforce: boolean;
  preCompactionFlush: boolean;
  recallLimit: number;
  recallTier?: MemoryTier;
  recallProfile?: string;
  recallMinScore?: number;
  captureTier: MemoryTier;
  reinforceMode: ReinforceMode;
  injected: boolean;
  reinforceDelta: number;
  redactBeforeRemember: boolean;
  failOpen: boolean;
  telemetryEnabled: boolean;
  timeoutMs: number;
  maxRetries: number;
}

export const DEFAULT_PLUGIN_CONFIG: Readonly<Omit<EbbmemOpenClawConfig, "agentNamespaces">> = {
  tenant: DEFAULT_TENANT,
  autoRecall: true,
  autoCapture: true,
  autoReinforce: true,
  preCompactionFlush: true,
  recallLimit: 6,
  captureTier: "episodic",
  reinforceMode: DEFAULT_FRAMEWORK_REINFORCE_MODE,
  injected: false,
  reinforceDelta: DEFAULT_REINFORCE_DELTA,
  redactBeforeRemember: true,
  failOpen: true,
  telemetryEnabled: TELEMETRY_ENABLED_DEFAULT,
  timeoutMs: 5000,
  maxRetries: 2,
};

function trimNonEmpty(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
}

function parseBool(value: unknown, fallback: boolean, field: string): boolean {
  if (value === undefined || value === null) return fallback;
  if (typeof value !== "boolean") {
    throw new ValidationError(`${field} must be a boolean`);
  }
  return value;
}

function parseTier(value: unknown, field: string): MemoryTier | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  if (typeof value !== "string" || !VALID_TIERS.has(value)) {
    throw new ValidationError(`${field} must be one of: working, episodic, semantic, procedural`);
  }
  return value as MemoryTier;
}

export function parsePluginConfig(
  raw?: Record<string, unknown>,
  env: Record<string, string | undefined> = process.env,
): EbbmemOpenClawConfig {
  const src = raw ?? {};

  const rawBaseUrl = trimNonEmpty(src.baseUrl) ?? trimNonEmpty(env.EBBMEM_BASE_URL);
  const baseUrl = rawBaseUrl ? validateBaseUrl(rawBaseUrl) : undefined;

  const rawApiKey = trimNonEmpty(src.apiKey);
  const rawBearer = trimNonEmpty(src.bearerToken);
  const apiKey =
    rawApiKey ?? (rawBearer === undefined ? trimNonEmpty(env.EBBMEM_API_KEY) : undefined);
  const bearerToken =
    rawBearer ?? (rawApiKey === undefined ? trimNonEmpty(env.EBBMEM_BEARER_TOKEN) : undefined);
  validateAuthCredentials({ bearerToken, apiKey });

  const rawTenant =
    src.tenant !== undefined ? trimNonEmpty(src.tenant) : (trimNonEmpty(env.EBBMEM_TENANT) ?? DEFAULT_TENANT);
  if (!rawTenant) {
    throw new NamespaceError("tenant must be a non-empty string");
  }

  const userId = trimNonEmpty(src.userId) ?? trimNonEmpty(env.EBBMEM_USER_ID);
  const rawAppNs = trimNonEmpty(src.appNamespace) ?? trimNonEmpty(env.EBBMEM_NAMESPACE);
  const appNamespace = rawAppNs ? validateNamespace(rawAppNs) : undefined;
  const defaultAgentId = trimNonEmpty(src.defaultAgentId);

  const agentNamespaces: Record<string, string> = {};
  if (src.agentNamespaces !== undefined && src.agentNamespaces !== null) {
    if (typeof src.agentNamespaces !== "object" || Array.isArray(src.agentNamespaces)) {
      throw new ValidationError("agentNamespaces must be a plain object mapping agentId to namespace");
    }
    for (const [agentId, nsValue] of Object.entries(src.agentNamespaces as Record<string, unknown>)) {
      const cleanAgent = trimNonEmpty(agentId);
      if (!cleanAgent) {
        throw new NamespaceError("agentNamespaces keys must be non-empty strings");
      }
      if (typeof nsValue !== "string") {
        throw new NamespaceError(`agentNamespaces['${cleanAgent}'] must be a string`);
      }
      agentNamespaces[cleanAgent] = validateNamespace(nsValue.trim());
    }
  }

  if (userId) {
    validateNamespace(`${rawTenant}:u:${userId}`);
  }
  if (defaultAgentId && !(defaultAgentId in agentNamespaces)) {
    validateNamespace(`${rawTenant}:a:${defaultAgentId}`);
  }

  const recallLimitRaw = src.recallLimit ?? DEFAULT_PLUGIN_CONFIG.recallLimit;
  if (
    typeof recallLimitRaw !== "number" ||
    !Number.isInteger(recallLimitRaw) ||
    recallLimitRaw < 1 ||
    recallLimitRaw > 100
  ) {
    throw new ValidationError("recallLimit must be an integer between 1 and 100");
  }

  const recallTier = parseTier(src.recallTier, "recallTier");
  const captureTier = parseTier(src.captureTier, "captureTier") ?? DEFAULT_PLUGIN_CONFIG.captureTier;
  const recallProfile = trimNonEmpty(src.recallProfile);

  let recallMinScore: number | undefined;
  if (src.recallMinScore !== undefined && src.recallMinScore !== null) {
    if (
      typeof src.recallMinScore !== "number" ||
      !Number.isFinite(src.recallMinScore) ||
      src.recallMinScore < 0 ||
      src.recallMinScore > 1
    ) {
      throw new ValidationError("recallMinScore must be a finite number in [0, 1]");
    }
    recallMinScore = src.recallMinScore;
  }

  const reinforceModeRaw = src.reinforceMode ?? DEFAULT_PLUGIN_CONFIG.reinforceMode;
  if (typeof reinforceModeRaw !== "string" || !VALID_REINFORCE_MODES.has(reinforceModeRaw)) {
    throw new ValidationError("reinforceMode must be one of: tool, cited, feedback, injected");
  }

  const reinforceDelta = validateDelta(
    src.reinforceDelta ?? DEFAULT_PLUGIN_CONFIG.reinforceDelta,
  );

  const timeoutMsRaw = src.timeoutMs ?? DEFAULT_PLUGIN_CONFIG.timeoutMs;
  if (typeof timeoutMsRaw !== "number" || !Number.isInteger(timeoutMsRaw) || timeoutMsRaw < 100) {
    throw new ValidationError("timeoutMs must be an integer >= 100");
  }

  const maxRetriesRaw = src.maxRetries ?? DEFAULT_PLUGIN_CONFIG.maxRetries;
  if (
    typeof maxRetriesRaw !== "number" ||
    !Number.isInteger(maxRetriesRaw) ||
    maxRetriesRaw < 0 ||
    maxRetriesRaw > 10
  ) {
    throw new ValidationError("maxRetries must be an integer between 0 and 10");
  }

  return {
    baseUrl,
    apiKey,
    bearerToken,
    tenant: rawTenant,
    userId,
    appNamespace,
    defaultAgentId,
    agentNamespaces,
    autoRecall: parseBool(src.autoRecall, DEFAULT_PLUGIN_CONFIG.autoRecall, "autoRecall"),
    autoCapture: parseBool(src.autoCapture, DEFAULT_PLUGIN_CONFIG.autoCapture, "autoCapture"),
    autoReinforce: parseBool(src.autoReinforce, DEFAULT_PLUGIN_CONFIG.autoReinforce, "autoReinforce"),
    preCompactionFlush: parseBool(
      src.preCompactionFlush,
      DEFAULT_PLUGIN_CONFIG.preCompactionFlush,
      "preCompactionFlush",
    ),
    recallLimit: recallLimitRaw,
    recallTier,
    recallProfile,
    recallMinScore,
    captureTier,
    reinforceMode: reinforceModeRaw as ReinforceMode,
    injected: parseBool(src.injected, DEFAULT_PLUGIN_CONFIG.injected, "injected"),
    reinforceDelta,
    redactBeforeRemember: parseBool(
      src.redactBeforeRemember,
      DEFAULT_PLUGIN_CONFIG.redactBeforeRemember,
      "redactBeforeRemember",
    ),
    failOpen: parseBool(src.failOpen, DEFAULT_PLUGIN_CONFIG.failOpen, "failOpen"),
    telemetryEnabled: parseBool(
      src.telemetryEnabled,
      DEFAULT_PLUGIN_CONFIG.telemetryEnabled,
      "telemetryEnabled",
    ),
    timeoutMs: timeoutMsRaw,
    maxRetries: maxRetriesRaw,
  };
}

export interface ScopeResolutionInput {
  agentId?: string;
  sessionKey?: string;
  sessionId?: string;
  userId?: string;
  namespace?: string;
  headerNamespace?: string;
  bodyNamespace?: string;
}

export interface ResolvedOpenClawScope {
  namespace: string;
  agentId?: string;
  sessionId?: string;
  userId?: string;
  hasExplicitIdentity: boolean;
}

export function resolveScopeForContext(
  config: EbbmemOpenClawConfig,
  ctx: ScopeResolutionInput = {},
): ResolvedOpenClawScope {
  const effectiveAgentId = trimNonEmpty(ctx.agentId) ?? config.defaultAgentId;
  const effectiveSessionId = trimNonEmpty(ctx.sessionId) ?? trimNonEmpty(ctx.sessionKey);
  const effectiveUserId = trimNonEmpty(ctx.userId) ?? config.userId;

  const explicitHdrBody = checkNamespaceConflict(ctx.headerNamespace, ctx.bodyNamespace);
  const explicitCtxNs = trimNonEmpty(ctx.namespace);
  const agentMappedNs = effectiveAgentId ? config.agentNamespaces[effectiveAgentId] : undefined;

  let namespace: string;
  let hasExplicitIdentity = true;

  if (explicitHdrBody !== undefined) {
    namespace = explicitHdrBody;
  } else if (explicitCtxNs !== undefined) {
    namespace = validateNamespace(explicitCtxNs);
  } else if (agentMappedNs !== undefined) {
    namespace = agentMappedNs;
  } else if (config.appNamespace !== undefined) {
    namespace = config.appNamespace;
  } else if (effectiveUserId !== undefined) {
    namespace = validateNamespace(`${config.tenant}:u:${effectiveUserId}`);
  } else if (effectiveAgentId !== undefined) {
    namespace = validateNamespace(`${config.tenant}:a:${effectiveAgentId}`);
  } else {
    namespace = validateNamespace(`${config.tenant}:u:default`);
    hasExplicitIdentity = false;
  }

  return {
    namespace,
    agentId: effectiveAgentId,
    sessionId: effectiveSessionId,
    userId: effectiveUserId,
    hasExplicitIdentity,
  };
}

export function resolveStrictAuthScope(
  config: EbbmemOpenClawConfig,
  ctx: ScopeResolutionInput = {},
): ResolvedOpenClawScope | null {
  try {
    const resolved = resolveScopeForContext(config, ctx);
    if (!resolved.hasExplicitIdentity) {
      return null;
    }
    return resolved;
  } catch {
    return null;
  }
}
