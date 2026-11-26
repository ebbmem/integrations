export interface OpenClawLogger {
  info(message: string): void;
  warn(message: string): void;
  error(message: string): void;
  debug?(message: string): void;
}

export type OpenClawPluginConfig = Record<string, unknown>;

export interface OpenClawToolResult {
  content: Array<{ type: "text"; text: string }>;
  details?: Record<string, unknown>;
  isError?: boolean;
}

export interface OpenClawToolExecContext {
  agentId?: string;
  sessionKey?: string;
  sessionId?: string;
  userId?: string;
  namespace?: string;
  turnId?: string;
}

export interface OpenClawToolAnnotations {
  readOnlyHint?: boolean;
  destructiveHint?: boolean;
  idempotentHint?: boolean;
}

export interface OpenClawToolDefinition<TParams = Record<string, unknown>> {
  name: string;
  label?: string;
  description: string;
  parameters: unknown;
  outputSchema?: unknown;
  annotations?: OpenClawToolAnnotations;
  execute(
    toolCallId: string,
    params: TParams,
    ctx?: OpenClawToolExecContext,
  ): Promise<OpenClawToolResult>;
}

export interface OpenClawHookContext {
  agentId?: string;
  sessionKey?: string;
  sessionId?: string;
  userId?: string;
  namespace?: string;
  channel?: string;
  turnId?: string;
}

export interface OpenClawBeforeAgentStartEvent {
  prompt?: string;
  messages?: unknown[];
  userText?: string;
  turnId?: string;
}

export interface OpenClawBeforeAgentStartResult {
  prependContext?: string;
  prependSystemContext?: string;
}

export interface OpenClawAgentEndEvent {
  messages?: unknown[];
  messagesSnapshot?: unknown[];
  success?: boolean;
  error?: unknown;
  turnId?: string;
}

export interface OpenClawBeforeCompactionEvent {
  messages?: unknown[];
  sessionKey?: string;
  sessionId?: string;
  agentId?: string;
  reason?: string;
}

export interface MemorySearchHit {
  id: string;
  memory_id?: string;
  content: string;
  tier?: string;
  namespace?: string;
  session_id?: string;
  sessionId?: string;
  agent_id?: string;
  agentId?: string;
  score?: number;
  tags?: string[];
  metadata?: Record<string, unknown>;
  provenance?: Record<string, unknown>;
  snippet?: string;
  path?: string;
  startLine?: number;
  endLine?: number;
  source?: string;
}

export interface MemorySearchAuthContext {
  agentId?: string;
  sessionKey?: string;
  sessionId?: string;
  userId?: string;
  namespace?: string;
  requireSessionMatch?: boolean;
  sessionScopedOnly?: boolean;
}

export interface MemorySearchResult {
  query: string;
  count: number;
  recallId?: string;
  degraded?: boolean;
  results: MemorySearchHit[];
}

export interface MemoryHealthStatus {
  ok: boolean;
  status: string;
  provider: string;
  error?: string;
  details?: Record<string, unknown>;
}

export interface RegisteredMemorySearchManager {
  search(query: string, opts?: Record<string, unknown>): Promise<MemorySearchHit[]>;
  readFile?(params: {
    relPath?: string;
    memoryId?: string;
    from?: number;
    lines?: number;
    agentId?: string;
    sessionKey?: string;
    sessionId?: string;
    userId?: string;
    namespace?: string;
  }): Promise<{ text: string; path: string }>;
  status(): Promise<Record<string, unknown>>;
  probeEmbeddingAvailability?(): Promise<{ ok: boolean; error?: string }>;
  close(): Promise<void>;
}

export interface MemoryProviderRuntime {
  readonly providerId: string;
  search(params: {
    query: string;
    limit?: number;
    tier?: string;
    tags?: readonly string[];
    domain?: string;
    profile?: string;
    minScore?: number;
    agentId?: string;
    sessionKey?: string;
    sessionId?: string;
    userId?: string;
    namespace?: string;
    turnId?: string;
    requireSessionMatch?: boolean;
    explicitSessionFilter?: string;
  }): Promise<MemorySearchResult>;
  get(params: {
    memoryId: string;
    agentId?: string;
    sessionKey?: string;
    sessionId?: string;
    userId?: string;
    namespace?: string;
    from?: number;
    lines?: number;
  }): Promise<Record<string, unknown> | null>;
  health(): Promise<MemoryHealthStatus>;
  close(): Promise<void>;
  authorizeSearchHits(
    hits: readonly MemorySearchHit[],
    context: MemorySearchAuthContext,
  ): Promise<MemorySearchHit[]> | MemorySearchHit[];
  getMemorySearchManager?(params: {
    cfg?: unknown;
    agentId?: string;
    purpose?: "default" | "status";
  }): Promise<{ manager: RegisteredMemorySearchManager | null; error?: string }>;
  resolveMemoryBackendConfig?(params: {
    cfg?: unknown;
    agentId?: string;
  }): Record<string, unknown>;
  closeAllMemorySearchManagers?(): Promise<void>;
}

export type MemoryPromptBuilder = (ctx: {
  agentId?: string;
  sessionKey?: string;
  tools?: readonly string[];
}) => string | Promise<string>;

export type MemoryFlushPlan = {
  instructions: string;
  targetTool: "memory_remember";
  defaultTier: "episodic" | "semantic";
  redactSecrets: true;
  maxItems?: number;
} | null;

export type MemoryFlushPlanResolver = (ctx: {
  agentId?: string;
  sessionKey?: string;
  sessionId?: string;
  tokenCount?: number;
}) => MemoryFlushPlan | Promise<MemoryFlushPlan>;

export interface MemoryCapabilityRegistration {
  providerRuntime: MemoryProviderRuntime;
  runtime?: MemoryProviderRuntime;
  promptBuilder: MemoryPromptBuilder;
  flushPlanResolver: MemoryFlushPlanResolver;
  publicArtifacts?: {
    listArtifacts: (params?: unknown) => Promise<unknown[]>;
  };
}

export interface CommanderCommandLike {
  command(nameAndArgs: string): CommanderCommandLike;
  description(str: string): CommanderCommandLike;
  option(
    flags: string,
    description?: string,
    defaultValueOrParser?: string | boolean | string[] | ((value: string, previous: any) => any),
    defaultValue?: any,
  ): CommanderCommandLike;
  action(fn: (...args: any[]) => void | Promise<void>): CommanderCommandLike;
}

export interface OpenClawCliContext {
  program: CommanderCommandLike;
  configPath?: string;
  logger?: OpenClawLogger;
}

export interface OpenClawCliDescriptor {
  name: string;
  description: string;
  hasSubcommands?: boolean;
}

export interface OpenClawCommandContext {
  args?: string;
  senderId?: string;
  agentId?: string;
  sessionKey?: string;
  channel?: string;
}

export interface OpenClawCommandResult {
  text: string;
  isError?: boolean;
}

export interface OpenClawCommandDefinition {
  name: string;
  description: string;
  acceptsArgs?: boolean;
  requireAuth?: boolean;
  handler(ctx: OpenClawCommandContext): OpenClawCommandResult | Promise<OpenClawCommandResult>;
}

export interface OpenClawPluginApi {
  id?: string;
  pluginConfig?: OpenClawPluginConfig;
  config?: Record<string, unknown>;
  logger: OpenClawLogger;
  registerMemoryCapability(cap: MemoryCapabilityRegistration): void;
  registerTool(
    toolOrFactory:
      | OpenClawToolDefinition<any>
      | ((ctx: OpenClawToolExecContext) => OpenClawToolDefinition<any>),
    opts?: { name?: string },
  ): void;
  on(
    event: "before_agent_start" | "agent_end" | "before_compaction" | string,
    handler: (event: any, ctx: OpenClawHookContext) => unknown,
  ): void;
  registerCli(
    registrar: (ctx: OpenClawCliContext) => void | Promise<void>,
    opts?: { commands: string[]; descriptors?: OpenClawCliDescriptor[] },
  ): void;
  registerCommand?(def: OpenClawCommandDefinition): void;
}

export interface OpenClawPluginDefinition<TResult = unknown> {
  id: string;
  name: string;
  description: string;
  kind: "memory";
  register(api: OpenClawPluginApi): TResult;
}

export function definePluginEntry<TResult = unknown>(
  def: OpenClawPluginDefinition<TResult>,
): OpenClawPluginDefinition<TResult> {
  return def;
}
