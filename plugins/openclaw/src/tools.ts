import { Static, Type } from "typebox";
import { NotFoundError, frameRecalledMemories, redactSecrets } from "ebbmem-integration-core";
import { MemoryTier } from "./config.js";
import {
  OpenClawPluginApi,
  OpenClawToolExecContext,
  OpenClawToolResult,
} from "./openclaw-types.js";
import { EbbmemOpenClawRuntime } from "./runtime.js";

const TierSchema = Type.Enum(["working", "episodic", "semantic", "procedural"]);

export const MemorySearchParamsSchema = Type.Object({
  query: Type.String({
    minLength: 1,
    description: "Natural-language query for read-only cognitive memory recall.",
  }),
  limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100 })),
  tier: Type.Optional(TierSchema),
  tags: Type.Optional(Type.Array(Type.String())),
  domain: Type.Optional(Type.String()),
  session_id: Type.Optional(Type.String()),
  profile: Type.Optional(Type.String()),
  min_score: Type.Optional(Type.Number({ minimum: 0, maximum: 1 })),
});
export type MemorySearchParams = Static<typeof MemorySearchParamsSchema> & {
  sessionId?: string;
  minScore?: number;
};

export const MemoryGetParamsSchema = Type.Object({
  memory_id: Type.Optional(Type.String({ minLength: 1 })),
  path: Type.Optional(Type.String({ minLength: 1 })),
  from: Type.Optional(Type.Integer({ minimum: 1 })),
  lines: Type.Optional(Type.Integer({ minimum: 1 })),
});
export type MemoryGetParams = Static<typeof MemoryGetParamsSchema> & {
  memoryId?: string;
};

export const MemoryRememberParamsSchema = Type.Object({
  content: Type.String({
    minLength: 1,
    description: "Durable fact or episodic summary to store (no raw secrets).",
  }),
  tier: Type.Optional(TierSchema),
  tags: Type.Optional(Type.Array(Type.String())),
  domain: Type.Optional(Type.String()),
  session_id: Type.Optional(Type.String()),
  importance: Type.Optional(Type.Number({ minimum: 0, maximum: 1 })),
  metadata: Type.Optional(Type.Record(Type.String(), Type.Unknown())),
  idempotency_key: Type.Optional(Type.String({ minLength: 1, maxLength: 256 })),
});
export type MemoryRememberParams = Static<typeof MemoryRememberParamsSchema> & {
  sessionId?: string;
  idempotencyKey?: string;
};

export const MemoryReinforceParamsSchema = Type.Object({
  memory_id: Type.String({ minLength: 1 }),
  used_ids: Type.Optional(Type.Array(Type.String())),
  recall_id: Type.Optional(Type.String()),
  delta: Type.Optional(Type.Number({ minimum: -1, maximum: 1 })),
});
export type MemoryReinforceParams = Static<typeof MemoryReinforceParamsSchema> & {
  memoryId?: string;
  usedIds?: string[];
  recallId?: string;
};

export const MemoryForgetParamsSchema = Type.Object({
  memory_id: Type.String({ minLength: 1 }),
});
export type MemoryForgetParams = Static<typeof MemoryForgetParamsSchema> & {
  memoryId?: string;
};

function toolError(err: unknown): OpenClawToolResult {
  const raw = err instanceof Error ? err.message : String(err);
  return {
    content: [{ type: "text", text: redactSecrets(raw) }],
    isError: true,
    details: { error: redactSecrets(raw) },
  };
}

export function registerEbbmemTools(
  api: OpenClawPluginApi,
  runtime: EbbmemOpenClawRuntime,
): void {
  api.registerTool(
    (execCtx?: OpenClawToolExecContext) => ({
      name: "memory_search",
      label: "Memory Search",
      description:
        "Read-only search over EbbMem cognitive memories (universal alias for memory_recall). Returns untrusted reference blocks; memories decay unless reinforced via memory_reinforce.",
      parameters: MemorySearchParamsSchema,
      annotations: { readOnlyHint: true, idempotentHint: true },
      async execute(_toolCallId: string, params: MemorySearchParams, callCtx?: OpenClawToolExecContext) {
        const ctx = callCtx ?? execCtx;
        const explicitSession = params.session_id ?? params.sessionId;
        const res = await runtime.search({
          query: params.query,
          limit: params.limit,
          tier: params.tier as MemoryTier | undefined,
          tags: params.tags,
          domain: params.domain,
          profile: params.profile,
          minScore: params.min_score ?? params.minScore,
          agentId: ctx?.agentId,
          sessionKey: ctx?.sessionKey,
          sessionId: explicitSession ?? ctx?.sessionId,
          userId: ctx?.userId,
          namespace: ctx?.namespace,
          turnId: ctx?.turnId,
          explicitSessionFilter: explicitSession,
          requireSessionMatch: Boolean(explicitSession),
        });
        const text =
          res.results.length > 0
            ? frameRecalledMemories(res.results)
            : "No matching memories found.";
        return {
          content: [{ type: "text", text }],
          details: {
            query: res.query,
            count: res.count,
            recall_id: res.recallId,
            degraded: Boolean(res.degraded),
            results: res.results,
          },
        };
      },
    }),
    { name: "memory_search" },
  );

  api.registerTool(
    (execCtx?: OpenClawToolExecContext) => ({
      name: "memory_get",
      label: "Memory Get",
      description:
        "Fetch a specific EbbMem memory record by memory_id (or OpenClaw path alias) within the caller's authorized scope.",
      parameters: MemoryGetParamsSchema,
      annotations: { readOnlyHint: true, idempotentHint: true },
      async execute(_toolCallId: string, params: MemoryGetParams, callCtx?: OpenClawToolExecContext) {
        const ctx = callCtx ?? execCtx;
        const targetId = params.memory_id ?? params.memoryId ?? params.path;
        if (!targetId || !targetId.trim()) {
          return {
            content: [{ type: "text", text: "memory_id (or path) is required." }],
            isError: true,
          };
        }
        try {
          const rec = await runtime.get({
            memoryId: targetId.trim(),
            agentId: ctx?.agentId,
            sessionKey: ctx?.sessionKey,
            sessionId: ctx?.sessionId,
            userId: ctx?.userId,
            namespace: ctx?.namespace,
            from: params.from,
            lines: params.lines,
          });
          if (!rec) {
            return {
              content: [{ type: "text", text: "Not Found" }],
              isError: true,
              details: { status: 404, memory_id: targetId.trim() },
            };
          }
          const framed = frameRecalledMemories([
            {
              memory_id: String(rec.memory_id ?? rec.id ?? targetId.trim()),
              tier: typeof rec.tier === "string" ? rec.tier : "semantic",
              content: rec.content,
            },
          ]);
          return {
            content: [{ type: "text", text: framed }],
            details: rec,
          };
        } catch (err) {
          if (err instanceof NotFoundError) {
            return {
              content: [{ type: "text", text: "Not Found" }],
              isError: true,
              details: { status: 404, memory_id: targetId.trim() },
            };
          }
          return toolError(err);
        }
      },
    }),
    { name: "memory_get" },
  );

  api.registerTool(
    (execCtx?: OpenClawToolExecContext) => ({
      name: "memory_remember",
      label: "Memory Remember",
      description:
        "Persist a durable semantic fact or episodic event to EbbMem. Secrets are redacted automatically before storage.",
      parameters: MemoryRememberParamsSchema,
      annotations: { readOnlyHint: false },
      async execute(
        _toolCallId: string,
        params: MemoryRememberParams,
        callCtx?: OpenClawToolExecContext,
      ) {
        const ctx = callCtx ?? execCtx;
        try {
          const res = await runtime.remember(params.content, {
            tier: params.tier,
            tags: params.tags,
            domain: params.domain,
            importance: params.importance,
            metadata: params.metadata,
            idempotencyKey: params.idempotency_key ?? params.idempotencyKey,
            agentId: ctx?.agentId,
            sessionKey: ctx?.sessionKey,
            sessionId: params.session_id ?? params.sessionId ?? ctx?.sessionId,
            userId: ctx?.userId,
            namespace: ctx?.namespace,
          });
          return {
            content: [
              {
                type: "text",
                text: `Remembered memory ${String(res.memory_id ?? res.id ?? "")} (tier=${String(res.tier ?? params.tier ?? runtime.config.captureTier)})`,
              },
            ],
            details: res,
          };
        } catch (err) {
          return toolError(err);
        }
      },
    }),
    { name: "memory_remember" },
  );

  api.registerTool(
    (execCtx?: OpenClawToolExecContext) => ({
      name: "memory_reinforce",
      label: "Memory Reinforce",
      description:
        "Reinforce memories actually used in the response (pass memory_id, used_ids, and recall_id). Do not reinforce unused memories.",
      parameters: MemoryReinforceParamsSchema,
      annotations: { readOnlyHint: false },
      async execute(
        _toolCallId: string,
        params: MemoryReinforceParams,
        callCtx?: OpenClawToolExecContext,
      ) {
        const ctx = callCtx ?? execCtx;
        try {
          const memoryId = params.memory_id ?? params.memoryId ?? "";
          const res = await runtime.reinforce({
            memoryId,
            usedIds: params.used_ids ?? params.usedIds,
            recallId: params.recall_id ?? params.recallId,
            delta: params.delta,
            agentId: ctx?.agentId,
            sessionKey: ctx?.sessionKey,
            sessionId: ctx?.sessionId,
            userId: ctx?.userId,
            namespace: ctx?.namespace,
          });
          return {
            content: [
              {
                type: "text",
                text: `Reinforced memory ${memoryId} (strength=${String(res.storage_strength ?? "")}, outcome=${String(res.outcome ?? "reinforced")})`,
              },
            ],
            details: res,
          };
        } catch (err) {
          return toolError(err);
        }
      },
    }),
    { name: "memory_reinforce" },
  );

  api.registerTool(
    (execCtx?: OpenClawToolExecContext) => ({
      name: "memory_forget",
      label: "Memory Forget",
      description: "Soft-delete (tombstone) an EbbMem memory by memory_id while preserving audit lineage.",
      parameters: MemoryForgetParamsSchema,
      annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true },
      async execute(
        _toolCallId: string,
        params: MemoryForgetParams,
        callCtx?: OpenClawToolExecContext,
      ) {
        const ctx = callCtx ?? execCtx;
        try {
          const memoryId = params.memory_id ?? params.memoryId ?? "";
          const res = await runtime.forget({
            memoryId,
            agentId: ctx?.agentId,
            sessionKey: ctx?.sessionKey,
            sessionId: ctx?.sessionId,
            userId: ctx?.userId,
            namespace: ctx?.namespace,
          });
          return {
            content: [{ type: "text", text: `Forgotten memory ${memoryId}` }],
            details: res,
          };
        } catch (err) {
          return toolError(err);
        }
      },
    }),
    { name: "memory_forget" },
  );
}
