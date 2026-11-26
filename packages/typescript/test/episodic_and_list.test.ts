import { describe, it } from "node:test";
import * as assert from "node:assert/strict";

import {
  ApiValidationError,
  AuthError,
  EbbmemIntegrationCore,
  EbbmemTransport,
  IdempotencyConflictError,
  NotFoundError,
  RetryPolicy,
  ScopingResolver,
  SdkClientLike,
  SdkTransportAdapter,
  ServerError,
  UnsupportedCapabilityError,
  ValidationError,
  isIdempotentRequest,
} from "../src";
import { MockServerBridge } from "./helpers/mock_bridge";

function fastRetry(timeoutMs = 2000, maxRetries = 2): RetryPolicy {
  return new RetryPolicy({
    timeoutMs,
    maxRetries,
    backoffBaseMs: 1,
    backoffCapMs: 5,
    jitterFn: () => 0,
  });
}

function makeFakeV020Client(): SdkClientLike {
  return {
    memory: {
      remember: async (content: string) => ({ id: "m-1", content }),
      recall: async (query: string) => ({ query, count: 0, results: [] }),
      get: async (memoryId: string) => ({ id: memoryId }),
      forget: async (memoryId: string, purge = false) => ({
        status: purge ? "purged" : "forgotten",
        memory_id: memoryId,
      }),
      reinforce: async (memoryId: string, delta = 0.1) => ({
        status: "reinforced",
        memory_id: memoryId,
        delta,
      }),
      status: async () => ({ status: "ok" }),
    },
  };
}

describe("TypeScript listMemories & episodic MCP", () => {
  it("enforces isIdempotentRequest rules for GET /api/v1/memory and POST /mcp/full", () => {
    assert.equal(isIdempotentRequest("GET", "/api/v1/memory"), true);
    assert.equal(
      isIdempotentRequest("POST", "/mcp/full", {
        jsonBody: {
          jsonrpc: "2.0",
          id: 1,
          method: "tools/call",
          params: { name: "memory_episodic_tail", arguments: { thread_key: "t1" } },
        },
      }),
      true,
    );
    assert.equal(
      isIdempotentRequest("POST", "/mcp/full", {
        jsonBody: {
          jsonrpc: "2.0",
          id: 1,
          method: "tools/call",
          params: { name: "memory_episodic_remember", arguments: { thread_key: "t1", content: "hi" } },
        },
      }),
      false,
    );
    assert.equal(
      isIdempotentRequest("POST", "/mcp/full", {
        headers: { "Idempotency-Key": "ep-k1" },
        jsonBody: {
          jsonrpc: "2.0",
          id: 1,
          method: "tools/call",
          params: { name: "memory_episodic_remember", arguments: { thread_key: "t1", content: "hi" } },
        },
      }),
      true,
    );
    assert.equal(
      isIdempotentRequest("POST", "/mcp/full", {
        jsonBody: {
          jsonrpc: "2.0",
          id: 1,
          method: "tools/call",
          params: {
            name: "memory_episodic_remember",
            arguments: { thread_key: "t1", content: "hi", idempotency_key: "ep-k2" },
          },
        },
      }),
      true,
    );
  });

  it("guards v0.2.0 SdkTransportAdapter and custom transports missing list/episodic methods", async () => {
    const adapter = new SdkTransportAdapter(makeFakeV020Client());
    assert.equal(adapter.capabilities.listMemories, false);
    assert.equal(adapter.capabilities.episodicRemember, false);
    assert.equal(adapter.capabilities.episodicTail, false);

    await assert.rejects(() => adapter.listMemories(), UnsupportedCapabilityError);
    await assert.rejects(
      () => adapter.episodicRemember({ threadKey: "t1", content: "hi" }),
      UnsupportedCapabilityError,
    );
    await assert.rejects(() => adapter.episodicTail({ threadKey: "t1" }), UnsupportedCapabilityError);

    const v030Client: SdkClientLike = {
      ...makeFakeV020Client(),
      memory: {
        ...makeFakeV020Client().memory,
        listMemories: async () => ({ items: [{ id: "m-1" }], total: 1 }),
        episodicRemember: async (p) => ({ status: "remembered", thread_key: p.threadKey, sequence_id: 1 }),
        episodicTail: async (p) => ({ status: "ok", thread_key: p?.threadKey, count: 0, turns: [] }),
      },
    };
    const v030Adapter = new SdkTransportAdapter(v030Client);
    assert.equal(v030Adapter.capabilities.listMemories, true);
    assert.equal(v030Adapter.capabilities.episodicRemember, true);
    assert.equal(v030Adapter.capabilities.episodicTail, true);
    assert.equal((await v030Adapter.listMemories()).total, 1);
    assert.equal(
      (await v030Adapter.episodicRemember({ threadKey: "t1", content: "hi" })).sequence_id,
      1,
    );
    assert.equal((await v030Adapter.episodicTail({ threadKey: "t1" })).status, "ok");

    const minimalTransport: EbbmemTransport = {
      capabilities: {
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
        notes: [],
      },
      remember: async () => ({}),
      recall: async () => ({ query: "", count: 0, results: [] }),
      reinforce: async () => ({}),
      get: async () => ({}),
      forget: async () => ({}),
      purge: async () => ({}),
      status: async () => ({}),
      health: async () => ({ status: "ok" }),
    };
    const coreMin = new EbbmemIntegrationCore({ transport: minimalTransport });
    await assert.rejects(() => coreMin.listMemories(), UnsupportedCapabilityError);
    await assert.rejects(
      () => coreMin.episodicRemember("hi", { threadKey: "t1" }),
      UnsupportedCapabilityError,
    );
    await assert.rejects(() => coreMin.episodicTail({ threadKey: "t1" }), UnsupportedCapabilityError);
  });

  it("E2E listMemories, episodicRemember, and episodicTail against MockServerBridge", async () => {
    const bridge = await MockServerBridge.start();
    try {
      const alice = new EbbmemIntegrationCore({
        baseUrl: bridge.baseUrl,
        apiKey: "ebb_test_alice_key",
        scope: new ScopingResolver({ tenant: "acme", userId: "alice" }),
        retryPolicy: fastRetry(),
      });
      assert.equal(alice.transport.capabilities.listMemories, true);
      assert.equal(alice.transport.capabilities.episodicRemember, true);
      assert.equal(alice.transport.capabilities.episodicTail, true);

      const m1 = await alice.remember("First TS memory", {
        tier: "semantic",
        tags: ["alpha"],
        importance: 0.9,
        timestampMs: 1000,
        metadata: { source: "ts" },
      });
      const m2 = await alice.remember("Second TS memory", {
        tier: "episodic",
        tags: ["beta"],
        importance: 0.3,
        sessionId: "s-ts",
        timestampMs: 2000,
      });
      const m3 = await alice.remember("Third TS memory", {
        tier: "semantic",
        timestampMs: 3000,
      });

      const pageSizeOnly = await alice.listMemories({ pageSize: 2 });
      assert.equal(pageSizeOnly.total, 3);
      assert.equal((pageSizeOnly.items as unknown[]).length, 2);
      assert.equal(pageSizeOnly.has_more, true);

      const page1 = await alice.listMemories({ limit: 1 });
      assert.equal(page1.total, 3);
      assert.equal(page1.has_more, true);
      assert.deepEqual(
        (page1.items as Array<{ memory_id: string }>).map((x) => x.memory_id),
        [m3.memory_id],
      );

      const page2 = await alice.listMemories({ limit: 1, cursor: String(page1.next_cursor) });
      assert.equal(page2.has_more, true);
      assert.deepEqual(
        (page2.items as Array<{ memory_id: string }>).map((x) => x.memory_id),
        [m2.memory_id],
      );

      const filtered = await alice.listMemories({
        tier: "semantic",
        minImportance: 0.8,
        metadataFilter: { source: "ts" },
      });
      assert.deepEqual(
        (filtered.items as Array<{ memory_id: string }>).map((x) => x.memory_id),
        [m1.memory_id],
      );

      await assert.rejects(() => alice.listMemories({ appNamespace: "other:u:bob" }), NotFoundError);
      await assert.rejects(() => alice.listMemories({ limit: 0 }), ApiValidationError);

      const degradedList = await alice.listMemories({
        headers: { "X-Mock-Behavior": "status_503" },
      });
      assert.equal(degradedList.degraded, true);
      assert.deepEqual(degradedList.items, []);

      // Episodic remember with redaction & idempotency
      const ep1 = await alice.episodicRemember("Bearer ebb_ts_secret_12345", {
        threadId: "ts-thread-1",
        role: "user",
        idempotencyKey: "ts-ep-1",
      });
      assert.equal(ep1.status, "remembered");
      assert.equal(ep1.sequence_id, 1);

      await assert.rejects(
        () =>
          alice.episodicRemember("altered", {
            threadId: "ts-thread-1",
            idempotencyKey: "ts-ep-1",
          }),
        IdempotencyConflictError,
      );

      // Unkeyed episodicRemember never retries on 503; keyed retries 3 times
      const before = (await bridge.getLoggedRequests()).length;
      await assert.rejects(
        () =>
          alice.episodicRemember("no retry", {
            threadId: "ts-thread-1",
            headers: { "X-Mock-Behavior": "status_503" },
          }),
        ServerError,
      );
      const afterUnkeyed = (await bridge.getLoggedRequests()).length;
      assert.equal(afterUnkeyed - before, 1);

      await assert.rejects(
        () =>
          alice.episodicRemember("with retry", {
            threadId: "ts-thread-1",
            idempotencyKey: "ts-ep-retry",
            headers: { "X-Mock-Behavior": "status_503" },
          }),
        ServerError,
      );
      const afterKeyed = (await bridge.getLoggedRequests()).length;
      assert.equal(afterKeyed - afterUnkeyed, 3);

      // Tail reads redacted turn without mutating strength
      const tail = await alice.episodicTail({ threadId: "ts-thread-1", limit: 5 });
      assert.equal(tail.count, 1);
      const turns = tail.turns as Array<{ content: string }>;
      assert.equal(turns[0].content, "Bearer [REDACTED]");

      const raw = await bridge.getMemoryRaw("acme:u:alice", String(ep1.memory_id));
      assert.ok(raw);
      assert.equal(raw.storage_strength, 1.0);

      const degradedTail = await alice.episodicTail({
        threadKey: "ts-thread-1",
        headers: { "X-Mock-Behavior": "status_503" },
      });
      assert.equal(degradedTail.degraded, true);
      assert.deepEqual(degradedTail.turns, []);

      // Missing threadKey rejected
      await assert.rejects(() => alice.episodicRemember("hi"), ValidationError);
      await assert.rejects(() => alice.episodicTail(), ValidationError);

      // Readonly key rejected on episodicRemember, allowed on episodicTail
      const ro = new EbbmemIntegrationCore({
        baseUrl: bridge.baseUrl,
        apiKey: "ebb_test_readonly_key",
        scope: new ScopingResolver({ tenant: "acme", userId: "alice" }),
        retryPolicy: fastRetry(),
      });
      await assert.rejects(
        () => ro.episodicRemember("nope", { threadKey: "ts-thread-1" }),
        AuthError,
      );
      const roTail = await ro.episodicTail({ threadKey: "ts-thread-1" });
      assert.equal(roTail.count, 1);

      // AWS Access Key ID redaction in standalone content and tags
      const awsKey = "AKIAIOSFODNN7EXAMPLE";
      const epAws = await alice.episodicRemember(`aws key ${awsKey}`, {
        threadKey: "ts-aws-sec",
        tags: [`aws:${awsKey}`, awsKey],
      });
      assert.equal(epAws.status, "remembered");
      const awsTail = await alice.episodicTail({ threadKey: "ts-aws-sec", limit: 5 });
      const awsTurns = awsTail.turns as Array<{ content: string; tags: string[] }>;
      assert.equal(awsTurns.length, 1);
      assert.equal(awsTurns[0].content, "aws key [REDACTED]");
      assert.equal(awsTurns[0].content.includes(awsKey), false);
      assert.ok(awsTurns[0].tags.includes("aws:[REDACTED]"));
      assert.ok(awsTurns[0].tags.includes("[REDACTED]"));
      assert.equal(
        awsTurns[0].tags.some((t) => t.includes(awsKey)),
        false,
      );

      // Zero leaked secrets & zero telemetry headers
      const joinedLogs = alice.logSink.join("\n");
      assert.equal(joinedLogs.includes("ebb_test_alice_key"), false);
      assert.equal(joinedLogs.includes("ebb_ts_secret_12345"), false);
      const reqs = await bridge.getLoggedRequests();
      for (const req of reqs) {
        for (const h of req.header_names) {
          assert.equal(h.toLowerCase().startsWith("x-ebb-telemetry"), false);
        }
      }
    } finally {
      await bridge.close();
    }
  });
});
