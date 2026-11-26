import * as fs from "node:fs";
import {
  AdapterEvidence,
  AdapterInvariants,
  BadgeEvaluation,
  BadgeTier,
  SuiteCounts,
  TargetKind,
  TargetStatus,
} from "./types";

export const BADGE_LABELS: Record<BadgeTier, string> = {
  gold: "Gold (Live + Mock Verified)",
  silver: "Silver (Mock Verified)",
  bronze: "Bronze (Surface Verified)",
  unverified: "Unverified",
};

export const REQUIRED_FRAMEWORK_CATEGORIES = [
  "scoping_isolation",
  "cognitive_lifecycle",
  "resilience",
  "security",
  "closure_and_mcp",
] as const;

export interface RawManifestSuiteCounts {
  passed: number;
  failed: number;
  required: number;
}

export interface RawManifestInvariants {
  header_only_auth: boolean;
  telemetry_disabled_default: boolean;
  read_only_recall: boolean;
  injected_default_off: boolean;
  untrusted_framing: boolean;
  secret_redaction: boolean;
}

export interface RawManifestEvidence {
  kind: TargetKind;
  status: TargetStatus;
  mcp_supported: boolean;
  mcp_surface_validated: boolean;
  config_schema_validated: boolean;
  mock_results: RawManifestSuiteCounts;
  live_results: RawManifestSuiteCounts;
  invariants: RawManifestInvariants;
  ic1_categories_covered?: string[];
}

export interface CompatManifestRow {
  id: string;
  target: string;
  mcp_remote: string;
  mcp_local: string;
  native_shape: string;
  distribution: string;
  notes: string;
  evidence: RawManifestEvidence;
}

export interface CompatManifest {
  schema_version: string;
  targets: CompatManifestRow[];
}

export function normalizeManifestEvidence(row: CompatManifestRow): AdapterEvidence {
  const ev = row.evidence;
  const mockResults: SuiteCounts = {
    passed: Number(ev.mock_results.passed),
    failed: Number(ev.mock_results.failed),
    required: Number(ev.mock_results.required),
  };
  const liveResults: SuiteCounts = {
    passed: Number(ev.live_results.passed),
    failed: Number(ev.live_results.failed),
    required: Number(ev.live_results.required),
  };
  const invariants: AdapterInvariants = {
    headerOnlyAuth: Boolean(ev.invariants.header_only_auth),
    telemetryDisabledDefault: Boolean(ev.invariants.telemetry_disabled_default),
    readOnlyRecall: Boolean(ev.invariants.read_only_recall),
    injectedDefaultOff: Boolean(ev.invariants.injected_default_off),
    untrustedFraming: Boolean(ev.invariants.untrusted_framing),
    secretRedaction: Boolean(ev.invariants.secret_redaction),
  };
  return {
    id: row.id,
    target: row.target,
    kind: ev.kind,
    status: ev.status,
    mcpSupported: Boolean(ev.mcp_supported),
    mcpSurfaceValidated: Boolean(ev.mcp_surface_validated),
    configSchemaValidated: Boolean(ev.config_schema_validated),
    mockResults,
    liveResults,
    invariants,
    ic1CategoriesCovered: ev.ic1_categories_covered,
  };
}

function checkSafetyGate(ev: AdapterEvidence): string[] {
  const failures: string[] = [];
  if (ev.status !== "available") {
    failures.push(`status is ${JSON.stringify(ev.status)} (must be "available")`);
  }
  if (!ev.invariants.headerOnlyAuth) {
    failures.push("invariant headerOnlyAuth is false");
  }
  if (!ev.invariants.telemetryDisabledDefault) {
    failures.push("invariant telemetryDisabledDefault is false");
  }
  if (ev.kind === "framework-adapter" || ev.kind === "host-plugin") {
    if (!ev.invariants.injectedDefaultOff) {
      failures.push("invariant injectedDefaultOff is false");
    }
    if (!ev.invariants.readOnlyRecall) {
      failures.push("invariant readOnlyRecall is false");
    }
    if (!ev.invariants.untrustedFraming) {
      failures.push("invariant untrustedFraming is false");
    }
    if (!ev.invariants.secretRedaction) {
      failures.push("invariant secretRedaction is false");
    }
  }
  if (ev.mockResults.failed > 0) {
    failures.push(`mockResults.failed is ${ev.mockResults.failed}`);
  }
  if (ev.liveResults.failed > 0) {
    failures.push(`liveResults.failed is ${ev.liveResults.failed}`);
  }
  return failures;
}

function hasFrameworkIc1Coverage(ev: AdapterEvidence): { ok: boolean; reason: string } {
  if (ev.kind !== "framework-adapter") {
    return { ok: true, reason: "non-framework kind does not require 5-category matrix" };
  }
  if (ev.ic1CategoriesCovered && ev.ic1CategoriesCovered.length > 0) {
    const covered = new Set(ev.ic1CategoriesCovered);
    const missing = REQUIRED_FRAMEWORK_CATEGORIES.filter((cat) => !covered.has(cat));
    if (missing.length > 0) {
      return { ok: false, reason: `missing IC-1 categories: ${missing.join(", ")}` };
    }
    return { ok: true, reason: "all 5 IC-1 scenario categories covered" };
  }
  return { ok: true, reason: "full framework mock suite passed" };
}

export function evaluateBadge(ev: AdapterEvidence): BadgeEvaluation {
  const reasons: string[] = [];
  const safetyFailures = checkSafetyGate(ev);

  if (safetyFailures.length > 0) {
    return {
      id: ev.id,
      target: ev.target,
      tier: "unverified",
      label: BADGE_LABELS.unverified,
      reasons: safetyFailures.map((f) => `safety-gate-fail: ${f}`),
    };
  }
  reasons.push("safety-gate: passed");

  const fullMock = ev.mockResults.required > 0 && ev.mockResults.passed >= ev.mockResults.required;
  const fullLive = ev.liveResults.required > 0 && ev.liveResults.passed >= ev.liveResults.required;
  const mcpOk = !ev.mcpSupported || ev.mcpSurfaceValidated;

  if (fullMock && fullLive && mcpOk) {
    reasons.push(
      `mock: ${ev.mockResults.passed}/${ev.mockResults.required}`,
      `live: ${ev.liveResults.passed}/${ev.liveResults.required}`,
    );
    if (ev.mcpSupported) {
      reasons.push("mcpSurfaceValidated: true");
    }
    return {
      id: ev.id,
      target: ev.target,
      tier: "gold",
      label: BADGE_LABELS.gold,
      reasons,
    };
  }

  if (fullMock) {
    const categoryCheck = hasFrameworkIc1Coverage(ev);
    if (categoryCheck.ok) {
      reasons.push(
        `mock: ${ev.mockResults.passed}/${ev.mockResults.required}`,
        `live: ${ev.liveResults.passed}/${ev.liveResults.required} (untested live)`,
        categoryCheck.reason,
      );
      return {
        id: ev.id,
        target: ev.target,
        tier: "silver",
        label: BADGE_LABELS.silver,
        reasons,
      };
    }
    reasons.push(`silver-downgrade: ${categoryCheck.reason}`);
  }

  if (ev.mcpSurfaceValidated || ev.configSchemaValidated || ev.mockResults.passed > 0) {
    if (ev.mcpSurfaceValidated) reasons.push("mcpSurfaceValidated: true");
    if (ev.configSchemaValidated) reasons.push("configSchemaValidated: true");
    if (ev.mockResults.passed > 0) {
      reasons.push(`partial-mock: ${ev.mockResults.passed}/${ev.mockResults.required}`);
    }
    return {
      id: ev.id,
      target: ev.target,
      tier: "bronze",
      label: BADGE_LABELS.bronze,
      reasons,
    };
  }

  reasons.push("no mock, MCP surface, or config schema verification evidence");
  return {
    id: ev.id,
    target: ev.target,
    tier: "unverified",
    label: BADGE_LABELS.unverified,
    reasons,
  };
}

export function evaluateManifest(manifest: CompatManifest): BadgeEvaluation[] {
  return manifest.targets.map((row) => evaluateBadge(normalizeManifestEvidence(row)));
}

export function loadAndEvaluateManifest(manifestPath: string): BadgeEvaluation[] {
  const parsed = JSON.parse(fs.readFileSync(manifestPath, "utf8")) as CompatManifest;
  return evaluateManifest(parsed);
}
