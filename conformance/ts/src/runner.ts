import * as fs from "node:fs";
import * as path from "node:path";
import Ajv, { ValidateFunction } from "ajv";
import * as yaml from "yaml";
import {
  CredentialInUrlError,
  EbbmemIntegrationCore,
  FetchTransport,
  HttpResponse,
  ReinforcePolicy,
  RetryPolicy,
  ScopingResolver,
  frameRecalledMemories,
  redactSecrets,
} from "ebbmem-integration-core";
import {
  BUILTIN_IDENTITY_KEYS,
  MockServerBridge,
  findConformanceDir,
} from "./bridge";
import {
  GoldenSurface,
  ScenarioResult,
  ScenarioSpec,
  ScenarioStep,
} from "./types";

const FULL_VAR_RE = /^\$\{([A-Za-z0-9_.-]+)\}$/;
const VAR_RE = /\$\{([A-Za-z0-9_.-]+)\}/g;

const MOCK_ONLY_ACTIONS = new Set([
  "advance_clock",
  "set_exposure_logging",
  "erase_namespace",
  "set_in_flight",
  "assert_memory_state",
  "assert_recall_id_valid",
  "assert_no_logged_credentials",
]);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function deepEqual(actual: unknown, expected: unknown): boolean {
  if (Object.is(actual, expected)) return true;
  if (Array.isArray(actual) && Array.isArray(expected)) {
    if (actual.length !== expected.length) return false;
    return actual.every((item, idx) => deepEqual(item, expected[idx]));
  }
  if (isPlainObject(actual) && isPlainObject(expected)) {
    const aKeys = Object.keys(actual);
    const eKeys = Object.keys(expected);
    if (aKeys.length !== eKeys.length) return false;
    return eKeys.every(
      (k) => Object.prototype.hasOwnProperty.call(actual, k) && deepEqual(actual[k], expected[k]),
    );
  }
  return false;
}

export function defaultScenariosDir(): string {
  return path.join(findConformanceDir(), "scenarios");
}

export function defaultGoldenPath(): string {
  return path.join(findConformanceDir(), "golden", "mcp_surface_v2.json");
}

export function loadGoldenSurface(goldenPath?: string): GoldenSurface {
  const target = goldenPath ?? defaultGoldenPath();
  return JSON.parse(fs.readFileSync(target, "utf8")) as GoldenSurface;
}

export function loadScenarios(scenariosDir?: string): ScenarioSpec[] {
  const dir = scenariosDir ?? defaultScenariosDir();
  const files = fs
    .readdirSync(dir)
    .filter((f) => f.endsWith(".yaml"))
    .sort();
  const scenarios: ScenarioSpec[] = [];
  for (const fileName of files) {
    const fullPath = path.join(dir, fileName);
    const parsed = yaml.parse(fs.readFileSync(fullPath, "utf8")) as unknown;
    if (!isPlainObject(parsed) || typeof parsed.id !== "string" || !parsed.id) {
      throw new Error(`Invalid scenario YAML: ${fullPath}`);
    }
    const spec = parsed as unknown as ScenarioSpec;
    spec._source_file = fileName;
    scenarios.push(spec);
  }
  return scenarios;
}

export function resolvePath(context: unknown, dotted: string): unknown {
  const parts = dotted.split(".");
  let cur: unknown = context;
  for (const part of parts) {
    if (Array.isArray(cur)) {
      if (!/^-?\d+$/.test(part)) {
        throw new Error(`Missing list index ${JSON.stringify(part)} in ${JSON.stringify(dotted)}`);
      }
      const rawIdx = Number(part);
      const idx = rawIdx < 0 ? cur.length + rawIdx : rawIdx;
      if (!Number.isInteger(idx) || idx < 0 || idx >= cur.length) {
        throw new Error(`Missing list index ${JSON.stringify(part)} in ${JSON.stringify(dotted)}`);
      }
      cur = cur[idx];
    } else if (isPlainObject(cur)) {
      if (!Object.prototype.hasOwnProperty.call(cur, part)) {
        throw new Error(`Missing key ${JSON.stringify(part)} in ${JSON.stringify(dotted)}`);
      }
      cur = cur[part];
    } else {
      throw new Error(
        `Cannot traverse ${JSON.stringify(part)} on ${cur === null ? "null" : typeof cur} in ${JSON.stringify(dotted)}`,
      );
    }
  }
  return cur;
}

export function interpolate(value: unknown, context: Record<string, unknown>): unknown {
  if (typeof value === "string") {
    const fullMatch = FULL_VAR_RE.exec(value);
    if (fullMatch) {
      return resolvePath(context, fullMatch[1]);
    }
    return value.replace(VAR_RE, (_m, key: string) => String(resolvePath(context, key)));
  }
  if (Array.isArray(value)) {
    return value.map((item) => interpolate(item, context));
  }
  if (isPlainObject(value)) {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value)) {
      out[k] = interpolate(v, context);
    }
    return out;
  }
  return value;
}

export function assertSubset(actual: unknown, expected: unknown, prefix = "$"): void {
  if (isPlainObject(expected)) {
    if (!isPlainObject(actual)) {
      throw new Error(`${prefix}: expected object, got ${actual === null ? "null" : typeof actual}`);
    }
    for (const [key, expVal] of Object.entries(expected)) {
      if (!Object.prototype.hasOwnProperty.call(actual, key)) {
        throw new Error(`${prefix}.${key}: missing from actual ${JSON.stringify(actual)}`);
      }
      assertSubset(actual[key], expVal, `${prefix}.${key}`);
    }
    return;
  }
  if (Array.isArray(expected)) {
    if (!Array.isArray(actual)) {
      throw new Error(`${prefix}: expected array, got ${actual === null ? "null" : typeof actual}`);
    }
    if (actual.length !== expected.length) {
      throw new Error(
        `${prefix}: expected array len ${expected.length}, got ${actual.length}: ${JSON.stringify(actual)}`,
      );
    }
    for (let i = 0; i < expected.length; i++) {
      assertSubset(actual[i], expected[i], `${prefix}[${i}]`);
    }
    return;
  }
  if (!Object.is(actual, expected)) {
    throw new Error(`${prefix}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

function getHeaderCI(headers: Record<string, string>, name: string): string | undefined {
  const lower = name.toLowerCase();
  for (const [k, v] of Object.entries(headers)) {
    if (k.toLowerCase() === lower) return v;
  }
  return undefined;
}

function fullMatchRegex(pattern: string, value: unknown): boolean {
  if (typeof value !== "string") return false;
  return new RegExp(`^(?:${pattern})$`).test(value);
}

function resolveAuthKey(rawAuth: unknown, isLive: boolean): string | undefined {
  if (typeof rawAuth !== "string" || !rawAuth) return undefined;
  if (!isLive) return rawAuth;
  const envMap: Record<string, string | undefined> = {
    ebb_test_alice_key: process.env.EBBMEM_LIVE_ALICE_KEY,
    ebb_test_bob_key: process.env.EBBMEM_LIVE_BOB_KEY,
    ebb_test_admin_key: process.env.EBBMEM_LIVE_ADMIN_KEY,
    ebb_test_readonly_key: process.env.EBBMEM_LIVE_READONLY_KEY,
  };
  const override = envMap[rawAuth];
  return override && override.trim() ? override.trim() : rawAuth;
}

function makeTransport(baseUrl: string, apiKey?: string): FetchTransport {
  return new FetchTransport({
    baseUrl,
    apiKey,
    retryPolicy: new RetryPolicy({ timeoutMs: 2000, maxRetries: 0 }),
  });
}

async function rawHttpRequest(
  baseUrl: string,
  step: ScenarioStep,
  headers: Record<string, string>,
): Promise<HttpResponse> {
  const method = String(step.method ?? "GET").toUpperCase();
  const reqPath = String(step.path ?? "");
  let url = `${baseUrl}${reqPath}`;
  if (step.query && Object.keys(step.query).length > 0) {
    const qs = new URLSearchParams(step.query).toString();
    url = `${url}${reqPath.includes("?") ? "&" : "?"}${qs}`;
  }
  const reqHeaders: Record<string, string> = { ...headers };
  let bodyStr: string | undefined;
  if (step.json !== undefined) {
    bodyStr = JSON.stringify(step.json);
    if (!getHeaderCI(reqHeaders, "content-type")) {
      reqHeaders["Content-Type"] = "application/json";
    }
  }
  const resp = await fetch(url, {
    method,
    headers: reqHeaders,
    body: bodyStr,
    signal: AbortSignal.timeout(2000),
  });
  const rawText = await resp.text();
  let parsed: unknown = null;
  if (rawText) {
    try {
      parsed = JSON.parse(rawText);
    } catch {
      parsed = { raw: rawText };
    }
  }
  const respHeaders: Record<string, string> = {};
  resp.headers.forEach((v, k) => {
    respHeaders[k] = v;
  });
  return { status: resp.status, headers: respHeaders, body: parsed };
}

async function runClientCheck(
  step: ScenarioStep,
  baseUrl: string,
  bridge: MockServerBridge | undefined,
  context: Record<string, unknown>,
  isLive: boolean,
): Promise<void> {
  const operation = String(step.operation ?? "");
  const aliceKey = resolveAuthKey("ebb_test_alice_key", isLive) ?? "ebb_test_alice_key";

  if (operation === "url_credential_rejection") {
    const badUrls = [
      `${baseUrl}/api/v1/memory/status?api_key=ebb_test_alice_key`,
      `${baseUrl}/api/v1/memory/status?token=ebb_test_alice_key`,
      "http://user:secret@127.0.0.1:8080",
    ];
    for (const badUrl of badUrls) {
      let rejected = false;
      try {
        new EbbmemIntegrationCore({ baseUrl: badUrl, apiKey: aliceKey });
      } catch (err) {
        if (err instanceof CredentialInUrlError) {
          rejected = true;
        } else {
          throw err;
        }
      }
      if (!rejected) {
        throw new Error(`Expected CredentialInUrlError for ${badUrl}`);
      }
    }
    return;
  }

  if (operation === "fail_open_recall") {
    const core = new EbbmemIntegrationCore({
      baseUrl,
      apiKey: aliceKey,
      scope: new ScopingResolver({ appNamespace: "acme:u:alice" }),
      retryPolicy: new RetryPolicy({ timeoutMs: 50, maxRetries: 0 }),
      failOpen: true,
    });
    const resTimeout = await core.recall("preferences", {
      timeoutMs: 50,
      failOpen: true,
      headers: { "X-Mock-Behavior": "delay_ms=250" },
    });
    if (resTimeout.degraded !== true || !Array.isArray(resTimeout.results) || resTimeout.results.length > 0) {
      throw new Error(`Expected fail-open degraded empty result, got ${JSON.stringify(resTimeout)}`);
    }
    const res503 = await core.recall("preferences", {
      timeoutMs: 500,
      failOpen: true,
      headers: { "X-Mock-Behavior": "status_503" },
    });
    if (res503.degraded !== true || !Array.isArray(res503.results) || res503.results.length > 0) {
      throw new Error(`Expected fail-open on 503, got ${JSON.stringify(res503)}`);
    }
    return;
  }

  if (operation === "idempotent_retry_policy") {
    if (!bridge) {
      throw new Error("idempotent_retry_policy requires MockServerBridge");
    }
    const transport = new FetchTransport({
      baseUrl,
      apiKey: aliceKey,
      retryPolicy: new RetryPolicy({
        timeoutMs: 2000,
        maxRetries: 2,
        backoffBaseMs: 1,
        backoffCapMs: 5,
        jitterFn: () => 0,
      }),
    });
    const beforeUnkeyed = (await bridge.getLoggedRequests()).length;
    const respUnkeyed = await transport.requestWithRetry("POST", "/api/v1/memory/remember", {
      jsonBody: { content: "unkeyed" },
      headers: { "X-Mock-Behavior": "status_503" },
    });
    const afterUnkeyed = (await bridge.getLoggedRequests()).length;
    if (respUnkeyed.status !== 503 || afterUnkeyed - beforeUnkeyed !== 1) {
      throw new Error(
        `Unkeyed POST must not retry: attempts=${afterUnkeyed - beforeUnkeyed}, status=${respUnkeyed.status}`,
      );
    }

    const beforeKeyed = (await bridge.getLoggedRequests()).length;
    const respKeyed = await transport.requestWithRetry("POST", "/api/v1/memory/remember", {
      jsonBody: { content: "keyed", idempotency_key: "retry-test-key-1" },
      headers: { "X-Mock-Behavior": "status_503" },
    });
    const afterKeyed = (await bridge.getLoggedRequests()).length;
    if (respKeyed.status !== 503 || afterKeyed - beforeKeyed !== 3) {
      throw new Error(
        `Keyed POST must retry up to 3 attempts, got ${afterKeyed - beforeKeyed}`,
      );
    }
    return;
  }

  if (operation === "redact_and_no_telemetry") {
    const rawText = "User key is ebb_test_alice_key and Bearer secret_tok_123";
    const redacted = redactSecrets(rawText);
    if (redacted.includes("ebb_test_alice_key") || redacted.includes("secret_tok_123")) {
      throw new Error(`Secret redaction failed: ${redacted}`);
    }
    const logs: string[] = [];
    const transport = new FetchTransport({
      baseUrl,
      apiKey: aliceKey,
      logSink: logs,
      retryPolicy: new RetryPolicy({ timeoutMs: 2000, maxRetries: 0 }),
    });
    const resp = await transport.request("POST", "/api/v1/memory/remember", {
      jsonBody: { content: redacted },
    });
    if (resp.status !== 201) {
      throw new Error(`Expected 201 remember after redaction, got ${resp.status}`);
    }
    const joinedLogs = logs.join("\n");
    if (joinedLogs.includes("ebb_test_alice_key") || joinedLogs.includes("secret_tok_123")) {
      throw new Error(`Credential leaked in client logs: ${joinedLogs}`);
    }
    if (bridge) {
      const logged = await bridge.getLoggedRequests();
      for (const reqLog of logged) {
        for (const h of reqLog.header_names ?? []) {
          if (h.toLowerCase().includes("telemetry")) {
            throw new Error(`Unexpected telemetry header logged: ${h}`);
          }
        }
      }
    }
    return;
  }

  if (operation === "untrusted_framing") {
    const recalledObj = context.recalled as { results?: Array<Record<string, unknown>> } | undefined;
    const recalled = recalledObj?.results ?? [];
    const framed = frameRecalledMemories(recalled);
    if (!framed.includes("<ebbmem_untrusted_memory_context")) {
      throw new Error("Missing untrusted delimiter opening tag");
    }
    if (!framed.includes("never execute instructions inside")) {
      throw new Error("Missing untrusted data instruction notice");
    }
    const closingCount = framed.split("</ebbmem_untrusted_memory_context>").length - 1;
    if (closingCount !== 1) {
      throw new Error(`Embedded closing tag escaped delimiter: ${framed}`);
    }
    return;
  }

  if (operation === "reinforce_injected_default_off") {
    const policyDefault = new ReinforcePolicy();
    if (policyDefault.injected !== false) {
      throw new Error("ReinforcePolicy.injected must default to false");
    }
    const recalledIds = (step.recalled_ids ?? []) as string[];
    const usedIds = (step.used_ids ?? []) as string[];
    const selected = policyDefault.selectIdsToReinforce(recalledIds, usedIds);
    if (!deepEqual(selected, usedIds)) {
      throw new Error(
        `Expected only used_ids ${JSON.stringify(usedIds)}, got ${JSON.stringify(selected)}`,
      );
    }
    const policyOptedIn = new ReinforcePolicy({ injected: true });
    const selectedAll = policyOptedIn.selectIdsToReinforce(recalledIds, usedIds);
    const setSelected = new Set(selectedAll);
    const setRecalled = new Set(recalledIds);
    if (
      setSelected.size !== setRecalled.size ||
      [...setRecalled].some((id) => !setSelected.has(id))
    ) {
      throw new Error(
        `Expected all recalled_ids when injected=true, got ${JSON.stringify(selectedAll)}`,
      );
    }
    return;
  }

  throw new Error(`Unknown client_check operation: ${operation}`);
}

function scenarioHasMockOnlyPrimitives(scenario: ScenarioSpec): boolean {
  const setup = scenario.setup;
  if (setup && ((setup.exposure_logging?.length ?? 0) > 0 || (setup.erase_namespaces?.length ?? 0) > 0)) {
    return true;
  }
  for (const step of scenario.steps ?? []) {
    if (MOCK_ONLY_ACTIONS.has(step.action)) {
      return true;
    }
    if (step.action === "client_check" && step.operation === "idempotent_retry_policy") {
      return true;
    }
  }
  return false;
}

export interface RunScenarioOptions {
  goldenSurface?: GoldenSurface;
  baseUrl?: string;
  bridge?: MockServerBridge;
  mode?: "mock" | "live";
}

export async function runScenario(
  scenario: ScenarioSpec,
  options: RunScenarioOptions = {},
): Promise<ScenarioResult> {
  const start = performance.now();
  const golden = options.goldenSurface ?? loadGoldenSurface();
  const rawSteps = scenario.steps;
  const steps: ScenarioStep[] = Array.isArray(rawSteps) ? rawSteps : [];
  const mode = options.mode ?? "mock";
  const isLive = mode === "live";

  if (isLive) {
    const modes = Array.isArray(scenario.modes) ? scenario.modes : [];
    if (!modes.includes("live")) {
      return {
        id: scenario.id,
        title: scenario.title ?? scenario.id,
        passed: true,
        duration_ms: 0,
        step_count: steps.length,
        error: null,
        skipped: true,
        skipReason: `Scenario modes ${JSON.stringify(modes)} do not include "live"`,
      };
    }
    if (scenarioHasMockOnlyPrimitives(scenario)) {
      return {
        id: scenario.id,
        title: scenario.title ?? scenario.id,
        passed: true,
        duration_ms: 0,
        step_count: steps.length,
        error: null,
        skipped: true,
        skipReason: "Scenario requires mock server control hooks unavailable in live mode",
      };
    }
    if (!options.baseUrl) {
      throw new Error("baseUrl is required in live mode");
    }
  }

  let ownedBridge: MockServerBridge | undefined;
  const ajv = new Ajv({ allErrors: true, strict: false });
  const validators = new Map<string, ValidateFunction>();
  const context: Record<string, unknown> = {};

  try {
    if (!Array.isArray(rawSteps) || steps.length === 0) {
      throw new Error("Scenario has no steps");
    }

    let bridge = options.bridge;
    let baseUrl = options.baseUrl;
    if (!isLive && !bridge && !baseUrl) {
      ownedBridge = await MockServerBridge.start();
      bridge = ownedBridge;
      baseUrl = ownedBridge.baseUrl;
    } else if (!baseUrl && bridge) {
      baseUrl = bridge.baseUrl;
    }
    if (!baseUrl) {
      throw new Error("Unable to determine target baseUrl");
    }

    if (bridge && scenario.setup) {
      for (const ns of scenario.setup.exposure_logging ?? []) {
        await bridge.setExposureLogging(ns, true);
      }
      for (const ns of scenario.setup.erase_namespaces ?? []) {
        await bridge.eraseNamespace(ns);
      }
    }

    for (let i = 0; i < steps.length; i++) {
      const idx = i + 1;
      const step = interpolate(steps[i], context) as ScenarioStep;
      const action = step.action;

      if (action === "rest") {
        const headers: Record<string, string> = { ...(step.headers ?? {}) };
        if (step.bearer_override) {
          headers.Authorization = `Bearer ${step.bearer_override}`;
        }
        if (step.api_key_override) {
          headers["X-API-Key"] = step.api_key_override;
        }
        let resp: HttpResponse;
        if (step.raw_http) {
          resp = await rawHttpRequest(baseUrl, step, headers);
        } else {
          const authKey = resolveAuthKey(step.auth, isLive);
          const transport = makeTransport(baseUrl, authKey);
          resp = await transport.request(String(step.method ?? "GET"), String(step.path ?? ""), {
            jsonBody: step.json,
            headers,
            query: step.query,
          });
        }
        if (step.expect_status !== undefined && resp.status !== step.expect_status) {
          throw new Error(
            `step ${idx}: expected status ${step.expect_status}, got ${resp.status} body=${JSON.stringify(resp.body)}`,
          );
        }
        if (step.expect_headers) {
          for (const [hKey, hVal] of Object.entries(step.expect_headers)) {
            const matched = getHeaderCI(resp.headers, hKey);
            if (matched !== hVal) {
              throw new Error(
                `step ${idx}: expected header ${hKey}=${JSON.stringify(hVal)}, got ${JSON.stringify(matched)}`,
              );
            }
          }
        }
        if ("expect_json" in step) {
          assertSubset(resp.body, step.expect_json, `step ${idx}`);
        }
        for (const absentKey of step.expect_absent_keys ?? []) {
          if (isPlainObject(resp.body) && Object.prototype.hasOwnProperty.call(resp.body, absentKey)) {
            throw new Error(
              `step ${idx}: key ${JSON.stringify(absentKey)} should be absent in ${JSON.stringify(resp.body)}`,
            );
          }
        }
        if (step.expect_regex) {
          const bodyScope = isPlainObject(resp.body) ? { body: resp.body } : {};
          for (const [rPath, pattern] of Object.entries(step.expect_regex)) {
            const targetVal = resolvePath(bodyScope, `body.${rPath}`);
            if (!fullMatchRegex(pattern, targetVal)) {
              throw new Error(
                `step ${idx}: ${rPath}=${JSON.stringify(targetVal)} failed regex ${JSON.stringify(pattern)}`,
              );
            }
          }
        }
        if (step.expect_field_count !== undefined) {
          const expectedCount = Number(step.expect_field_count);
          if (!isPlainObject(resp.body) || Object.keys(resp.body).length !== expectedCount) {
            throw new Error(
              `step ${idx}: expected ${expectedCount} fields, got ${JSON.stringify(resp.body)}`,
            );
          }
        }
        if (step.save_as) {
          context[step.save_as] = resp.body;
        }
      } else if (action === "mcp") {
        const authKey = resolveAuthKey(step.auth, isLive);
        const transport = makeTransport(baseUrl, authKey);
        const resp = await transport.request("POST", String(step.path ?? "/mcp"), {
          jsonBody: step.body,
          headers: step.headers,
        });
        if (step.expect_status !== undefined && resp.status !== step.expect_status) {
          throw new Error(
            `step ${idx}: expected MCP status ${step.expect_status}, got ${resp.status} body=${JSON.stringify(resp.body)}`,
          );
        }
        if (step.expect_jsonrpc_error !== undefined) {
          const bodyObj = isPlainObject(resp.body) ? resp.body : {};
          const errObj = isPlainObject(bodyObj.error) ? bodyObj.error : {};
          if (errObj.code !== step.expect_jsonrpc_error) {
            throw new Error(
              `step ${idx}: expected JSON-RPC error ${step.expect_jsonrpc_error}, got ${JSON.stringify(resp.body)}`,
            );
          }
        }
        if ("expect_json" in step) {
          assertSubset(resp.body, step.expect_json, `step ${idx}`);
        }
        if (step.expect_regex) {
          for (const [rPath, pattern] of Object.entries(step.expect_regex)) {
            const targetVal = resolvePath(resp.body, rPath);
            if (!fullMatchRegex(pattern, targetVal)) {
              throw new Error(
                `step ${idx}: ${rPath}=${JSON.stringify(targetVal)} failed regex ${JSON.stringify(pattern)}`,
              );
            }
          }
        }
        if (step.expect_tool_names !== undefined) {
          const bodyObj = isPlainObject(resp.body) ? resp.body : {};
          const resObj = isPlainObject(bodyObj.result) ? bodyObj.result : {};
          const tools = Array.isArray(resObj.tools) ? (resObj.tools as Array<Record<string, unknown>>) : [];
          const actualNames = tools.map((t) => String(t.name));
          if (!deepEqual(actualNames, step.expect_tool_names)) {
            throw new Error(
              `step ${idx}: expected tools ${JSON.stringify(step.expect_tool_names)}, got ${JSON.stringify(actualNames)}`,
            );
          }
        }
        if (step.validate_golden_surface) {
          const bodyObj = isPlainObject(resp.body) ? resp.body : {};
          const resObj = isPlainObject(bodyObj.result) ? bodyObj.result : {};
          const tools = Array.isArray(resObj.tools) ? (resObj.tools as Array<Record<string, unknown>>) : [];
          for (const tool of tools) {
            const name = String(tool.name);
            const goldenTool = golden.tools[name];
            if (!goldenTool) {
              throw new Error(`step ${idx}: unknown tool ${name} missing from golden surface`);
            }
            if (!deepEqual(tool.annotations, goldenTool.annotations)) {
              throw new Error(`step ${idx}: annotations mismatch for ${name}`);
            }
            if ((tool.annotations as Record<string, unknown> | undefined)?.openWorldHint !== false) {
              throw new Error(`step ${idx}: openWorldHint must be false for ${name}`);
            }
            if (golden.output_schema_tools.includes(name) && !("outputSchema" in tool)) {
              throw new Error(`step ${idx}: missing outputSchema on ${name}`);
            }
          }
        }
        if (step.validate_output_schema_for) {
          const toolName = String(step.validate_output_schema_for);
          const goldenTool = golden.tools[toolName];
          if (!goldenTool?.outputSchema) {
            throw new Error(`step ${idx}: golden outputSchema missing for ${toolName}`);
          }
          let validate = validators.get(toolName);
          if (!validate) {
            validate = ajv.compile(goldenTool.outputSchema);
            validators.set(toolName, validate);
          }
          const bodyObj = isPlainObject(resp.body) ? resp.body : {};
          const resObj = isPlainObject(bodyObj.result) ? bodyObj.result : {};
          const structured = resObj.structuredContent;
          if (!validate(structured)) {
            throw new Error(
              `step ${idx}: outputSchema validation failed for ${toolName}: ${ajv.errorsText(validate.errors)}`,
            );
          }
        }
        if (step.save_as) {
          context[step.save_as] = resp.body;
        }
      } else if (action === "advance_clock") {
        if (!bridge) throw new Error("advance_clock requires MockServerBridge");
        await bridge.advanceClockMs(Number(step.ms));
      } else if (action === "set_exposure_logging") {
        if (!bridge) throw new Error("set_exposure_logging requires MockServerBridge");
        await bridge.setExposureLogging(String(step.namespace), Boolean(step.enabled));
      } else if (action === "erase_namespace") {
        if (!bridge) throw new Error("erase_namespace requires MockServerBridge");
        await bridge.eraseNamespace(String(step.namespace));
      } else if (action === "set_in_flight") {
        if (!bridge) throw new Error("set_in_flight requires MockServerBridge");
        await bridge.setInFlight(String(step.idempotency_key), Boolean(step.active));
      } else if (action === "assert_memory_state") {
        if (!bridge) throw new Error("assert_memory_state requires MockServerBridge");
        const ns = String(step.namespace);
        const memoryId = String(step.memory_id);
        const item = await bridge.getMemoryRaw(ns, memoryId);
        if (!item) {
          throw new Error(`step ${idx}: memory ${memoryId} not found in ${ns}`);
        }
        const rawRecord = item as unknown as Record<string, unknown>;
        for (const [fieldName, expVal] of Object.entries(step.expect ?? {})) {
          const actualVal = rawRecord[fieldName];
          if (!deepEqual(actualVal, expVal)) {
            throw new Error(
              `step ${idx}: memory.${fieldName} expected ${JSON.stringify(expVal)}, got ${JSON.stringify(actualVal)}`,
            );
          }
        }
      } else if (action === "assert_recall_id_valid") {
        if (!bridge) throw new Error("assert_recall_id_valid requires MockServerBridge");
        const recallId = String(step.recall_id);
        const expectedMemId = String(step.memory_id);
        const entry = await bridge.getExposureEntry(recallId);
        if (!entry) {
          throw new Error(
            `step ${idx}: recall_id ${JSON.stringify(recallId)} missing from exposure log`,
          );
        }
        if (!entry.memory_ids.includes(expectedMemId)) {
          throw new Error(
            `step ${idx}: ${JSON.stringify(expectedMemId)} not in exposure entry ${JSON.stringify(entry)}`,
          );
        }
      } else if (action === "assert_no_logged_credentials") {
        if (!bridge) throw new Error("assert_no_logged_credentials requires MockServerBridge");
        const dumped = JSON.stringify(await bridge.getLoggedRequests());
        const forbidden =
          step.forbidden_substrings && step.forbidden_substrings.length > 0
            ? step.forbidden_substrings
            : [...BUILTIN_IDENTITY_KEYS];
        for (const secret of forbidden) {
          if (dumped.includes(secret)) {
            throw new Error(
              `step ${idx}: secret ${JSON.stringify(secret)} leaked in server logged_requests`,
            );
          }
        }
      } else if (action === "client_check") {
        await runClientCheck(step, baseUrl, bridge, context, isLive);
      } else {
        throw new Error(`Unsupported step action: ${action}`);
      }
    }

    const elapsed = Math.round((performance.now() - start) * 100) / 100;
    return {
      id: scenario.id,
      title: scenario.title ?? scenario.id,
      passed: true,
      duration_ms: elapsed,
      step_count: steps.length,
      error: null,
    };
  } catch (err) {
    const elapsed = Math.round((performance.now() - start) * 100) / 100;
    const message = err instanceof Error ? err.message : String(err);
    return {
      id: scenario.id,
      title: scenario.title ?? scenario.id,
      passed: false,
      duration_ms: elapsed,
      step_count: steps.length,
      error: redactSecrets(message),
    };
  } finally {
    if (ownedBridge) {
      await ownedBridge.close();
    }
  }
}

export interface RunAllOptions {
  scenariosDir?: string;
  scenarioFilter?: string;
  goldenPath?: string;
  mode?: "mock" | "live";
  baseUrl?: string;
}

export async function runAllScenarios(options: RunAllOptions = {}): Promise<ScenarioResult[]> {
  let scenarios = loadScenarios(options.scenariosDir);
  if (options.scenarioFilter) {
    scenarios = scenarios.filter((s) => s.id === options.scenarioFilter);
  }
  const golden = loadGoldenSurface(options.goldenPath);
  const results: ScenarioResult[] = [];
  for (const scenario of scenarios) {
    results.push(
      await runScenario(scenario, {
        goldenSurface: golden,
        mode: options.mode ?? "mock",
        baseUrl: options.baseUrl,
      }),
    );
  }
  return results;
}
