import * as assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";
import {
  evaluateBadge,
  evaluateManifest,
  findRepoRoot,
  loadAndEvaluateManifest,
  resolvePythonExecutable,
} from "../src";
import { CompatManifest } from "../src/badges";
import { AdapterEvidence } from "../src/types";

function makeBaseEvidence(overrides: Partial<AdapterEvidence> = {}): AdapterEvidence {
  return {
    id: "test-adapter",
    target: "Test Adapter",
    kind: "framework-adapter",
    status: "available",
    mcpSupported: true,
    mcpSurfaceValidated: true,
    configSchemaValidated: true,
    mockResults: { passed: 21, failed: 0, required: 21 },
    liveResults: { passed: 21, failed: 0, required: 21 },
    invariants: {
      headerOnlyAuth: true,
      telemetryDisabledDefault: true,
      readOnlyRecall: true,
      injectedDefaultOff: true,
      untrustedFraming: true,
      secretRedaction: true,
    },
    ic1CategoriesCovered: [
      "scoping_isolation",
      "cognitive_lifecycle",
      "resilience",
      "security",
      "closure_and_mcp",
    ],
    ...overrides,
  };
}

test("badge tiers: gold, silver, bronze, and unverified rules", () => {
  const gold = evaluateBadge(makeBaseEvidence());
  assert.equal(gold.tier, "gold");

  // Gold requires mcpSurfaceValidated when mcpSupported is true.
  const missingMcpSurface = evaluateBadge(makeBaseEvidence({ mcpSurfaceValidated: false }));
  assert.equal(missingMcpSurface.tier, "silver");

  const silver = evaluateBadge(
    makeBaseEvidence({
      liveResults: { passed: 0, failed: 0, required: 0 },
    }),
  );
  assert.equal(silver.tier, "silver");

  // Framework adapter missing one of the 5 IC-1 categories downgrades from silver to bronze.
  const partialCategories = evaluateBadge(
    makeBaseEvidence({
      liveResults: { passed: 0, failed: 0, required: 0 },
      ic1CategoriesCovered: ["scoping_isolation", "security"],
    }),
  );
  assert.equal(partialCategories.tier, "bronze");

  const bronze = evaluateBadge(
    makeBaseEvidence({
      kind: "host-plugin",
      mockResults: { passed: 1, failed: 0, required: 0 },
      liveResults: { passed: 0, failed: 0, required: 0 },
    }),
  );
  assert.equal(bronze.tier, "bronze");

  const planned = evaluateBadge(makeBaseEvidence({ status: "planned" }));
  assert.equal(planned.tier, "unverified");

  const inProgress = evaluateBadge(makeBaseEvidence({ status: "in progress" }));
  assert.equal(inProgress.tier, "unverified");
});

test("safety-gate downgrades force unverified on any violated invariant or suite failure", () => {
  const invariantKeys: Array<keyof AdapterEvidence["invariants"]> = [
    "headerOnlyAuth",
    "telemetryDisabledDefault",
    "readOnlyRecall",
    "injectedDefaultOff",
    "untrustedFraming",
    "secretRedaction",
  ];
  for (const key of invariantKeys) {
    const base = makeBaseEvidence();
    const mutated = makeBaseEvidence({
      invariants: { ...base.invariants, [key]: false },
    });
    const res = evaluateBadge(mutated);
    assert.equal(res.tier, "unverified", `Expected unverified when ${key}=false`);
    assert.ok(res.reasons.some((r) => r.includes(key)));
  }

  const failedMock = evaluateBadge(
    makeBaseEvidence({ mockResults: { passed: 20, failed: 1, required: 21 } }),
  );
  assert.equal(failedMock.tier, "unverified");

  const failedLive = evaluateBadge(
    makeBaseEvidence({ liveResults: { passed: 20, failed: 1, required: 21 } }),
  );
  assert.equal(failedLive.tier, "unverified");
});

test("compat_manifest.json evaluates accurately and matches generate_compat_table.py --check", () => {
  const repoRoot = findRepoRoot();
  const manifestPath = path.join(repoRoot, "conformance", "scripts", "compat_manifest.json");
  const readmePath = path.join(repoRoot, "docs", "integrations", "README.md");
  const generatorPath = path.join(repoRoot, "conformance", "scripts", "generate_compat_table.py");

  const evaluations = loadAndEvaluateManifest(manifestPath);
  const byId = new Map(evaluations.map((e) => [e.id, e]));

  assert.equal(evaluations.length, 16);
  assert.equal(byId.get("copilot-vscode")?.tier, "bronze");
  assert.equal(byId.get("copilot-cloud-agent")?.tier, "bronze");
  assert.equal(byId.get("claude-code")?.tier, "bronze");
  assert.equal(byId.get("cursor")?.tier, "bronze");
  assert.equal(byId.get("langgraph")?.tier, "silver");
  assert.equal(byId.get("vercel-ai-sdk")?.tier, "silver");
  assert.equal(byId.get("openai-agents-chatgpt")?.tier, "silver");
  assert.equal(byId.get("openclaw")?.tier, "unverified");
  assert.equal(byId.get("open-webui")?.tier, "unverified");

  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8")) as CompatManifest;
  const reEvaluated = evaluateManifest(manifest);
  assert.deepEqual(reEvaluated, evaluations);

  // Verify docs/integrations/README.md includes every evaluated badge tier.
  const readmeText = fs.readFileSync(readmePath, "utf8");
  for (const row of manifest.targets) {
    const badge = byId.get(row.id)!.tier;
    assert.ok(
      readmeText.includes(`| ${row.target} |`) && readmeText.includes(`| \`${badge}\` |`),
      `Missing expected row/badge for ${row.id} (${badge}) in docs/integrations/README.md`,
    );
  }

  // Verify generate_compat_table.py --check exits 0 on current repo and exits 1 on drift.
  const pythonBin = resolvePythonExecutable();
  const checkPass = spawnSync(
    pythonBin,
    [generatorPath, "--manifest", manifestPath, "--target", readmePath, "--check"],
    { encoding: "utf8" },
  );
  assert.equal(checkPass.status, 0, `Expected --check exit 0, stderr=${checkPass.stderr}`);

  const buildDir = path.resolve(__dirname, "..", "..", "build");
  fs.mkdirSync(buildDir, { recursive: true });
  const tmpDir = fs.mkdtempSync(path.join(buildDir, "tmp-badges-"));
  try {
    const driftedReadme = path.join(tmpDir, "README.md");
    fs.writeFileSync(
      driftedReadme,
      readmeText.replace("| `silver` |", "| `gold` |"),
      "utf8",
    );
    const checkFail = spawnSync(
      pythonBin,
      [generatorPath, "--manifest", manifestPath, "--target", driftedReadme, "--check"],
      { encoding: "utf8" },
    );
    assert.equal(checkFail.status, 1);
    assert.match(checkFail.stderr, /Compatibility table drift detected/);
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});
