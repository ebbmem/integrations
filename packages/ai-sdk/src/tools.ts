import { tool } from "ai";
import { z } from "zod";
import {
  EbbmemAiSdkOptions,
  mergeTags,
  resolveCoreAndScope,
} from "./config.js";

export type EbbmemToolsOptions = EbbmemAiSdkOptions;

const memoryTierSchema = z.enum([
  "working",
  "episodic",
  "semantic",
  "procedural",
]);

/**
 * Creates Vercel AI SDK tools (`memory_recall`, `memory_remember`,
 * `memory_reinforce`, `memory_forget`) backed by `EbbmemIntegrationCore`.
 */
export function ebbmemTools(options: EbbmemToolsOptions = {}) {
  const { core, scopeOverrides } = resolveCoreAndScope(options);

  const memory_recall = tool({
    description:
      "Search and recall relevant memories from EbbMem. Strictly read-only (does not boost memory strength). Returns recalled items and untrusted XML-framed context.",
    inputSchema: z.object({
      query: z.string().min(1).describe("Search query describing the needed memory."),
      limit: z
        .number()
        .int()
        .min(1)
        .max(100)
        .optional()
        .describe("Maximum number of memories to return (default 10)."),
      tier: memoryTierSchema.optional().describe("Optional memory tier filter."),
      tags: z.array(z.string()).optional().describe("Optional tags filter."),
      domain: z.string().optional().describe("Optional domain filter."),
      sessionId: z.string().optional().describe("Optional session identifier."),
      profile: z.string().optional().describe("Optional recall profile."),
      minScore: z
        .number()
        .min(0)
        .max(1)
        .optional()
        .describe("Minimum relevance score threshold."),
    }),
    execute: async (input) => {
      const response = await core.recall(input.query, {
        userId: scopeOverrides.userId,
        appNamespace: scopeOverrides.appNamespace,
        sessionId: input.sessionId ?? scopeOverrides.sessionId,
        threadId: scopeOverrides.threadId,
        tags: mergeTags(scopeOverrides.tags, input.tags),
        domain: input.domain ?? scopeOverrides.domain,
        headers: scopeOverrides.headers,
        limit: input.limit,
        tier: input.tier,
        profile: input.profile,
        minScore: input.minScore,
      });
      const results = Array.isArray(response.results) ? response.results : [];
      const framedContext =
        results.length > 0 ? core.frameMemories(response) : "";
      return {
        ...response,
        framedContext,
      };
    },
  });

  const memory_remember = tool({
    description:
      "Store a durable fact, user preference, decision, or episodic note in EbbMem. Secrets are redacted automatically before storage.",
    inputSchema: z.object({
      content: z.string().min(1).describe("The memory text to store."),
      tier: memoryTierSchema
        .optional()
        .describe("Memory tier (default: semantic)."),
      tags: z.array(z.string()).optional().describe("Optional categorization tags."),
      domain: z.string().optional().describe("Optional domain label."),
      sessionId: z.string().optional().describe("Optional session identifier."),
      importance: z
        .number()
        .min(0)
        .max(1)
        .optional()
        .describe("Importance score in [0, 1]."),
      valence: z
        .number()
        .min(-1)
        .max(1)
        .optional()
        .describe("Emotional valence in [-1, 1]."),
      arousal: z
        .number()
        .min(0)
        .max(1)
        .optional()
        .describe("Arousal score in [0, 1]."),
      metadata: z
        .record(z.string(), z.unknown())
        .optional()
        .describe("Optional structured metadata (ebb.* prefix is reserved)."),
      idempotencyKey: z
        .string()
        .optional()
        .describe("Optional idempotency key for safe retries."),
    }),
    execute: async (input) => {
      return core.remember(input.content, {
        userId: scopeOverrides.userId,
        appNamespace: scopeOverrides.appNamespace,
        agentId: scopeOverrides.agentId,
        sessionId: input.sessionId ?? scopeOverrides.sessionId,
        threadId: scopeOverrides.threadId,
        tags: mergeTags(scopeOverrides.tags, input.tags),
        domain: input.domain ?? scopeOverrides.domain,
        headers: scopeOverrides.headers,
        tier: input.tier,
        importance: input.importance,
        valence: input.valence,
        arousal: input.arousal,
        metadata: input.metadata,
        idempotencyKey: input.idempotencyKey,
      });
    },
  });

  const memory_reinforce = tool({
    description:
      "Reinforce memories that were actually used to answer or act (default delta +0.1, bounds [-1.0, 1.0]). Do not reinforce unused memories.",
    inputSchema: z.object({
      memoryId: z.string().min(1).describe("Primary memory ID to reinforce."),
      usedIds: z
        .array(z.string().min(1))
        .optional()
        .describe("Additional used memory IDs from the same turn."),
      recallId: z
        .string()
        .optional()
        .describe("Optional recall_id returned by memory_recall."),
      delta: z
        .number()
        .min(-1)
        .max(1)
        .optional()
        .describe("Reinforcement delta in [-1.0, 1.0] (default 0.1)."),
    }),
    execute: async (input) => {
      return core.reinforce(input.memoryId, {
        userId: scopeOverrides.userId,
        appNamespace: scopeOverrides.appNamespace,
        headers: scopeOverrides.headers,
        usedIds: input.usedIds,
        recallId: input.recallId,
        delta: input.delta,
      });
    },
  });

  const memory_forget = tool({
    description:
      "Soft-delete (tombstone) a memory by ID while preserving audit lineage.",
    inputSchema: z.object({
      memoryId: z.string().min(1).describe("Memory ID to forget."),
    }),
    execute: async (input) => {
      return core.forget(input.memoryId, {
        userId: scopeOverrides.userId,
        appNamespace: scopeOverrides.appNamespace,
        headers: scopeOverrides.headers,
      });
    },
  });

  return {
    memory_recall,
    memory_remember,
    memory_reinforce,
    memory_forget,
  };
}

export type EbbmemToolSet = ReturnType<typeof ebbmemTools>;
