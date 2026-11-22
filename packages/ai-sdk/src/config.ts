import {
  EbbmemIntegrationCore,
  EbbmemIntegrationCoreOptions,
  ScopingResolver,
} from "ebbmem-integration-core";

export type MemoryTier = "working" | "episodic" | "semantic" | "procedural";

export interface EbbmemScopeOptions {
  tenant?: string;
  userId?: string;
  appNamespace?: string;
  agentId?: string;
  sessionId?: string;
  threadId?: string;
  tags?: readonly string[];
  domain?: string;
  headers?: Record<string, string>;
}

export interface EbbmemAiSdkOptions
  extends EbbmemScopeOptions,
    EbbmemIntegrationCoreOptions {
  core?: EbbmemIntegrationCore;
}

export interface ResolvedCoreAndScope {
  core: EbbmemIntegrationCore;
  scopeOverrides: EbbmemScopeOptions;
}

export function mergeTags(
  baseTags?: readonly string[],
  callTags?: readonly string[],
): string[] | undefined {
  const combined = [...(baseTags ?? []), ...(callTags ?? [])];
  return combined.length > 0 ? combined : undefined;
}

export function resolveCoreAndScope(
  options: EbbmemAiSdkOptions = {},
): ResolvedCoreAndScope {
  const scopeOverrides: EbbmemScopeOptions = {
    userId: options.userId,
    appNamespace: options.appNamespace,
    agentId: options.agentId,
    sessionId: options.sessionId,
    threadId: options.threadId,
    tags: options.tags,
    domain: options.domain,
    headers: options.headers,
  };

  if (options.core) {
    return { core: options.core, scopeOverrides };
  }

  const scope =
    options.scope ??
    new ScopingResolver({
      tenant: options.tenant,
      userId: options.userId,
      appNamespace:
        options.appNamespace ??
        (options.userId ? undefined : "default:u:default"),
      agentId: options.agentId,
      defaultSessionId: options.sessionId ?? options.threadId,
    });

  const core = new EbbmemIntegrationCore({
    baseUrl: options.baseUrl,
    bearerToken: options.bearerToken,
    apiKey: options.apiKey,
    scope,
    transport: options.transport,
    failOpen: options.failOpen,
    timeoutMs: options.timeoutMs,
    maxRetries: options.maxRetries,
    retryPolicy: options.retryPolicy,
    reinforcePolicy: options.reinforcePolicy,
    exposureBookkeeper: options.exposureBookkeeper,
    telemetryEnabled: options.telemetryEnabled,
    redactBeforeRemember: options.redactBeforeRemember,
    logSink: options.logSink,
  });

  return { core, scopeOverrides };
}
