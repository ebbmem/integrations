import {
  frameRecalledMemories,
  makeIdempotencyKey,
  redactSecrets,
} from "ebbmem-integration-core";
import { buildSetupGuidance, matchSetupPhrase } from "./cli.js";
import { resolveScopeForContext } from "./config.js";
import {
  OpenClawAgentEndEvent,
  OpenClawBeforeAgentStartEvent,
  OpenClawBeforeAgentStartResult,
  OpenClawBeforeCompactionEvent,
  OpenClawHookContext,
  OpenClawPluginApi,
} from "./openclaw-types.js";
import { EbbmemOpenClawRuntime } from "./runtime.js";

function extractTextFromContent(content: unknown): string {
  if (typeof content === "string") {
    return content.trim();
  }
  if (Array.isArray(content)) {
    const parts: string[] = [];
    for (const block of content) {
      if (typeof block === "string") {
        if (block.trim()) parts.push(block.trim());
      } else if (block && typeof block === "object") {
        const obj = block as Record<string, unknown>;
        if (obj.type === "text" && typeof obj.text === "string" && obj.text.trim()) {
          parts.push(obj.text.trim());
        } else if (typeof obj.content === "string" && obj.content.trim()) {
          parts.push(obj.content.trim());
        }
      }
    }
    return parts.join("\n");
  }
  return "";
}

function findLatestByRole(messages: unknown[] | undefined, role: "user" | "assistant"): string {
  if (!Array.isArray(messages)) return "";
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (!msg || typeof msg !== "object") continue;
    const obj = msg as Record<string, unknown>;
    if (obj.role === role) {
      const text = extractTextFromContent(obj.content ?? obj.text);
      if (text) return text;
    }
  }
  return "";
}

function summarizeMessagesForFlush(messages: unknown[] | undefined): string {
  if (!Array.isArray(messages) || messages.length === 0) return "";
  const lines: string[] = [];
  for (const msg of messages.slice(-10)) {
    if (!msg || typeof msg !== "object") continue;
    const obj = msg as Record<string, unknown>;
    const role = typeof obj.role === "string" ? obj.role : "message";
    if (role !== "user" && role !== "assistant") continue;
    const text = extractTextFromContent(obj.content ?? obj.text);
    if (text) {
      lines.push(`${role === "user" ? "User" : "Assistant"}: ${text}`);
    }
  }
  return lines.join("\n").trim();
}

export function registerEbbmemHooks(
  api: OpenClawPluginApi,
  runtime: EbbmemOpenClawRuntime,
): void {
  api.on(
    "before_agent_start",
    async (
      event: OpenClawBeforeAgentStartEvent,
      ctx: OpenClawHookContext,
    ): Promise<OpenClawBeforeAgentStartResult | void> => {
      if (runtime.isClosed()) return;
      const userText =
        (typeof event.userText === "string" && event.userText.trim()) ||
        (typeof event.prompt === "string" && event.prompt.trim()) ||
        findLatestByRole(event.messages, "user");

      if (!userText) return;

      if (matchSetupPhrase(userText)) {
        return {
          prependContext: buildSetupGuidance(runtime.config),
        };
      }

      if (!runtime.config.autoRecall) return;

      const searchRes = await runtime.search({
        query: userText,
        limit: runtime.config.recallLimit,
        agentId: ctx.agentId,
        sessionKey: ctx.sessionKey,
        sessionId: ctx.sessionId,
        userId: ctx.userId,
        namespace: ctx.namespace,
        turnId: event.turnId ?? ctx.turnId,
      });

      if (searchRes.results.length === 0) {
        return;
      }

      return {
        prependContext: frameRecalledMemories(searchRes.results),
      };
    },
  );

  api.on(
    "agent_end",
    async (event: OpenClawAgentEndEvent, ctx: OpenClawHookContext): Promise<void> => {
      if (runtime.isClosed() || event.success === false) {
        return;
      }

      const messages = event.messagesSnapshot ?? event.messages;
      const userText = findLatestByRole(messages, "user");
      const assistantText = findLatestByRole(messages, "assistant");
      const scope = resolveScopeForContext(runtime.config, ctx);

      if (runtime.config.autoCapture && runtime.core && (userText || assistantText)) {
        if (!matchSetupPhrase(userText)) {
          const parts: string[] = [];
          if (userText) parts.push(`User: ${userText}`);
          if (assistantText) parts.push(`Assistant: ${assistantText}`);
          const formatted = parts.join("\n");
          const turnKey = event.turnId ?? ctx.turnId ?? "latest";
          const idempotencyKey = makeIdempotencyKey([
            scope.namespace,
            scope.sessionId ?? "",
            turnKey,
            formatted,
          ]);
          try {
            await runtime.remember(formatted, {
              tier: runtime.config.captureTier,
              tags: ["openclaw", "auto-capture"],
              idempotencyKey,
              agentId: ctx.agentId,
              sessionKey: ctx.sessionKey,
              sessionId: ctx.sessionId,
              userId: ctx.userId,
              namespace: ctx.namespace,
            });
          } catch (err) {
            if (!runtime.config.failOpen) throw err;
            const msg = err instanceof Error ? err.message : String(err);
            runtime.logger?.warn(`ebbmem auto-capture fail-open: ${redactSecrets(msg)}`);
          }
        }
      }

      if (runtime.config.autoReinforce && assistantText) {
        try {
          await runtime.reinforceFromExposure({
            responseText: assistantText,
            agentId: ctx.agentId,
            sessionKey: ctx.sessionKey,
            sessionId: ctx.sessionId,
            userId: ctx.userId,
            namespace: ctx.namespace,
          });
        } catch (err) {
          if (!runtime.config.failOpen) throw err;
          const msg = err instanceof Error ? err.message : String(err);
          runtime.logger?.warn(`ebbmem auto-reinforce fail-open: ${redactSecrets(msg)}`);
        }
      }
    },
  );

  api.on(
    "before_compaction",
    async (event: OpenClawBeforeCompactionEvent, ctx: OpenClawHookContext): Promise<void> => {
      if (runtime.isClosed() || !runtime.config.preCompactionFlush || !runtime.core) {
        return;
      }
      const summary = summarizeMessagesForFlush(event.messages);
      if (!summary) return;

      const mergedCtx = {
        agentId: event.agentId ?? ctx.agentId,
        sessionKey: event.sessionKey ?? ctx.sessionKey,
        sessionId: event.sessionId ?? ctx.sessionId,
        userId: ctx.userId,
        namespace: ctx.namespace,
      };
      const scope = resolveScopeForContext(runtime.config, mergedCtx);
      const idempotencyKey = makeIdempotencyKey([
        scope.namespace,
        scope.sessionId ?? "",
        "before_compaction",
        summary,
      ]);

      try {
        await runtime.remember(summary, {
          tier: "episodic",
          tags: ["openclaw", "compaction-flush"],
          idempotencyKey,
          ...mergedCtx,
        });
      } catch (err) {
        if (!runtime.config.failOpen) throw err;
        const msg = err instanceof Error ? err.message : String(err);
        runtime.logger?.warn(`ebbmem before_compaction fail-open: ${redactSecrets(msg)}`);
      }
    },
  );
}
