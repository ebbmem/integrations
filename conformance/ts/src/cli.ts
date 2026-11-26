#!/usr/bin/env node
import * as fs from "node:fs";
import * as path from "node:path";
import { redactSecrets, validateBaseUrl } from "ebbmem-integration-core";
import { loadAndEvaluateManifest } from "./badges";
import { runAllScenarios } from "./runner";
import { ConformanceMode, ConformanceReport } from "./types";

interface CliArgs {
  mode?: ConformanceMode;
  baseUrl?: string;
  scenario?: string;
  scenariosDir?: string;
  jsonReport?: string;
  badgesJson?: string;
  manifest?: string;
  help?: boolean;
}

function parseArgs(argv: string[]): CliArgs {
  const parsed: CliArgs = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--help" || arg === "-h") {
      parsed.help = true;
    } else if (arg === "--mode" && i + 1 < argv.length) {
      const modeVal = argv[++i];
      if (modeVal !== "mock" && modeVal !== "live") {
        throw new Error(`Invalid --mode ${JSON.stringify(modeVal)}; expected "mock" or "live"`);
      }
      parsed.mode = modeVal;
    } else if (arg === "--base-url" && i + 1 < argv.length) {
      parsed.baseUrl = argv[++i];
    } else if (arg === "--scenario" && i + 1 < argv.length) {
      parsed.scenario = argv[++i];
    } else if (arg === "--scenarios-dir" && i + 1 < argv.length) {
      parsed.scenariosDir = argv[++i];
    } else if (arg === "--json-report" && i + 1 < argv.length) {
      parsed.jsonReport = argv[++i];
    } else if (arg === "--badges-json" && i + 1 < argv.length) {
      parsed.badgesJson = argv[++i];
    } else if (arg === "--manifest" && i + 1 < argv.length) {
      parsed.manifest = argv[++i];
    } else {
      throw new Error(`Unknown or incomplete argument: ${arg}`);
    }
  }
  return parsed;
}

function printHelp(): void {
  console.log(
    [
      "Usage: ebbmem-conformance-ts --mode <mock|live> [options]",
      "",
      "Options:",
      "  --mode <mock|live>        Execution mode (required unless only --badges-json/--manifest)",
      "  --base-url <url>          Target server URL (required for --mode live)",
      "  --scenario <id>           Single IC-1 scenario ID to execute",
      "  --scenarios-dir <path>    Custom directory containing YAML scenario files",
      "  --json-report <path>      Write structured JSON report to file",
      "  --manifest <path>         Path to compat_manifest.json for badge evaluation",
      "  --badges-json <path>      Write evaluated adapter badges JSON to file",
      "  -h, --help                Show help",
    ].join("\n"),
  );
}

export async function main(argv: string[] = process.argv.slice(2)): Promise<number> {
  let args: CliArgs;
  try {
    args = parseArgs(argv);
  } catch (err) {
    console.error(`Error: ${err instanceof Error ? err.message : String(err)}`);
    return 1;
  }

  if (args.help) {
    printHelp();
    return 0;
  }

  if (args.badgesJson) {
    const manifestPath =
      args.manifest ?? path.resolve(__dirname, "..", "..", "scripts", "compat_manifest.json");
    const evaluations = loadAndEvaluateManifest(manifestPath);
    fs.mkdirSync(path.dirname(path.resolve(args.badgesJson)), { recursive: true });
    fs.writeFileSync(args.badgesJson, JSON.stringify({ badges: evaluations }, null, 2), "utf8");
    if (!args.mode) {
      console.log(`Evaluated ${evaluations.length} adapter badges -> ${args.badgesJson}`);
      return 0;
    }
  }

  if (!args.mode) {
    console.error("Error: --mode <mock|live> is required.");
    return 1;
  }

  let validatedBaseUrl: string | undefined;
  if (args.mode === "live") {
    if (!args.baseUrl) {
      console.error("Error: --base-url is required with --mode live.");
      return 1;
    }
    try {
      validatedBaseUrl = validateBaseUrl(args.baseUrl);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error(`Security/validation error in --base-url: ${redactSecrets(msg)}`);
      return 1;
    }
  }

  const results = await runAllScenarios({
    scenariosDir: args.scenariosDir,
    scenarioFilter: args.scenario,
    mode: args.mode,
    baseUrl: validatedBaseUrl,
  });

  if (results.length === 0) {
    console.error("Error: no matching conformance scenarios found.");
    return 1;
  }

  const skippedCount = results.filter((r) => r.skipped).length;
  const failedCount = results.filter((r) => !r.passed).length;
  const passedCount = results.filter((r) => r.passed && !r.skipped).length;

  console.log(
    `EbbMem IC-1 Conformance TS (${args.mode}): ${passedCount}/${results.length - skippedCount} executed passed` +
      (skippedCount > 0 ? ` (${skippedCount} skipped)` : ""),
  );
  for (const res of results) {
    const statusLabel = res.skipped ? "SKIP" : res.passed ? "PASS" : "FAIL";
    console.log(
      `  [${statusLabel}] ${res.id} (${res.step_count} steps, ${res.duration_ms}ms)`,
    );
    if (res.skipReason) {
      console.log(`         skip: ${res.skipReason}`);
    }
    if (res.error) {
      console.log(`         error: ${res.error}`);
    }
  }

  if (args.jsonReport) {
    const report: ConformanceReport = {
      mode: args.mode,
      passed: passedCount,
      failed: failedCount,
      skipped: skippedCount,
      total: results.length,
      results,
    };
    const outPath = path.resolve(args.jsonReport);
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    fs.writeFileSync(outPath, JSON.stringify(report, null, 2), "utf8");
  }

  return failedCount === 0 ? 0 : 1;
}

if (require.main === module) {
  main()
    .then((code) => {
      process.exitCode = code;
    })
    .catch((err) => {
      const msg = err instanceof Error ? err.message : String(err);
      console.error(`Fatal error: ${redactSecrets(msg)}`);
      process.exitCode = 1;
    });
}
