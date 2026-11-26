import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { afterEach, describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import {
  CredentialConflictError,
  CredentialInUrlError,
  EbbmemTransport,
  NamespaceError,
  RecallParams,
  ReinforceParams,
  RememberParams,
  ReservedMetadataError,
  SdkCapabilities,
  ValidationError,
} from "ebbmem-integration-core";
import {
  DEFAULT_LOCAL_DOCKER_URL,
  MemorySearchHit,
  buildSetupGuidance,
  matchSetupPhrase,
  parsePluginConfig,
  register,
  resetActiveEbbmemRuntime,
  resolveScopeForContext,
  runInitWizard,
} from "../src/index.js";
import { MockOpenClawApi } from "./helpers/mock_openclaw_api.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PLUGIN_ROOT = path.resolve(__dirname, "..", "..", "..");

function makeStubCapabilities(): SdkCapabilities {
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

class StubTransport implements EbbmemTransport {
  readonly capabilities = makeStubCapabilities();
  readonly reinforceCalls: ReinforceParams[] = [];
  readonly rememberCalls: RememberParams[] = [];
  readonly recallCalls: RecallParams[] = [];
  recallResults: Array<Record<string, unknown>> = [];
  recallId = "rec-unit-1";

  async remember(params: RememberParams): Promise<Record<string, unknown>> {
    this.rememberCalls.push(params);
    return {
      id: `mem-${this.rememberCalls.length}`,
      memory_id: `mem-${this.rememberCalls.length}`,
      namespace: params.namespace ?? "default:u:default",
      tier: params.tier ?? "episodic",
    };
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
    return { id: memoryId, memory_id: memoryId, content: "stub", namespace: "default:u:alice" };
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

afterEach(async () => {
  await resetActiveEbbmemRuntime();
});

describe("OpenClaw EbbMem config, security & scope defaults", () => {
  it("enforces safe defaults with injected=false and telemetryEnabled=false", () => {
    const cfg = parsePluginConfig({}, {});
    assert.equal(cfg.injected, false);
    assert.equal(cfg.telemetryEnabled, false);
    assert.equal(cfg.redactBeforeRemember, true);
    assert.equal(cfg.failOpen, true);
    assert.equal(cfg.reinforceMode, "cited");
    assert.equal(cfg.reinforceDelta, 0.1);
    assert.equal(cfg.captureTier, "episodic");
    assert.equal(cfg.autoRecall, true);
    assert.equal(cfg.autoCapture, true);
    assert.equal(cfg.autoReinforce, true);
    assert.equal(cfg.preCompactionFlush, true);
  });

  it("rejects credentials in baseUrl and conflicting bearerToken + apiKey", () => {
    assert.throws(
      () => parsePluginConfig({ baseUrl: "https://ebbmem.example.com?token=secret123" }, {}),
      CredentialInUrlError,
    );
    assert.throws(
      () => parsePluginConfig({ baseUrl: "https://ebbmem.example.com?api_key=abc" }, {}),
      CredentialInUrlError,
    );
    assert.throws(
      () => parsePluginConfig({ baseUrl: "https://user:pass@ebbmem.example.com" }, {}),
      CredentialInUrlError,
    );
    assert.throws(
      () =>
        parsePluginConfig(
          {
            baseUrl: "https://ebbmem.example.com",
            apiKey: "ebb_key",
            bearerToken: "tok_123",
          },
          {},
        ),
      CredentialConflictError,
    );
    assert.throws(
      () => parsePluginConfig({ reinforceDelta: 2.5 }, {}),
      ValidationError,
    );
  });

  it("resolves per-agent namespaces, user fallbacks, and namespace conflicts", () => {
    const cfg = parsePluginConfig(
      {
        tenant: "acme",
        userId: "alice",
        appNamespace: "acme:shared",
        agentNamespaces: {
          ops: "acme:a:ops",
        },
      },
      {},
    );
    assert.equal(resolveScopeForContext(cfg, { agentId: "ops" }).namespace, "acme:a:ops");
    assert.equal(resolveScopeForContext(cfg, { agentId: "other" }).namespace, "acme:shared");

    const noAppCfg = parsePluginConfig({ tenant: "acme" }, {});
    assert.equal(
      resolveScopeForContext(noAppCfg, { agentId: "planner" }).namespace,
      "acme:a:planner",
    );
    assert.equal(
      resolveScopeForContext(noAppCfg, { userId: "bob" }).namespace,
      "acme:u:bob",
    );
    assert.throws(
      () =>
        resolveScopeForContext(cfg, {
          headerNamespace: "acme:a:ops",
          bodyNamespace: "acme:a:other",
        }),
      NamespaceError,
    );
  });

  it("rejects reserved ebb.* metadata keys when remembering", async () => {
    const stub = new StubTransport();
    const api = new MockOpenClawApi({ tenant: "acme", userId: "alice" });
    register(api, { transport: stub, env: {} });

    const res = await api.invokeTool(
      "memory_remember",
      {
        content: "User prefers dark mode",
        metadata: { "ebb.internal": "forbidden" },
      },
      { agentId: "main" },
    );
    assert.equal(res.isError, true);
    assert.equal(stub.rememberCalls.length, 0);
    assert.throws(() => {
      throw new ReservedMetadataError("ebb. prefix");
    }, ReservedMetadataError);
  });
});

describe("Fail-closed authorizeSearchHits & default-off reinforcement", () => {
  it("drops hits missing namespace, mismatched namespace/agent, or cross-session working hits", () => {
    const stub = new StubTransport();
    const api = new MockOpenClawApi({
      tenant: "acme",
      agentNamespaces: { alpha: "acme:a:alpha" },
    });
    const { runtime } = register(api, { transport: stub, env: {} });

    const hits: MemorySearchHit[] = [
      {
        id: "hit-semantic-ok",
        content: "User prefers concise TypeScript examples.",
        tier: "semantic",
        namespace: "acme:a:alpha",
        agent_id: "alpha",
        session_id: "sess-old",
      },
      {
        id: "hit-missing-ns",
        content: "Missing namespace",
        tier: "semantic",
      },
      {
        id: "hit-wrong-ns",
        content: "Wrong namespace",
        tier: "semantic",
        namespace: "other:a:beta",
        agent_id: "alpha",
      },
      {
        id: "hit-wrong-agent",
        content: "Wrong agent",
        tier: "semantic",
        namespace: "acme:a:alpha",
        agent_id: "beta",
      },
      {
        id: "hit-working-other-session",
        content: "Working scratchpad from another session",
        tier: "working",
        namespace: "acme:a:alpha",
        agent_id: "alpha",
        session_id: "sess-other",
      },
      {
        id: "hit-working-same-session",
        content: "Working scratchpad for current session",
        tier: "working",
        namespace: "acme:a:alpha",
        agent_id: "alpha",
        session_id: "sess-now",
      },
      {
        id: "hit-session-scoped-meta",
        content: "Session scoped episodic note",
        tier: "episodic",
        namespace: "acme:a:alpha",
        agent_id: "alpha",
        session_id: "sess-other",
        metadata: { session_scoped: true },
      },
    ];

    const allowed = runtime.authorizeSearchHits(hits, {
      agentId: "alpha",
      sessionId: "sess-now",
    });
    assert.deepEqual(
      allowed.map((h) => h.id),
      ["hit-semantic-ok", "hit-working-same-session"],
    );

    const strictSession = runtime.authorizeSearchHits(hits, {
      agentId: "alpha",
      sessionId: "sess-now",
      requireSessionMatch: true,
    });
    assert.deepEqual(
      strictSession.map((h) => h.id),
      ["hit-working-same-session"],
    );

    // Anonymous / unidentified caller with no explicit scope must fail closed
    const unauthApi = new MockOpenClawApi({ tenant: "acme" });
    const { runtime: anonRuntime } = register(unauthApi, { transport: stub, env: {} });
    assert.deepEqual(anonRuntime.authorizeSearchHits(hits, {}), []);
  });

  it("proves default-off injected=false reinforces only cited memories in agent_end", async () => {
    const stub = new StubTransport();
    stub.recallResults = [
      {
        id: "mem-cited-1",
        memory_id: "mem-cited-1",
        content: "Deployment uses blue-green rollouts.",
        tier: "semantic",
        provenance: { namespace: "acme:u:alice" },
      },
      {
        id: "mem-unused-2",
        memory_id: "mem-unused-2",
        content: "User likes espresso.",
        tier: "semantic",
        provenance: { namespace: "acme:u:alice" },
      },
    ];
    const api = new MockOpenClawApi({
      tenant: "acme",
      userId: "alice",
      autoCapture: false,
    });
    register(api, { transport: stub, env: {} });

    await api.dispatchHook(
      "before_agent_start",
      { userText: "How do we deploy?", turnId: "t1" },
      { userId: "alice", sessionId: "s1" },
    );

    // Case 1: Assistant cites only mem-cited-1 -> only mem-cited-1 reinforced
    await api.dispatchHook(
      "agent_end",
      {
        success: true,
        messages: [
          { role: "user", content: "How do we deploy?" },
          { role: "assistant", content: "Per memory mem-cited-1, we use blue-green rollouts." },
        ],
      },
      { userId: "alice", sessionId: "s1" },
    );
    assert.equal(stub.reinforceCalls.length, 1);
    assert.equal(stub.reinforceCalls[0].memoryId, "mem-cited-1");
    assert.deepEqual(stub.reinforceCalls[0].usedIds, []);
    assert.equal(stub.reinforceCalls[0].recallId, "rec-unit-1");

    // Case 2: Assistant cites neither memory -> zero reinforce calls
    stub.reinforceCalls.length = 0;
    await api.dispatchHook(
      "before_agent_start",
      { userText: "Any updates?", turnId: "t2" },
      { userId: "alice", sessionId: "s2" },
    );
    await api.dispatchHook(
      "agent_end",
      {
        success: true,
        messages: [
          { role: "user", content: "Any updates?" },
          { role: "assistant", content: "Everything looks good today." },
        ],
      },
      { userId: "alice", sessionId: "s2" },
    );
    assert.equal(stub.reinforceCalls.length, 0);
  });
});

describe("Manifest, TypeBox tools, CLI wizard, chat setup & reload idempotency", () => {
  it("aligns openclaw.plugin.json, package.json, SKILL.md, and registered tools", () => {
    const manifest = JSON.parse(
      fs.readFileSync(path.join(PLUGIN_ROOT, "openclaw.plugin.json"), "utf8"),
    ) as Record<string, any>;
    const pkg = JSON.parse(
      fs.readFileSync(path.join(PLUGIN_ROOT, "package.json"), "utf8"),
    ) as Record<string, any>;
    const skill = fs.readFileSync(
      path.join(PLUGIN_ROOT, "skills", "ebbmem-memory", "SKILL.md"),
      "utf8",
    );

    assert.equal(manifest.id, "ebbmem");
    assert.equal(manifest.kind, "memory");
    assert.deepEqual(pkg.openclaw?.extensions, ["./dist/index.js"]);
    assert.equal(manifest.configSchema.properties.injected.default, false);
    assert.equal(manifest.configSchema.properties.telemetryEnabled.default, false);
    assert.equal(manifest.configSchema.properties.redactBeforeRemember.default, true);
    assert.equal(manifest.configSchema.properties.failOpen.default, true);
    assert.equal(manifest.uiHints.apiKey.sensitive, true);
    assert.equal(manifest.uiHints.bearerToken.sensitive, true);
    assert.ok(skill.includes("memory_search") && skill.includes("memory_get"));

    const api = new MockOpenClawApi({});
    const { runtime } = register(api, { env: {} });
    assert.deepEqual(api.registeredToolNames, manifest.contracts.tools);
    assert.equal(api.capabilities.length, 1);
    assert.equal(api.capabilities[0].providerRuntime.providerId, "ebbmem");

    const flushPlan = runtime.flushPlanResolver({ agentId: "main" }) as Record<string, unknown>;
    assert.ok(flushPlan);
    assert.equal("model" in flushPlan, false);
    assert.equal(flushPlan.redactSecrets, true);
    assert.equal(flushPlan.targetTool, "memory_remember");
  });

  it("supports CLI init wizard (remote & docker), /ebbmem setup, and chat setup phrase", async () => {
    const files = new Map<string, string>();
    const logs: string[] = [];

    const dockerRes = await runInitWizard({
      mode: "docker",
      userId: "alice",
      yes: true,
      configPath: "/virtual/openclaw.json",
      readFileFn: (p) => files.get(p) ?? '{"gateway":{"port":18789}}',
      writeFileFn: (p, content) => files.set(p, content),
      healthProbeFn: async () => ({ ok: true, status: "ok" }),
      stdout: (line) => logs.push(line),
    });
    assert.equal(dockerRes.pluginConfig.baseUrl, DEFAULT_LOCAL_DOCKER_URL);
    assert.equal(dockerRes.pluginConfig.injected, false);
    assert.equal(dockerRes.pluginConfig.telemetryEnabled, false);

    const written = JSON.parse(files.get("/virtual/openclaw.json") ?? "{}") as Record<string, any>;
    assert.equal(written.gateway.port, 18789);
    assert.equal(written.plugins.slots.memory, "ebbmem");
    assert.equal(written.plugins.entries.ebbmem.enabled, true);

    const answers = ["remote", "https://ebbmem.example.com", "api-key", "ebb_super_secret"];
    const remoteRes = await runInitWizard({
      configPath: "/virtual/openclaw2.json",
      promptFn: async () => answers.shift() ?? "",
      readFileFn: () => "{}",
      writeFileFn: (p, content) => files.set(p, content),
      healthProbeFn: async () => ({ ok: true, status: "ok" }),
      stdout: (line) => logs.push(line),
    });
    assert.equal(remoteRes.pluginConfig.baseUrl, "https://ebbmem.example.com");
    assert.equal(remoteRes.pluginConfig.apiKey, "ebb_super_secret");
    assert.equal(logs.some((l) => l.includes("ebb_super_secret")), false);

    await assert.rejects(
      () =>
        runInitWizard({
          mode: "remote",
          url: "https://ebbmem.example.com?api_key=leak",
          yes: true,
          writeFileFn: () => {},
        }),
      CredentialInUrlError,
    );

    assert.equal(matchSetupPhrase("Can you help me setup ebbmem?"), true);
    assert.equal(matchSetupPhrase("What is the weather?"), false);
    assert.ok(buildSetupGuidance().includes("openclaw ebbmem init"));

    const api = new MockOpenClawApi({ tenant: "acme", userId: "alice" });
    register(api, { env: {} });
    const hookOut = await api.dispatchHook("before_agent_start", {
      userText: "Please configure ebbmem for me",
    });
    assert.equal(hookOut.length, 1);
    assert.ok(String((hookOut[0] as { prependContext?: string }).prependContext).includes("openclaw ebbmem init"));

    const cmdOut = await api.runSlashCommand("ebbmem", { args: "setup" });
    assert.ok(cmdOut.text.includes("openclaw ebbmem init"));

    const cmdStatus = await api.runSlashCommand("ebbmem", { args: "status", agentId: "main" });
    assert.ok(cmdStatus.text.includes("EbbMem status:"));
    assert.ok(cmdStatus.text.includes("telemetry=false"));

    await api.runCli(["ebbmem", "status"], { agentId: "main" });
    assert.ok(api.logs.info.some((line) => line.includes('"telemetryEnabled":false')));
  });

  it("handles Gateway reload idempotently and makes close() idempotent", async () => {
    const stub = new StubTransport();
    const api1 = new MockOpenClawApi({ tenant: "acme", userId: "alice" });
    const first = register(api1, { transport: stub, env: {} });
    assert.equal(first.reused, false);
    const second = register(api1, { transport: stub, env: {} });
    assert.equal(second.reused, true);
    assert.equal(second.runtime, first.runtime);
    assert.equal(api1.capabilities.length, 1);

    const api2 = new MockOpenClawApi({ tenant: "acme", userId: "alice" });
    const reloaded = register(api2, { transport: stub, env: {} });
    assert.equal(reloaded.reused, false);
    assert.equal(first.runtime.isClosed(), true);
    assert.equal(reloaded.runtime.isClosed(), false);

    // Idempotent close() across multiple calls
    await reloaded.runtime.close();
    await reloaded.runtime.close();
    await reloaded.runtime.close();
    assert.equal(reloaded.runtime.isClosed(), true);
  });
});
