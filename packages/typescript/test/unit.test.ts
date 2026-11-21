import { createHash } from "node:crypto";
import * as http from "node:http";
import { describe, it } from "node:test";
import * as assert from "node:assert/strict";

import {
  ApiValidationError,
  CredentialConflictError,
  CredentialInUrlError,
  DEFAULT_REINFORCE_DELTA,
  EbbmemApiError,
  EbbmemIntegrationCore,
  EbbmemTransport,
  ExposureBookkeeper,
  FetchTransport,
  IdempotencyConflictError,
  NamespaceError,
  RateLimitError,
  ReinforcePolicy,
  ReinforcementTracker,
  RequestTimeoutError,
  ReservedMetadataError,
  RetryPolicy,
  ScopingResolver,
  SdkCapabilities,
  SdkClientLike,
  SdkTransportAdapter,
  ServerError,
  TELEMETRY_ENABLED_DEFAULT,
  TransportError,
  UnsupportedCapabilityError,
  ValidationError,
  checkNamespaceConflict,
  computeBackoffMs,
  extractCitedIds,
  frameRecalledMemories,
  isIdempotentRequest,
  makeIdempotencyKey,
  normalizeTagsAndSession,
  parseRetryAfterMs,
  redactPayload,
  redactSecrets,
  validateAuthCredentials,
  validateBaseUrl,
  validateDelta,
  validateIdempotencyKey,
  validateMetadata,
  validateNamespace,
  validateRequestTarget,
} from "../src";

describe("scoping", () => {
  it("validates namespaces and resolves per-user scope without explosion", () => {
    assert.equal(validateNamespace("acme:u:alice"), "acme:u:alice");
    for (const bad of ["", "a".repeat(257), "has space", "acme*all", "ns?q=1"]) {
      assert.throws(() => validateNamespace(bad), NamespaceError);
    }

    const resolver = new ScopingResolver({
      tenant: "acme",
      userId: "alice",
      agentId: "planner-1",
    });
    const r1 = resolver.resolve({ threadId: "t-101", tupleSegments: ["org", "support"] });
    const r2 = resolver.resolve({ threadId: "t-202", tupleSegments: ["org", "billing"] });

    assert.equal(r1.namespace, "acme:u:alice");
    assert.equal(r2.namespace, "acme:u:alice");
    assert.equal(r1.soulScope, "agent");
    assert.equal(r1.soulId, "planner-1");
    assert.equal(r1.soulPath, "/api/v1/soul/agent/planner-1");
    assert.equal(r1.sessionId, "t-101");
    assert.deepEqual(r1.tags, ["org", "support"]);
    assert.deepEqual(r2.tags, ["org", "billing"]);
  });

  it("detects header/body namespace conflicts and normalizes tags/session/metadata", () => {
    assert.equal(checkNamespaceConflict("acme:u:alice", "acme:u:alice"), "acme:u:alice");
    assert.throws(() => checkNamespaceConflict("acme:u:alice", "other:u:bob"), NamespaceError);
    assert.throws(() => checkNamespaceConflict("   ", "acme:u:alice"), NamespaceError);
    assert.throws(() => checkNamespaceConflict("", undefined), NamespaceError);
    assert.throws(() => checkNamespaceConflict(undefined, "   "), NamespaceError);

    const norm = normalizeTagsAndSession(
      [" #Work ", "session:sess-42", "Work", "#finance", "session:sess-99", ""],
      { tupleSegments: [" #team-alpha ", "finance"], domain: " #Engineering " },
    );
    assert.deepEqual(norm.tags, ["Work", "finance", "team-alpha"]);
    assert.equal(norm.domain, "Engineering");
    assert.equal(norm.sessionId, "sess-42");

    assert.deepEqual(validateMetadata({ source: "chat" }), { source: "chat" });
    assert.deepEqual(validateMetadata({ sub: { "ebb.nested": 1 } }), { sub: { "ebb.nested": 1 } });
    for (const badKey of ["ebb.internal", "EBB.internal", " ebb.internal"]) {
      assert.throws(() => validateMetadata({ [badKey]: 1 }), ReservedMetadataError);
    }
  });
});

describe("security & framing", () => {
  it("rejects URL credentials without echoing secret tokens", () => {
    const cases: Array<[string, string[]]> = [
      ["https://example.invalid?TOKEN=UPPER_SECRET_99", ["UPPER_SECRET_99"]],
      ["https://example.invalid?api_key=ebb_secret_123", ["ebb_secret_123"]],
      ["http://alice_sec:pass_sec@example.invalid", ["alice_sec", "pass_sec"]],
      ["ftp://alice_sec:pass_sec@example.invalid", ["alice_sec", "pass_sec"]],
      ["alice_sec:pass_sec@example.invalid", ["alice_sec", "pass_sec"]],
      ["example.invalid?token=leaked_missing_scheme", ["leaked_missing_scheme"]],
    ];
    for (const [badUrl, secrets] of cases) {
      assert.throws(() => validateBaseUrl(badUrl), (err: unknown) => {
        assert.ok(err instanceof Error);
        for (const s of secrets) {
          assert.ok(!err.message.includes(s), `Secret ${s} leaked in error message`);
        }
        return true;
      });
    }
    assert.equal(validateBaseUrl("https://api.example.invalid/"), "https://api.example.invalid");

    assert.throws(
      () => validateRequestTarget("/api/v1/memory/status?Access_Token=sec1"),
      CredentialInUrlError,
    );
    assert.throws(
      () => validateRequestTarget("/api/v1/memory/status", { API_KEY: "sec2" }),
      CredentialInUrlError,
    );
    assert.throws(
      () => validateRequestTarget("//u_tgt:p_tgt@evil.invalid/x"),
      CredentialInUrlError,
    );
    assert.throws(() => validateBaseUrl("//u_tgt:p_tgt@evil.invalid"), CredentialInUrlError);
    validateRequestTarget("/users/@alice");
    validateRequestTarget("///u:p@host");
  });

  it("rejects dual auth credentials and defaults telemetry off", () => {
    assert.equal(TELEMETRY_ENABLED_DEFAULT, false);
    assert.deepEqual(validateAuthCredentials({ bearerToken: "tok" }), {
      Authorization: "Bearer tok",
    });
    assert.deepEqual(validateAuthCredentials({ apiKey: "ebb_k" }), {
      "X-API-Key": "ebb_k",
    });
    assert.throws(
      () => validateAuthCredentials({ bearerToken: "tok", apiKey: "ebb_k" }),
      CredentialConflictError,
    );
  });

  it("redacts secrets in text and cyclic/deep payloads", () => {
    const jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig_xyz";
    const pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAK\n-----END RSA PRIVATE KEY-----";
    const ghp = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8";
    const raw = [
      "Authorization: Bearer super_secret_bearer_tok",
      "X-API-Key: ebb_test_alice_key",
      "https://example.invalid/x?token=url_tok_val",
      jwt,
      pem,
      "sk-proj1234567890abcdef",
      ghp,
      'password="SuperSecret99" api_key=\'my_secret_val\' {"client_secret": "cs_val"}',
    ].join("\n");

    const redacted = redactSecrets(raw);
    for (const leaked of [
      "super_secret_bearer_tok",
      "ebb_test_alice_key",
      "url_tok_val",
      jwt,
      "MIIEowIBAAK",
      "sk-proj1234567890abcdef",
      ghp,
      "SuperSecret99",
      "my_secret_val",
      "cs_val",
    ]) {
      assert.ok(!redacted.includes(leaked), `Secret ${leaked} was not redacted`);
    }

    const cyclic: Record<string, unknown> = { token: "secret_tok", safe: "Bearer inline_tok" };
    cyclic.self = cyclic;
    const redactedObj = redactPayload(cyclic) as Record<string, unknown>;
    assert.equal(redactedObj.token, "[REDACTED]");
    assert.equal(redactedObj.safe, "Bearer [REDACTED]");
    assert.equal(redactedObj.self, "[CIRCULAR/MAX_DEPTH]");

    const expanded = redactPayload({
      refresh_token: "rt_1",
      bearer_token: "bt_2",
      id_token: "id_3",
      auth_token: "at_4",
      secret_key: "sk_5",
      api_secret: "as_6",
      inline: 'secret_key="raw_secret_val_123" api_secret=raw_as_7',
    }) as Record<string, unknown>;
    for (const k of [
      "refresh_token",
      "bearer_token",
      "id_token",
      "auth_token",
      "secret_key",
      "api_secret",
    ]) {
      assert.equal(expanded[k], "[REDACTED]");
    }
    assert.ok(!String(expanded.inline).includes("raw_secret_val_123"));
    assert.ok(!String(expanded.inline).includes("raw_as_7"));

    const apiErr = new EbbmemApiError("err", {
      body: { detail: "Bearer secret_1", refresh_token: "rt_1" },
    });
    assert.deepEqual(apiErr.body, { detail: "Bearer [REDACTED]", refresh_token: "[REDACTED]" });
  });

  it("frames recalled memories and escapes attributes and closing tags", () => {
    const framed = frameRecalledMemories([
      {
        memory_id: 'm1" onclick="evil"><injected>',
        tier: 'semantic" data-x="1"></memory>',
        content: "Hi </Ebbmem_Untrusted_Memory_Context > </memory> < / MEMORY\n> end",
      },
      { content: "Fallback" },
      { id: "0", content: 0 },
      { id: "1", content: false },
      { id: "2", content: "hi </ebbmem_untrusted_memory_context" },
    ]);
    assert.ok(framed.includes('id="m1&quot; onclick=&quot;evil&quot;&gt;&lt;injected&gt;"'));
    assert.ok(framed.includes('tier="semantic&quot; data-x=&quot;1&quot;&gt;&lt;/memory&gt;"'));
    assert.ok(framed.includes('id="item-2" tier="semantic">Fallback</memory>'));
    assert.ok(framed.includes('<memory id="0" tier="semantic">0</memory>'));
    assert.ok(framed.includes('<memory id="1" tier="semantic">false</memory>'));
    assert.ok(framed.includes('<memory id="2" tier="semantic">hi [escaped_tag]</memory>'));
    assert.equal(framed.toLowerCase().split("</ebbmem_untrusted_memory_context>").length - 1, 1);
    assert.equal(framed.toLowerCase().split("</memory>").length - 1, 5);
    assert.ok(framed.includes("[escaped_tag]"));
  });
});

describe("retry & fail-open", () => {
  it("generates deterministic VCHAR keys and enforces idempotent retry rules", () => {
    const k1 = makeIdempotencyKey(["acme:u:alice", "remember", "hello"]);
    const k2 = makeIdempotencyKey(["acme:u:alice", "remember", "hello"]);
    assert.equal(k1, k2);
    assert.ok(k1.startsWith("ebb_"));
    assert.equal(k1.length, 68);
    assert.equal(validateIdempotencyKey(k1), k1);
    assert.throws(() => validateIdempotencyKey("bad key with space"), ValidationError);

    assert.equal(isIdempotentRequest("GET", "/api/v1/memory/status"), true);
    assert.equal(isIdempotentRequest("POST", "/api/v1/memory/recall"), true);
    assert.equal(isIdempotentRequest("DELETE", "/api/v1/memory/m1"), true);
    assert.equal(isIdempotentRequest("DELETE", "/api/v1/memory/m1/purge"), false);
    assert.equal(
      isIdempotentRequest("POST", "/api/v1/memory/remember", { jsonBody: { content: "x" } }),
      false,
    );
    assert.equal(
      isIdempotentRequest("POST", "/api/v1/memory/reinforce", { jsonBody: { memory_id: "m1" } }),
      false,
    );
    assert.equal(
      isIdempotentRequest("POST", "/api/v1/memory/remember", {
        headers: { "idempotency-key": "k1" },
        jsonBody: { content: "x" },
      }),
      true,
    );

    assert.equal(computeBackoffMs(0, { jitterFn: () => 0 }), 200);
    assert.equal(computeBackoffMs(10, { jitterFn: () => 0 }), 8_000);
    assert.equal(computeBackoffMs(1, { retryAfterMs: 1500 }), 1500);
    assert.equal(parseRetryAfterMs({ "retry-after": "2.5" }), 2500);
    assert.throws(() => new RetryPolicy({ timeoutMs: 0 }), ValidationError);

    // Cross-language canonical JSON parity (Unicode, codepoint sort, undefined, toJSON)
    const kUnicode = makeIdempotencyKey(["unicode", "café", "日本語", "😀"]);
    const expectedUnicode =
      "ebb_" + createHash("sha256").update('["unicode","café","日本語","😀"]', "utf8").digest("hex");
    assert.equal(kUnicode, expectedUnicode);

    const kDict = makeIdempotencyKey([{ a: 1, B: 2, _c: 3 }]);
    const expectedDict =
      "ebb_" + createHash("sha256").update('[{"B":2,"_c":3,"a":1}]', "utf8").digest("hex");
    assert.equal(kDict, expectedDict);

    assert.equal(makeIdempotencyKey([{ a: 1, b: undefined }]), makeIdempotencyKey([{ a: 1 }]));
    assert.equal(makeIdempotencyKey([[1, undefined, 2]]), makeIdempotencyKey([[1, null, 2]]));
    const dt = new Date("2026-01-01T00:00:00.000Z");
    assert.equal(makeIdempotencyKey([dt]), makeIdempotencyKey([dt.toISOString()]));
  });

  it("degrades recall on transient errors when failOpen=true and rethrows validation errors", async () => {
    const makeCore = (err: Error, failOpen: boolean, logs: string[]) => {
      const transport: EbbmemTransport = {
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
        recall: async () => {
          throw err;
        },
        reinforce: async () => ({}),
        get: async () => ({}),
        forget: async () => ({}),
        purge: async () => ({}),
        status: async () => ({}),
        health: async () => ({}),
      };
      return new EbbmemIntegrationCore({
        transport,
        scope: new ScopingResolver({ appNamespace: "acme:u:alice" }),
        failOpen,
        logSink: logs,
      });
    };

    for (const err of [
      new RequestTimeoutError("timeout Bearer leaked_tok_1"),
      new ServerError("503 ebb_leaked_key", { status: 503 }),
      new RateLimitError("429 limited", { status: 429 }),
    ]) {
      const logs: string[] = [];
      const core = makeCore(err, true, logs);
      const res = await core.recall("hello");
      assert.deepEqual(res, { query: "hello", count: 0, results: [], degraded: true });
      assert.equal(logs.length, 1);
      assert.ok(!logs[0].includes("leaked_tok_1"));
      assert.ok(!logs[0].includes("ebb_leaked_key"));
    }

    await assert.rejects(
      () => makeCore(new ServerError("down", { status: 503 }), false, []).recall("hello"),
      ServerError,
    );
    await assert.rejects(
      () => makeCore(new ApiValidationError("bad", { status: 422 }), true, []).recall("hello"),
      ApiValidationError,
    );
  });
});

describe("reinforcement & exposure bookkeeper", () => {
  it("validates delta, enforces injected=false default, and tracks exposures", () => {
    assert.equal(DEFAULT_REINFORCE_DELTA, 0.1);
    assert.equal(validateDelta(0.25), 0.25);
    assert.equal(validateDelta(2.5, { clamp: true }), 1.0);
    for (const bad of [true, false, Number.NaN, Number.POSITIVE_INFINITY, "bad", 1.5, -1.5]) {
      assert.throws(() => validateDelta(bad), ValidationError);
    }

    const policy = new ReinforcePolicy();
    assert.equal(policy.injected, false);
    assert.deepEqual(policy.selectIdsToReinforce(["m1", "m2"], ["m2", "m_other", "m2"]), ["m2"]);
    const optedIn = new ReinforcePolicy({ injected: true });
    assert.deepEqual(optedIn.selectIdsToReinforce(["m1", "m2"], ["m2"]), ["m2", "m1"]);
    assert.deepEqual(extractCitedIds("See [m2] and m1", ["m1", "m2", "m3"]), ["m2", "m1"]);

    const bookkeeper = new ExposureBookkeeper(2);
    bookkeeper.recordRecall(
      "acme:u:alice",
      { recall_id: "r1", results: [{ memory_id: "m1", score: 0.9 }] },
      { turnId: "t1" },
    );
    assert.equal(bookkeeper.getByRecallId("r1", "other:u:bob"), undefined);
    bookkeeper.recordRecall("acme:u:alice", { results: [{ memory_id: "m2" }] }, { turnId: "t2" });
    bookkeeper.recordRecall("acme:u:alice", { results: [{ memory_id: "m3" }] }, { turnId: "t3" });
    assert.equal(bookkeeper.getForTurn("acme:u:alice", "t1"), undefined);

    const tracker = new ReinforcementTracker();
    tracker.recordExposure(
      "acme:u:alice",
      { recall_id: "rec-9", results: [{ memory_id: "m1" }, { memory_id: "m2" }] },
      { turnId: "t-cite" },
    );
    assert.equal(
      tracker.planReinforcement("acme:u:alice", { responseText: "no match", turnId: "t-cite" }),
      undefined,
    );
    const plan = tracker.planReinforcement("acme:u:alice", {
      responseText: "Using m2 here.",
      turnId: "t-cite",
    });
    assert.ok(plan);
    assert.equal(plan.memoryId, "m2");
    assert.equal(plan.recallId, "rec-9");
    assert.equal(plan.delta, 0.1);

    // Explicit turnId / recallId miss must NOT fall back to getLatest
    const toolTracker = new ReinforcementTracker(new ReinforcePolicy({ mode: "tool" }));
    toolTracker.recordExposure(
      "acme:u:alice",
      { recall_id: "rec-1", results: [{ memory_id: "m1" }] },
      { turnId: "turn-1" },
    );
    assert.equal(
      toolTracker.planReinforcement("acme:u:alice", { usedIds: ["m1"], turnId: "turn-missing" }),
      undefined,
    );
    assert.equal(
      toolTracker.planReinforcement("acme:u:alice", { usedIds: ["m1"], recallId: "rec-missing" }),
      undefined,
    );

    // Composite (namespace, recallId) isolation & true LRU eviction
    const lru = new ExposureBookkeeper(2);
    const recAlice = lru.recordRecall(
      "acme:u:alice",
      { recall_id: "shared-rec", results: [{ memory_id: "m-a" }] },
      { turnId: "t1" },
    );
    const recBob = lru.recordRecall(
      "other:u:bob",
      { recall_id: "shared-rec", results: [{ memory_id: "m-b" }] },
      { turnId: "t1" },
    );
    assert.equal(lru.getByRecallId("shared-rec", "acme:u:alice"), recAlice);
    assert.equal(lru.getByRecallId("shared-rec", "other:u:bob"), recBob);
    assert.equal(lru.getForTurn("acme:u:alice", "t1"), recAlice);
    lru.recordRecall(
      "acme:u:carol",
      { recall_id: "rec-3", results: [{ memory_id: "m-c" }] },
      { turnId: "t3" },
    );
    assert.equal(lru.getByRecallId("shared-rec", "acme:u:alice"), recAlice);
    assert.equal(lru.getByRecallId("shared-rec", "other:u:bob"), undefined);
  });
});

describe("SdkTransportAdapter v0.2.0 bridges and P0-09 gaps", () => {
  it("enforces URL/credential safety, bridges raw.memory, and flags unsupported P0-09 APIs", async () => {
    assert.throws(
      () =>
        new SdkTransportAdapter(
          {
            baseUrl: "https://example.invalid?token=secret",
            memory: {
              remember: async () => ({}),
              recall: async () => ({}),
              get: async () => ({}),
              forget: async () => ({}),
              reinforce: async () => ({}),
              status: async () => ({}),
            },
          },
        ),
      CredentialInUrlError,
    );

    const calls: Array<[string, unknown]> = [];
    const fakeSdk: SdkClientLike = {
      baseUrl: "https://example.invalid",
      apiKey: "ebb_k1",
      memory: {
        remember: async () => ({}),
        recall: async () => ({}),
        get: async () => ({}),
        forget: async () => ({}),
        reinforce: async () => ({}),
        status: async () => ({}),
      },
      raw: {
        memory: {
          remember: async (req) => {
            calls.push(["remember", req]);
            return { id: "m-1", ...req.rememberRequestModel };
          },
          recall: async (req) => {
            calls.push(["recall", req]);
            return { query: req.recallRequestModel.query, count: 1, recall_id: "rec-1", results: [] };
          },
          reinforce: async (req) => {
            calls.push(["reinforce", req]);
            return { status: "reinforced", outcome: "counted", ...req.reinforceRequestModel };
          },
          forgetMemory: async (req) => {
            calls.push(["forget", req]);
            return { status: req.purge ? "purged" : "forgotten" };
          },
        },
      },
      health: async () => ({ status: "ok" }),
    };

    const adapter = new SdkTransportAdapter(fakeSdk);
    assert.equal(adapter.capabilities.exactKeyGet, false);
    assert.equal(adapter.capabilities.namespacePrefixList, false);

    const rem = await adapter.remember({
      content: "hi",
      namespace: "acme:u:alice",
      tier: "episodic",
      sessionId: "sess-1",
      metadata: { k: "v" },
    });
    assert.equal(rem.tier, "episodic");
    assert.equal(rem.session_id, "sess-1");

    const reinf = await adapter.reinforce({
      memoryId: "m-1",
      usedIds: ["m-2"],
      recallId: "rec-1",
      namespace: "acme:u:alice",
    });
    assert.equal(reinf.delta, 0.1);
    assert.deepEqual(reinf.used_ids, ["m-2"]);

    await assert.rejects(
      () => adapter.recall({ query: "hi", tier: "semantic" }),
      UnsupportedCapabilityError,
    );
    await assert.rejects(() => adapter.getByKey("k1"), UnsupportedCapabilityError);
    await assert.rejects(() => adapter.deleteByKey("k1"), UnsupportedCapabilityError);
    await assert.rejects(() => adapter.listNamespaces("acme:"), UnsupportedCapabilityError);
  });

  it("supports @ebbmem/client v0.2.0 config, string invoke idempotencyKey, initOverrides, ConnectionError, and facade guards", async () => {
    assert.throws(
      () =>
        new SdkTransportAdapter({
          config: { baseUrl: "https://example.invalid?token=sec" },
          memory: {
            remember: async () => ({}),
            recall: async () => ({}),
            get: async () => ({}),
            forget: async () => ({}),
            reinforce: async () => ({}),
            status: async () => ({}),
          },
        }),
      CredentialInUrlError,
    );

    const invokeKeys: Array<string | undefined> = [];
    const seenInitHeaders: Array<Record<string, string>> = [];
    const v020Sdk: SdkClientLike = {
      config: { baseUrl: "https://example.invalid", apiKey: "ebb_k1" },
      async invoke(_name, op, idempotencyKey) {
        invokeKeys.push(idempotencyKey);
        return op();
      },
      memory: {
        remember: async () => ({}),
        recall: async () => ({}),
        get: async () => ({}),
        forget: async () => ({}),
        reinforce: async () => ({}),
        status: async () => ({}),
      },
      raw: {
        memory: {
          remember: async (req, initOverrides) => {
            if (typeof initOverrides === "function") {
              const init = await initOverrides({
                init: { headers: { Authorization: "Bearer kept", "X-Ebb-Namespace": "acme:u:alice" } },
                context: {},
              });
              const hObj: Record<string, string> = {};
              new Headers(init.headers).forEach((v, k) => {
                hObj[k] = v;
              });
              seenInitHeaders.push(hObj);
            }
            return { id: "m-2", idempotencyKey: req.idempotencyKey };
          },
          recall: async () => {
            const err = new Error("socket hangup");
            err.name = "ConnectionError";
            throw err;
          },
        },
      },
    };

    const adapter = new SdkTransportAdapter(v020Sdk);
    const rem = await adapter.remember({
      content: "hi",
      namespace: "acme:u:alice",
      idempotencyKey: "ebb_idem_v020",
      headers: { "X-Custom": "yes" },
    });
    assert.equal(rem.idempotencyKey, "ebb_idem_v020");
    assert.deepEqual(invokeKeys, ["ebb_idem_v020"]);
    assert.equal(seenInitHeaders[0].authorization, "Bearer kept");
    assert.equal(seenInitHeaders[0]["x-custom"], "yes");

    const core = new EbbmemIntegrationCore({
      transport: adapter,
      scope: new ScopingResolver({ appNamespace: "acme:u:alice" }),
      failOpen: true,
    });
    const degraded = await core.recall("q");
    assert.deepEqual(degraded, { query: "q", count: 0, results: [], degraded: true });

    const facadeOnly = new SdkTransportAdapter({
      config: { baseUrl: "https://example.invalid", apiKey: "ebb_k1" },
      memory: {
        remember: async (c) => ({ id: "f1", content: c }),
        recall: async (q) => ({ query: q, count: 0, results: [] }),
        get: async (id) => ({ id }),
        forget: async (id) => ({ id }),
        reinforce: async (id) => ({ id }),
        status: async () => ({ ok: true }),
      },
    });
    assert.equal((await facadeOnly.remember({ content: "ok", tier: "semantic" })).id, "f1");
    await assert.rejects(
      () => facadeOnly.remember({ content: "bad", namespace: "acme:u:alice" }),
      UnsupportedCapabilityError,
    );
    await assert.rejects(
      () => facadeOnly.recall({ query: "bad", profile: "core" }),
      UnsupportedCapabilityError,
    );
    await assert.rejects(
      () => facadeOnly.get("m1", { namespace: "acme:u:alice" }),
      UnsupportedCapabilityError,
    );
  });

  it("preserves single case-insensitive x-ebb-namespace and idempotency-key headers without merging", async () => {
    const receivedRawHeaders: string[][] = [];
    const server = http.createServer((req, res) => {
      receivedRawHeaders.push([...req.rawHeaders]);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ id: "m-hdr" }));
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    try {
      const addr = server.address();
      assert.ok(addr && typeof addr === "object");
      const transport = new FetchTransport({
        baseUrl: `http://127.0.0.1:${addr.port}`,
        apiKey: "ebb_test_key",
      });
      await transport.remember({
        content: "hello",
        namespace: "acme:u:alice",
        idempotencyKey: "k-match",
        headers: { "x-ebb-namespace": "acme:u:alice", "idempotency-key": "k-match" },
      });
      const raw = receivedRawHeaders[0];
      const nsValues: string[] = [];
      const idemValues: string[] = [];
      for (let i = 0; i < raw.length; i += 2) {
        if (raw[i].toLowerCase() === "x-ebb-namespace") nsValues.push(raw[i + 1]);
        if (raw[i].toLowerCase() === "idempotency-key") idemValues.push(raw[i + 1]);
      }
      assert.deepEqual(nsValues, ["acme:u:alice"]);
      assert.deepEqual(idemValues, ["k-match"]);
    } finally {
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });

  it("preserves this binding on all class-based raw.memory prototype methods", async () => {
    class ClassBasedRawMemoryApi {
      readonly prefix = "cls";
      readonly configuration = { basePath: "https://example.invalid" };
      readonly calls: string[] = [];

      private rememberRaw(req: { rememberRequestModel: Record<string, unknown> }) {
        assert.ok(this instanceof ClassBasedRawMemoryApi);
        this.calls.push(`remember:${this.prefix}:${this.configuration.basePath}`);
        return { id: "m-cls", tier: req.rememberRequestModel.tier };
      }

      async remember(
        req: { rememberRequestModel: Record<string, unknown>; xEbbNamespace?: string; idempotencyKey?: string },
        _initOverrides?: unknown,
      ) {
        return this.rememberRaw(req);
      }

      async recall(
        req: { recallRequestModel: Record<string, unknown>; xEbbNamespace?: string },
        _initOverrides?: unknown,
      ) {
        assert.ok(this instanceof ClassBasedRawMemoryApi);
        this.calls.push(`recall:${this.prefix}`);
        return { query: req.recallRequestModel.query, count: 0, results: [] };
      }

      async reinforce(
        req: { reinforceRequestModel: Record<string, unknown>; xEbbNamespace?: string },
        _initOverrides?: unknown,
      ) {
        assert.ok(this instanceof ClassBasedRawMemoryApi);
        this.calls.push(`reinforce:${this.prefix}`);
        return { outcome: "counted", delta: req.reinforceRequestModel.delta };
      }

      async getMemory(req: { memoryId: string; xEbbNamespace?: string }, _initOverrides?: unknown) {
        assert.ok(this instanceof ClassBasedRawMemoryApi);
        this.calls.push(`getMemory:${this.prefix}`);
        return { id: req.memoryId };
      }

      async forgetMemory(
        req: { memoryId: string; purge?: boolean; xEbbNamespace?: string },
        _initOverrides?: unknown,
      ) {
        assert.ok(this instanceof ClassBasedRawMemoryApi);
        this.calls.push(`forgetMemory:${Boolean(req.purge)}:${this.prefix}`);
        return { id: req.memoryId, status: req.purge ? "purged" : "forgotten" };
      }

      async purgeMemory(
        req: { memoryId: string; xEbbNamespace?: string },
        _initOverrides?: unknown,
      ) {
        assert.ok(this instanceof ClassBasedRawMemoryApi);
        this.calls.push(`purgeMemory:${this.prefix}`);
        return { id: req.memoryId, status: "purged", irreversible: true };
      }

      async status(_req: { xEbbNamespace?: string }, _initOverrides?: unknown) {
        assert.ok(this instanceof ClassBasedRawMemoryApi);
        this.calls.push(`status:${this.prefix}`);
        return { ok: true };
      }

      async getByKey(req: { key: string; xEbbNamespace?: string }) {
        assert.ok(this instanceof ClassBasedRawMemoryApi);
        this.calls.push(`getByKey:${this.prefix}`);
        return { key: req.key };
      }

      async deleteByKey(req: { key: string; xEbbNamespace?: string }) {
        assert.ok(this instanceof ClassBasedRawMemoryApi);
        this.calls.push(`deleteByKey:${this.prefix}`);
        return { key: req.key, deleted: true };
      }

      async listNamespaces(req: { prefix?: string }) {
        assert.ok(this instanceof ClassBasedRawMemoryApi);
        this.calls.push(`listNamespaces:${this.prefix}`);
        return [`${req.prefix ?? ""}u:alice`];
      }
    }

    const rawMemory = new ClassBasedRawMemoryApi();
    const invokeKeys: Array<string | undefined> = [];
    const classSdk: SdkClientLike = {
      config: { baseUrl: "https://example.invalid", apiKey: "ebb_k1" },
      async invoke(_name, op, idempotencyKey) {
        invokeKeys.push(idempotencyKey);
        return op();
      },
      memory: {
        remember: async () => {
          throw new Error("should use raw.memory");
        },
        recall: async () => {
          throw new Error("should use raw.memory");
        },
        get: async () => {
          throw new Error("should use raw.memory");
        },
        forget: async () => {
          throw new Error("should use raw.memory");
        },
        reinforce: async () => {
          throw new Error("should use raw.memory");
        },
        status: async () => {
          throw new Error("should use raw.memory");
        },
      },
      raw: { memory: rawMemory },
    };

    const adapter = new SdkTransportAdapter(classSdk);
    assert.equal(
      (
        await adapter.remember({
          content: "cls-mem",
          namespace: "acme:u:alice",
          headers: { "idempotency-key": "ebb_hdr_idem_1" },
        })
      ).id,
      "m-cls",
    );
    assert.equal(invokeKeys[0], "ebb_hdr_idem_1");
    assert.equal((await adapter.recall({ query: "q", namespace: "acme:u:alice" })).count, 0);
    assert.equal((await adapter.reinforce({ memoryId: "m-cls", namespace: "acme:u:alice" })).outcome, "counted");
    assert.equal((await adapter.get("m-cls", { namespace: "acme:u:alice" })).id, "m-cls");
    assert.equal((await adapter.forget("m-cls", { namespace: "acme:u:alice" })).status, "forgotten");
    const purged = await adapter.purge("m-cls", { namespace: "acme:u:alice" });
    assert.equal(purged.status, "purged");
    assert.equal(purged.irreversible, true);
    assert.equal((await adapter.status({ namespace: "acme:u:alice" })).ok, true);
    assert.equal((await adapter.getByKey("ext-1", "acme:u:alice")).key, "ext-1");
    assert.equal((await adapter.deleteByKey("ext-1", "acme:u:alice")).deleted, true);
    assert.deepEqual(await adapter.listNamespaces("acme:"), ["acme:u:alice"]);

    assert.deepEqual(rawMemory.calls, [
      "remember:cls:https://example.invalid",
      "recall:cls",
      "reinforce:cls",
      "getMemory:cls",
      "forgetMemory:false:cls",
      "purgeMemory:cls",
      "status:cls",
      "getByKey:cls",
      "deleteByKey:cls",
      "listNamespaces:cls",
    ]);

    // Fallback to forgetMemory(..., purge: true) when purgeMemory is absent
    const fallbackCalls: Array<Record<string, unknown>> = [];
    const fallbackAdapter = new SdkTransportAdapter({
      config: { baseUrl: "https://example.invalid", apiKey: "ebb_k1" },
      memory: {
        remember: async () => ({}),
        recall: async () => ({}),
        get: async () => ({}),
        forget: async () => ({}),
        reinforce: async () => ({}),
        status: async () => ({}),
      },
      raw: {
        memory: {
          forgetMemory: async (req) => {
            fallbackCalls.push(req);
            return { id: req.memoryId, status: req.purge ? "purged" : "forgotten" };
          },
        },
      },
    });
    assert.equal((await fallbackAdapter.purge("m-fb", { namespace: "acme:u:alice" })).status, "purged");
    assert.deepEqual(fallbackCalls, [
      { memoryId: "m-fb", purge: true, xEbbNamespace: "acme:u:alice" },
    ]);
  });

  it("maps 400 EBB-100-023 to ApiValidationError and EBB-100-021/024/029 to IdempotencyConflictError", async () => {
    const statusServer = http.createServer((req, res) => {
      const reqPath = (req.url ?? "/").split("?")[0];
      if (reqPath.endsWith("/m-023")) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ code: "EBB-100-023", detail: "reserved_metadata_prefix" }));
      } else if (reqPath.endsWith("/m-021")) {
        res.writeHead(400, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ code: "EBB-100-021", detail: "invalid_idempotency_key" }));
      } else if (reqPath.endsWith("/m-024")) {
        res.writeHead(422, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ code: "EBB-100-024", detail: "idempotency_payload_mismatch" }));
      } else {
        res.writeHead(422, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ code: "EBB-100-029", detail: "idempotency_conflict" }));
      }
    });
    await new Promise<void>((resolve) => statusServer.listen(0, "127.0.0.1", resolve));
    try {
      const sAddr = statusServer.address();
      assert.ok(sAddr && typeof sAddr === "object");
      const t = new FetchTransport({
        baseUrl: `http://127.0.0.1:${sAddr.port}`,
        apiKey: "ebb_test_key",
      });
      await assert.rejects(
        () => t.get("m-023"),
        (err: unknown) => err instanceof ApiValidationError && err.status === 400 && err.code === "EBB-100-023",
      );
      await assert.rejects(
        () => t.get("m-021"),
        (err: unknown) => err instanceof IdempotencyConflictError && err.status === 400 && err.code === "EBB-100-021",
      );
      await assert.rejects(
        () => t.get("m-024"),
        (err: unknown) => err instanceof IdempotencyConflictError && err.status === 422 && err.code === "EBB-100-024",
      );
      await assert.rejects(
        () => t.get("m-029"),
        (err: unknown) => err instanceof IdempotencyConflictError && err.status === 422 && err.code === "EBB-100-029",
      );
    } finally {
      await new Promise<void>((resolve) => statusServer.close(() => resolve()));
    }

    const sdkAdapter = new SdkTransportAdapter({
      config: { baseUrl: "https://example.invalid", apiKey: "ebb_k1" },
      memory: {
        remember: async () => ({}),
        recall: async () => ({}),
        get: async () => ({}),
        forget: async () => ({}),
        reinforce: async () => ({}),
        status: async () => ({}),
      },
      raw: {
        memory: {
          getMemory: async ({ memoryId }) => {
            const err = new Error(`sdk_${memoryId}`) as Error & { status?: number; code?: string };
            if (memoryId === "m-023") {
              err.status = 400;
              err.code = "EBB-100-023";
            } else if (memoryId === "m-021") {
              err.status = 400;
              err.code = "EBB-100-021";
            } else if (memoryId === "m-024") {
              err.status = 422;
              err.code = "EBB-100-024";
            } else {
              err.status = 422;
              err.code = "EBB-100-029";
            }
            throw err;
          },
        },
      },
    });
    await assert.rejects(
      () => sdkAdapter.get("m-023"),
      (err: unknown) => err instanceof ApiValidationError && err.status === 400 && err.code === "EBB-100-023",
    );
    await assert.rejects(
      () => sdkAdapter.get("m-021"),
      (err: unknown) => err instanceof IdempotencyConflictError && err.status === 400 && err.code === "EBB-100-021",
    );
    await assert.rejects(
      () => sdkAdapter.get("m-024"),
      (err: unknown) => err instanceof IdempotencyConflictError && err.status === 422 && err.code === "EBB-100-024",
    );
    await assert.rejects(
      () => sdkAdapter.get("m-029"),
      (err: unknown) => err instanceof IdempotencyConflictError && err.status === 422 && err.code === "EBB-100-029",
    );
  });
});
