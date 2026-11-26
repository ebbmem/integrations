import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { after, afterEach, before, describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import {
  EbbmemTransport,
  NamespaceError,
  RecallParams,
  ReinforceParams,
  RememberParams,
  SdkCapabilities,
  ServerError,
} from "ebbmem-integration-core";
import {
  MemorySearchHit,
  parsePluginConfig,
  redactPluginPayload,
  redactPluginSecrets,
  register,
  resetActiveEbbmemRuntime,
} from "../src/index.js";
import { MockServerBridge } from "./helpers/mock_bridge.js";
import { MockOpenClawApi } from "./helpers/mock_openclaw_api.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PLUGIN_ROOT = path.resolve(__dirname, "..", "..", "..");

function makeCaps(): SdkCapabilities {
  return {
    tierOnRemember: true,
    sessionIdOnRemember: true,
    metadataOnRemember: true,
    recallFilters: true,
    recallBreakdownAndId: true,
    reinforceUsedIdsAndRecallId: true,
    reinforceDefaultDelta01: true,
    deterministicIdempotencyKey: true,
    exactKeyGet: true,
    exactKeyDelete: true,
    metadataFilterOnListRecall: false,
    namespacePrefixList: false,
    notes: [],
  };
}

class RegressionStubTransport implements EbbmemTransport {
  readonly capabilities = makeCaps();
  readonly rememberCalls: RememberParams[] = [];
  readonly recallCalls: RecallParams[] = [];
  readonly reinforceCalls: ReinforceParams[] = [];
  readonly records = new Map<string, Record<string, unknown>>();
  recallResults: Array<Record<string, unknown>> = [];
  recallId = "rec-reg-1";
  rememberError?: Error;

  async remember(params: RememberParams): Promise<Record<string, unknown>> {
    if (this.rememberError) {
      throw this.rememberError;
    }
    this.rememberCalls.push(params);
    const id = `mem-reg-${this.rememberCalls.length}`;
    const rec: Record<string, unknown> = {
      id,
      memory_id: id,
      namespace: params.namespace ?? "default:u:default",
      tier: params.tier ?? "episodic",
      content: params.content,
      session_id: params.sessionId,
      metadata: params.metadata,
    };
    this.records.set(id, rec);
    return rec;
  }

  async recall(params: RecallParams): Promise<Record<string, unknown>> {
    this.recallCalls.push(params);
    return {
      query: params.query,
      recall_id: this.recallId,
      count: this.recallResults.length,
      results: this.recallResults,
    };
  }

  async reinforce(params: ReinforceParams): Promise<Record<string, unknown>> {
    this.reinforceCalls.push(params);
    return { status: "reinforced", memory_id: params.memoryId, storage_strength: 1.1 };
  }

  async get(memoryId: string): Promise<Record<string, unknown>> {
    return (
      this.records.get(memoryId) ?? {
        id: memoryId,
        memory_id: memoryId,
        namespace: "default:u:default",
        content: "default record",
      }
    );
  }

  async forget(memoryId: string): Promise<Record<string, unknown>> {
    return { status: "forgotten", memory_id: memoryId, tombstoned: true };
  }

  async purge(memoryId: string): Promise<Record<string, unknown>> {
    return { status: "purged", memory_id: memoryId };
  }

  async status(): Promise<Record<string, unknown>> {
    return { status: "ok" };
  }

  async health(): Promise<Record<string, unknown>> {
    return { status: "ok" };
  }
}

let bridge: MockServerBridge;

before(async () => {
  bridge = await MockServerBridge.start();
});

afterEach(async () => {
  await resetActiveEbbmemRuntime();
});

after(async () => {
  if (bridge) {
    await bridge.close();
  }
});

describe("Verifier regression suite (Findings 1-8)", () => {
  it("1. blocks anonymous caller bypass in providerRuntime.search() and get() (stub + live HTTP)", async () => {
    const stub = new RegressionStubTransport();
    stub.recallResults = [
      {
        id: "m-secret",
        memory_id: "m-secret",
        content: "anon leak",
        tier: "semantic",
        namespace: "default:u:default",
      },
    ];
    stub.records.set("m-1", {
      id: "m-1",
      memory_id: "m-1",
      content: "anon leak",
      tier: "semantic",
      namespace: "default:u:default",
    });

    const api = new MockOpenClawApi({});
    const { runtime } = register(api, { transport: stub, env: {} });

    const anonSearch = await runtime.providerRuntime.search({ query: "leak" });
    assert.equal(anonSearch.count, 0);
    assert.deepEqual(anonSearch.results, []);

    const anonGet = await runtime.providerRuntime.get({ memoryId: "m-1" });
    assert.equal(anonGet, null);

    // Explicit caller namespace or userId succeeds when authorized
    const explicitSearch = await runtime.providerRuntime.search({
      query: "leak",
      namespace: "default:u:default",
    });
    assert.equal(explicitSearch.count, 1);

    // Live HTTP MockServerBridge verification
    const writerApi = new MockOpenClawApi({
      baseUrl: bridge.baseUrl,
      apiKey: "ebb_test_admin_key",
    });
    const { runtime: writerRuntime } = register(writerApi, { env: {} });
    const created = await writerRuntime.remember("sensitive default record", {
      tier: "semantic",
      namespace: "default:u:default",
    });
    const createdId = String(created.memory_id);
    await writerRuntime.close();

    const anonLiveApi = new MockOpenClawApi({
      baseUrl: bridge.baseUrl,
      apiKey: "ebb_test_admin_key",
    });
    const { runtime: anonLiveRuntime } = register(anonLiveApi, { env: {} });
    const liveSearch = await anonLiveRuntime.providerRuntime.search({ query: "sensitive default" });
    assert.equal(liveSearch.count, 0);
    assert.deepEqual(liveSearch.results, []);

    const liveGet = await anonLiveRuntime.providerRuntime.get({ memoryId: createdId });
    assert.equal(liveGet, null);
  });

  it("2. enforces record.agent_id/agentId in providerRuntime.get() and forwards caller context in readFile()", async () => {
    const stub = new RegressionStubTransport();
    stub.records.set("m-victim-snake", {
      memory_id: "m-victim-snake",
      namespace: "shared:team",
      agent_id: "victim-agent",
      tier: "semantic",
      content: "victim secret snake",
    });
    stub.records.set("m-victim-camel", {
      memory_id: "m-victim-camel",
      namespace: "shared:team",
      agentId: "victim-agent",
      tier: "semantic",
      content: "victim secret camel",
    });
    stub.records.set("m-victim-working", {
      memory_id: "m-victim-working",
      namespace: "shared:team",
      agent_id: "victim-agent",
      sessionId: "sess-owner",
      tier: "working",
      content: "victim working note",
    });

    const api = new MockOpenClawApi({ appNamespace: "shared:team" });
    const { runtime } = register(api, { transport: stub, env: {} });

    assert.equal(
      await runtime.providerRuntime.get({ memoryId: "m-victim-snake", agentId: "attacker-agent" }),
      null,
    );
    assert.equal(
      await runtime.providerRuntime.get({ memoryId: "m-victim-camel", agentId: "attacker-agent" }),
      null,
    );

    const okOwner = await runtime.providerRuntime.get({
      memoryId: "m-victim-snake",
      agentId: "victim-agent",
    });
    assert.equal(okOwner?.content, "victim secret snake");

    const attackerMgr = await runtime.providerRuntime.getMemorySearchManager?.({
      agentId: "attacker-agent",
    });
    assert.ok(attackerMgr?.manager?.readFile);
    const attackerRead = await attackerMgr!.manager!.readFile!({ memoryId: "m-victim-snake" });
    assert.equal(attackerRead.text, "");

    const ownerMgr = await runtime.providerRuntime.getMemorySearchManager?.({
      agentId: "victim-agent",
    });
    const wrongSessRead = await ownerMgr!.manager!.readFile!({
      memoryId: "m-victim-working",
      sessionId: "sess-other",
    });
    assert.equal(wrongSessRead.text, "");

    const rightSessRead = await ownerMgr!.manager!.readFile!({
      memoryId: "m-victim-working",
      sessionId: "sess-owner",
      userId: "u1",
      namespace: "shared:team",
    });
    assert.equal(rightSessRead.text, "victim working note");
  });

  it("3. handles Gateway reload sequence api1 -> api2 -> api3 -> api1 cleanly without duplicating handlers", async () => {
    const stub = new RegressionStubTransport();
    stub.recallResults = [
      {
        id: "m-reload",
        memory_id: "m-reload",
        content: "Reloaded memory",
        tier: "semantic",
        namespace: "acme:u:alice",
      },
    ];
    const cfg = { tenant: "acme", userId: "alice", autoCapture: false };

    const api1 = new MockOpenClawApi(cfg);
    const api2 = new MockOpenClawApi(cfg);
    const api3 = new MockOpenClawApi(cfg);

    const r1 = register(api1, { transport: stub, env: {} });
    const r2 = register(api2, { transport: stub, env: {} });
    const r3 = register(api3, { transport: stub, env: {} });
    const r4 = register(api1, { transport: stub, env: {} });

    assert.equal(r1.runtime.isClosed(), true);
    assert.equal(r2.runtime.isClosed(), true);
    assert.equal(r3.runtime.isClosed(), true);
    assert.equal(r4.reused, false);
    assert.equal(r4.runtime.isClosed(), false);

    // Calling register(api1) again while r4 is active reuses r4
    const r4Again = register(api1, { transport: stub, env: {} });
    assert.equal(r4Again.reused, true);
    assert.equal(r4Again.runtime, r4.runtime);

    // Hook and tool registrations on api1 were not duplicated
    assert.equal(api1.hooks.get("before_agent_start")?.length, 1);
    assert.equal(api1.toolEntries.filter((t) => t.name === "memory_search").length, 1);

    // Hooks and tools on api1 execute against live r4 exactly once
    const hookRes = await api1.dispatchHook(
      "before_agent_start",
      { userText: "find reloaded memory" },
      { userId: "alice", sessionId: "s-reload" },
    );
    assert.equal(hookRes.length, 1);
    assert.ok(String((hookRes[0] as { prependContext?: string }).prependContext).includes("Reloaded memory"));
    assert.equal(stub.recallCalls.length, 1);

    const toolRes = await api1.invokeTool(
      "memory_search",
      { query: "find reloaded memory" },
      { userId: "alice", sessionId: "s-reload" },
    );
    assert.equal(toolRes.details?.count, 1);
    assert.equal(stub.recallCalls.length, 2);
  });

  it("4. registers --agent-namespace on init CLI command and rejects unknown CLI options", async () => {
    const api = new MockOpenClawApi({ tenant: "acme", userId: "alice" });
    register(api, { env: {} });

    const tmpDir = fs.mkdtempSync(path.join(PLUGIN_ROOT, "build", "cli-test-"));
    const cfgPath = path.join(tmpDir, "openclaw.json");
    try {
      await api.runCli(
        ["ebbmem", "init"],
        {
          mode: "docker",
          agentNamespace: ["ops=acme:a:ops", "qa=acme:a:qa"],
          configPath: cfgPath,
          yes: true,
        },
        cfgPath,
      );
      const written = JSON.parse(fs.readFileSync(cfgPath, "utf8")) as Record<string, any>;
      assert.deepEqual(written.plugins.entries.ebbmem.config.agentNamespaces, {
        ops: "acme:a:ops",
        qa: "acme:a:qa",
      });

      await assert.rejects(
        () => api.runCli(["ebbmem", "init"], { unknownOption: "bad", configPath: cfgPath, yes: true }),
        /Unknown CLI option/,
      );
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  it("5. prevents cross-session and cross-turn stale exposure leakage (with and without agentId/sessionId)", async () => {
    // 5a. Cross-session isolation when session s2 has 0 recall hits (even with injected=true)
    const stub = new RegressionStubTransport();
    const apiInjected = new MockOpenClawApi({
      tenant: "acme",
      userId: "alice",
      autoCapture: false,
      injected: true,
    });
    register(apiInjected, { transport: stub, env: {} });

    stub.recallResults = [
      { id: "m-s1", memory_id: "m-s1", content: "Session 1 fact", tier: "semantic", namespace: "acme:u:alice" },
    ];
    await apiInjected.dispatchHook(
      "before_agent_start",
      { userText: "Session 1 question", turnId: "t-s1" },
      { userId: "alice", sessionId: "s1" },
    );

    stub.recallResults = [];
    await apiInjected.dispatchHook(
      "before_agent_start",
      { userText: "Session 2 question", turnId: "t-s2" },
      { userId: "alice", sessionId: "s2" },
    );
    await apiInjected.dispatchHook(
      "agent_end",
      {
        success: true,
        messages: [
          { role: "user", content: "Session 2 question" },
          { role: "assistant", content: "Mentioning m-s1 without recalling it." },
        ],
      },
      { userId: "alice", sessionId: "s2" },
    );
    assert.equal(stub.reinforceCalls.length, 0);

    await resetActiveEbbmemRuntime();

    // 5b. Live HTTP cross-turn stale exposure when agentId and sessionId are omitted (userId-only)
    const ns = "acme:u:staleuser";
    const apiLive1 = new MockOpenClawApi({
      baseUrl: bridge.baseUrl,
      apiKey: "ebb_test_admin_key",
      tenant: "acme",
      userId: "staleuser",
      autoCapture: false,
      autoRecall: true,
    });
    const { runtime: rtLive1 } = register(apiLive1, { env: {} });
    const created = await rtLive1.remember("Stale exposure anchor memory.", { tier: "semantic" });
    const memId = String(created.memory_id);

    // Turn 1 recalls memId, assistant does NOT cite it
    await apiLive1.dispatchHook("before_agent_start", { userText: "anchor memory" }, {});
    await apiLive1.dispatchHook(
      "agent_end",
      {
        success: true,
        messages: [
          { role: "user", content: "anchor memory" },
          { role: "assistant", content: "No citation on turn 1." },
        ],
      },
      {},
    );

    // Simulate Turn 2 skipping recall (e.g. setup phrase or autoRecall=false) while referencing memId
    await apiLive1.dispatchHook("before_agent_start", { userText: "configure ebbmem" }, {});
    await apiLive1.dispatchHook(
      "agent_end",
      {
        success: true,
        messages: [
          { role: "user", content: "configure ebbmem" },
          { role: "assistant", content: `Referring to ${memId} on turn 2.` },
        ],
      },
      {},
    );

    const raw = await bridge.getMemoryRaw(ns, memId);
    assert.equal(raw?.storage_strength, 1.0);

    // Direct reinforceFromExposure without active turn exposure must be a no-op
    const directRes = await rtLive1.reinforceFromExposure({ responseText: memId });
    assert.equal(directRes, undefined);
    const rawAfterDirect = await bridge.getMemoryRaw(ns, memId);
    assert.equal(rawAfterDirect?.storage_strength, 1.0);
  });

  it("6. rejects conflicting identity fields and prototype-inherited properties in authorizeSearchHits & get()", async () => {
    const stub = new RegressionStubTransport();
    const api = new MockOpenClawApi({
      tenant: "acme",
      agentNamespaces: { a1: "acme:a:a1" },
    });
    const { runtime } = register(api, { transport: stub, env: {} });

    const protoHit = Object.create({ namespace: "acme:a:a1", agent_id: "a1" }) as MemorySearchHit;
    protoHit.id = "proto-hit";
    protoHit.content = "inherited ns";
    protoHit.tier = "semantic";

    const protoMeta = Object.create({ namespace: "acme:a:a1" }) as Record<string, unknown>;

    const badHits: MemorySearchHit[] = [
      {
        id: "conflict-ns",
        content: "ns conflict",
        tier: "semantic",
        namespace: "acme:a:a1",
        agent_id: "a1",
        metadata: { namespace: "acme:a:a2" },
      },
      {
        id: "conflict-prov-ns",
        content: "prov ns conflict",
        tier: "semantic",
        namespace: "acme:a:a1",
        agent_id: "a1",
        provenance: { namespace: "acme:a:a2" },
      },
      {
        id: "conflict-agent",
        content: "agent conflict",
        tier: "semantic",
        namespace: "acme:a:a1",
        agent_id: "a1",
        agentId: "a2",
      },
      {
        id: "conflict-meta-agent",
        content: "meta agent conflict",
        tier: "semantic",
        namespace: "acme:a:a1",
        agent_id: "a1",
        metadata: { agentId: "a2" },
      },
      {
        id: "conflict-session",
        content: "session conflict",
        tier: "semantic",
        namespace: "acme:a:a1",
        agent_id: "a1",
        session_id: "s1",
        sessionId: "s2",
      },
      protoHit,
      {
        id: "proto-meta-hit",
        content: "proto meta",
        tier: "semantic",
        metadata: protoMeta,
      },
    ];

    assert.deepEqual(
      runtime.authorizeSearchHits(badHits, { agentId: "a1", sessionId: "s1" }),
      [],
    );

    stub.records.set("m-conflict", {
      memory_id: "m-conflict",
      namespace: "acme:a:a1",
      agent_id: "a1",
      agentId: "a2",
      tier: "semantic",
      content: "spoofed",
    });
    assert.equal(
      await runtime.providerRuntime.get({ memoryId: "m-conflict", agentId: "a1" }),
      null,
    );
  });

  it("7. redacts AWS-style access keys and secret assignments across remember, agent_end, before_compaction, and errors", async () => {
    const awsKeyId = "AKIA" + "IOSFODNN7EXAMPLE";
    const awsTempId = "ASIA" + "IOSFODNN7EXAMPLE";
    const awsSecretVal = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY";
    const sample = `${awsKeyId} ${awsTempId} AWS_SECRET_ACCESS_KEY=${awsSecretVal} aws_secret_access_key: ${awsSecretVal}`;
    const redacted = redactPluginSecrets(sample);
    assert.equal(redacted.includes(awsKeyId), false);
    assert.equal(redacted.includes(awsTempId), false);
    assert.equal(redacted.includes(awsSecretVal), false);

    const payload = redactPluginPayload({
      aws_secret_access_key: awsSecretVal,
      nested: { note: `key=${awsKeyId}` },
    }) as Record<string, any>;
    assert.equal(payload.aws_secret_access_key, "[REDACTED]");
    assert.equal(String(payload.nested.note).includes(awsKeyId), false);

    const stub = new RegressionStubTransport();
    const api = new MockOpenClawApi({ tenant: "acme", userId: "alice" });
    register(api, { transport: stub, env: {} });

    // Tool path: memory_remember
    await api.invokeTool(
      "memory_remember",
      {
        content: `Creds ${awsKeyId} AWS_SECRET_ACCESS_KEY=${awsSecretVal}`,
        metadata: { aws_secret_access_key: awsSecretVal },
      },
      { userId: "alice", sessionId: "s-aws" },
    );

    // Hook path: agent_end autoCapture
    await api.dispatchHook(
      "agent_end",
      {
        success: true,
        turnId: "t-aws-1",
        messages: [
          { role: "user", content: `My temp key is ${awsTempId}` },
          { role: "assistant", content: `aws_secret_access_key: ${awsSecretVal}` },
        ],
      },
      { userId: "alice", sessionId: "s-aws" },
    );

    // Hook path: before_compaction flush
    await api.dispatchHook(
      "before_compaction",
      {
        messages: [{ role: "user", content: `Flush ${awsKeyId} AWS_SECRET_ACCESS_KEY=${awsSecretVal}` }],
      },
      { userId: "alice", sessionId: "s-aws" },
    );

    assert.equal(stub.rememberCalls.length, 3);
    for (const call of stub.rememberCalls) {
      const serialized = JSON.stringify(call);
      assert.equal(serialized.includes(awsKeyId), false);
      assert.equal(serialized.includes(awsTempId), false);
      assert.equal(serialized.includes(awsSecretVal), false);
      assert.ok(serialized.includes("[REDACTED]"));
    }

    // Error logging path
    stub.rememberError = new ServerError(`Upstream rejected ${awsKeyId} AWS_SECRET_ACCESS_KEY=${awsSecretVal}`, {
      status: 500,
    });
    const errTool = await api.invokeTool(
      "memory_remember",
      { content: "trigger error" },
      { userId: "alice" },
    );
    assert.equal(errTool.isError, true);
    assert.equal(errTool.content[0].text.includes(awsKeyId), false);
    assert.equal(errTool.content[0].text.includes(awsSecretVal), false);
  });

  it("8. degrades gracefully on invalid ctx.namespace when failOpen=true while validating static config", async () => {
    assert.throws(
      () => parsePluginConfig({ appNamespace: "invalid namespace !" }, {}),
      NamespaceError,
    );

    const stub = new RegressionStubTransport();
    const api = new MockOpenClawApi({ tenant: "acme", userId: "alice", failOpen: true });
    const { runtime } = register(api, { transport: stub, env: {} });

    const hookRes = await api.dispatchHook(
      "before_agent_start",
      { userText: "hello" },
      { namespace: "invalid namespace !" },
    );
    assert.equal(hookRes[0], undefined);

    const searchRes = await runtime.providerRuntime.search({
      query: "hello",
      namespace: "invalid namespace !",
    });
    assert.equal(searchRes.count, 0);
    assert.equal(searchRes.degraded, true);
    assert.deepEqual(searchRes.results, []);

    await resetActiveEbbmemRuntime();
    const strictApi = new MockOpenClawApi({ tenant: "acme", userId: "alice", failOpen: false });
    const { runtime: strictRt } = register(strictApi, { transport: stub, env: {} });
    await assert.rejects(
      () => strictRt.providerRuntime.search({ query: "hello", namespace: "invalid namespace !" }),
      NamespaceError,
    );
  });
});
