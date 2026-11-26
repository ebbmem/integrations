import * as assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as http from "node:http";
import * as path from "node:path";
import { test } from "node:test";
import {
  assertSubset,
  cliMain,
  interpolate,
  loadGoldenSurface,
  loadScenarios,
  resolvePath,
  runScenario,
} from "../src";
import { ScenarioSpec } from "../src/types";

const EXPECTED_IC1_IDS = [
  "ic1.scoping.namespace_and_session_mapping",
  "ic1.scoping.namespace_conflict_400",
  "ic1.isolation.uniform_404",
  "ic1.roundtrip.remember_recall",
  "ic1.remember.idempotent_retry",
  "ic1.recall.no_strength_change",
  "ic1.reinforce.raises_strength_after_use",
  "ic1.reinforce.spacing_window_dedup",
  "ic1.reinforce.bounds_and_tiers",
  "ic1.reinforce.modes_injected_off_by_default",
  "ic1.reinforce.recall_id_fallback",
  "ic1.forget_vs_purge",
  "ic1.resilience.fail_open_timeout",
  "ic1.resilience.retry_idempotent_only",
  "ic1.security.no_url_or_log_credentials",
  "ic1.security.least_privilege_key_scope",
  "ic1.security.secret_redaction_and_no_telemetry",
  "ic1.security.untrusted_recall_framing",
  "ic1.closure.recall_id_valid_target",
  "ic1.mcp.golden_schemas",
  "ic1.mcp.profiles_and_aliases",
];

test("all 21 IC-1 YAML scenarios are present and pass in TS runner", async () => {
  const scenarios = loadScenarios();
  const golden = loadGoldenSurface();
  assert.equal(scenarios.length, 21);
  assert.deepEqual(
    scenarios.map((s) => s.id),
    EXPECTED_IC1_IDS,
  );

  for (const scenario of scenarios) {
    const result = await runScenario(scenario, { goldenSurface: golden });
    assert.equal(
      result.passed,
      true,
      `Scenario ${result.id} failed: ${result.error ?? "unknown error"}`,
    );
    assert.equal(result.skipped ?? false, false);
  }
});

test("interpolation preserves runtime types on full match and coerces inline", () => {
  const ctx = {
    num: 42,
    flag: true,
    arr: ["a", "b"],
    obj: { nested: [10, 20] },
  };
  assert.strictEqual(interpolate("${num}", ctx), 42);
  assert.strictEqual(interpolate("${flag}", ctx), true);
  assert.deepEqual(interpolate("${arr}", ctx), ["a", "b"]);
  assert.strictEqual(interpolate("${obj.nested.1}", ctx), 20);
  assert.strictEqual(interpolate("count=${num},ok=${flag}", ctx), "count=42,ok=true");
  assert.throws(() => resolvePath(ctx, "obj.nested.5"), /Missing list index/);
  assert.throws(() => resolvePath(ctx, "missing.key"), /Missing key/);
});

test("assertSubset enforces object subset and strict array length equality", () => {
  assertSubset({ a: 1, b: { c: 2, extra: true }, items: [{ x: 1, y: 2 }] }, { a: 1, b: { c: 2 }, items: [{ x: 1 }] });
  assert.throws(() => assertSubset({ items: [1, 2] }, { items: [1] }), /expected array len 1, got 2/);
  assert.throws(() => assertSubset({ a: 1 }, { a: 2 }), /expected 2, got 1/);
});

test("deliberate-break negative tests fail deterministically on broken assertions, schemas, and invariants", async () => {
  const golden = loadGoldenSurface();

  const brokenStatus: ScenarioSpec = {
    id: "neg.broken_status",
    steps: [
      {
        action: "rest",
        method: "GET",
        path: "/healthz",
        expect_status: 201,
      },
    ],
  };
  const resStatus = await runScenario(brokenStatus, { goldenSurface: golden });
  assert.equal(resStatus.passed, false);
  assert.match(resStatus.error ?? "", /expected status 201, got 200/);

  const brokenRecallStrength: ScenarioSpec = {
    id: "neg.broken_recall_invariant",
    steps: [
      {
        action: "rest",
        method: "POST",
        path: "/api/v1/memory/remember",
        auth: "ebb_test_alice_key",
        json: { content: "Negative test memory", tier: "semantic" },
        expect_status: 201,
        save_as: "m",
      },
      {
        action: "assert_memory_state",
        namespace: "acme:u:alice",
        memory_id: "${m.id}",
        expect: { storage_strength: 9.9 },
      },
    ],
  };
  const resStrength = await runScenario(brokenRecallStrength, { goldenSurface: golden });
  assert.equal(resStrength.passed, false);
  assert.match(resStrength.error ?? "", /storage_strength expected 9\.9, got 1/);

  const brokenRegexPartial: ScenarioSpec = {
    id: "neg.regex_fullmatch_parity",
    steps: [
      {
        action: "rest",
        method: "GET",
        path: "/healthz",
        expect_status: 200,
        expect_regex: { status: "o" },
      },
    ],
  };
  const resRegex = await runScenario(brokenRegexPartial, { goldenSurface: golden });
  assert.equal(resRegex.passed, false);
  assert.match(resRegex.error ?? "", /failed regex/);

  const emptyScenario: ScenarioSpec = {
    id: "neg.empty_steps",
    steps: [],
  };
  const resEmpty = await runScenario(emptyScenario, { goldenSurface: golden });
  assert.equal(resEmpty.passed, false);
  assert.match(resEmpty.error ?? "", /Scenario has no steps/);

  // Deliberate-break MCP outputSchema validation against a local stub HTTP server.
  const badMcpServer = http.createServer((_req, res) => {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        result: {
          structuredContent: {
            id: "mem-1",
            // Missing required memory_id, namespace, tier, storage_strength!
          },
        },
      }),
    );
  });
  await new Promise<void>((resolve) => badMcpServer.listen(0, "127.0.0.1", () => resolve()));
  try {
    const addr = badMcpServer.address();
    const port = typeof addr === "object" && addr ? addr.port : 0;
    const brokenSchema: ScenarioSpec = {
      id: "neg.broken_mcp_output_schema",
      modes: ["live"],
      steps: [
        {
          action: "mcp",
          path: "/mcp/core",
          auth: "ebb_test_alice_key",
          body: { jsonrpc: "2.0", id: 1, method: "tools/call", params: { name: "memory_remember" } },
          expect_status: 200,
          validate_output_schema_for: "memory_remember",
        },
      ],
    };
    const resSchema = await runScenario(brokenSchema, {
      goldenSurface: golden,
      mode: "live",
      baseUrl: `http://127.0.0.1:${port}`,
    });
    assert.equal(resSchema.passed, false);
    assert.match(resSchema.error ?? "", /outputSchema validation failed for memory_remember/);
  } finally {
    await new Promise<void>((resolve) => badMcpServer.close(() => resolve()));
  }
});

test("CLI --mode mock produces JSON report and --mode live rejects URL credentials without leaking secrets", async () => {
  const buildDir = path.resolve(__dirname, "..", "..", "build");
  fs.mkdirSync(buildDir, { recursive: true });
  const tmpDir = fs.mkdtempSync(path.join(buildDir, "tmp-conf-ts-"));
  const reportPath = path.join(tmpDir, "report.json");
  try {
    const codeMock = await cliMain(["--mode", "mock", "--json-report", reportPath]);
    assert.equal(codeMock, 0);
    const report = JSON.parse(fs.readFileSync(reportPath, "utf8")) as {
      mode: string;
      passed: number;
      failed: number;
      total: number;
    };
    assert.equal(report.mode, "mock");
    assert.equal(report.total, 21);
    assert.equal(report.passed, 21);
    assert.equal(report.failed, 0);

    const secret = "SECRET_LEAK_TOKEN_99";
    const badUrl = `https://example.invalid/api?api_key=${secret}`;
    const cliDistPath = path.resolve(__dirname, "..", "..", "dist", "cli.js");
    // Ensure compiled dist/cli.js is tested if built, or invoke cliMain directly.
    const codeLive = await cliMain(["--mode", "live", "--base-url", badUrl]);
    assert.equal(codeLive, 1);

    if (fs.existsSync(cliDistPath)) {
      const proc = spawnSync(
        process.execPath,
        [cliDistPath, "--mode", "live", "--base-url", badUrl],
        { encoding: "utf8" },
      );
      assert.equal(proc.status, 1);
      assert.equal(proc.stdout.includes(secret), false);
      assert.equal(proc.stderr.includes(secret), false);
    }
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});
