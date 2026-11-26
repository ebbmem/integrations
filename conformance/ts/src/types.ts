export type ConformanceMode = "mock" | "live";

export interface ScenarioSetup {
  exposure_logging?: string[];
  erase_namespaces?: string[];
}

export interface ScenarioStep {
  action: string;
  method?: string;
  path?: string;
  auth?: string;
  headers?: Record<string, string>;
  query?: Record<string, string>;
  json?: unknown;
  body?: unknown;
  bearer_override?: string;
  api_key_override?: string;
  raw_http?: boolean;
  save_as?: string;
  expect_status?: number;
  expect_headers?: Record<string, string>;
  expect_json?: unknown;
  expect_absent_keys?: string[];
  expect_regex?: Record<string, string>;
  expect_field_count?: number;
  expect_jsonrpc_error?: number;
  expect_tool_names?: string[];
  validate_golden_surface?: boolean;
  validate_output_schema_for?: string;
  ms?: number;
  namespace?: string;
  enabled?: boolean;
  idempotency_key?: string;
  active?: boolean;
  memory_id?: string;
  recall_id?: string;
  expect?: Record<string, unknown>;
  forbidden_substrings?: string[];
  operation?: string;
  recalled_ids?: string[];
  used_ids?: string[];
  [key: string]: unknown;
}

export interface ScenarioSpec {
  id: string;
  title?: string;
  ic1_section?: string;
  modes?: string[];
  adapter_contract?: Record<string, unknown>;
  setup?: ScenarioSetup;
  steps?: ScenarioStep[];
  _source_file?: string;
}

export interface ScenarioResult {
  id: string;
  title: string;
  passed: boolean;
  duration_ms: number;
  step_count: number;
  error: string | null;
  skipped?: boolean;
  skipReason?: string;
}

export interface GoldenToolDefinition {
  name: string;
  title?: string;
  description?: string;
  annotations: {
    readOnlyHint: boolean;
    destructiveHint: boolean;
    idempotentHint: boolean;
    openWorldHint: boolean;
    [key: string]: unknown;
  };
  inputSchema?: Record<string, unknown>;
  outputSchema?: Record<string, unknown>;
}

export interface GoldenSurface {
  contract: string;
  version: string;
  revisions: {
    preferred: string;
    supported: string[];
    stateless_headers: string[];
  };
  profiles: Record<string, string[]>;
  universal_aliases: string[];
  admin_only_tools: string[];
  output_schema_tools: string[];
  tools: Record<string, GoldenToolDefinition>;
}

export interface ConformanceReport {
  mode: ConformanceMode;
  passed: number;
  failed: number;
  skipped: number;
  total: number;
  results: ScenarioResult[];
}

export type BadgeTier = "gold" | "silver" | "bronze" | "unverified";
export type TargetStatus = "available" | "in progress" | "planned";
export type TargetKind =
  | "conformance-kit"
  | "core-library"
  | "framework-adapter"
  | "host-plugin"
  | "host-config"
  | "recipe";

export interface SuiteCounts {
  passed: number;
  failed: number;
  required: number;
}

export interface AdapterInvariants {
  headerOnlyAuth: boolean;
  telemetryDisabledDefault: boolean;
  readOnlyRecall: boolean;
  injectedDefaultOff: boolean;
  untrustedFraming: boolean;
  secretRedaction: boolean;
}

export interface AdapterEvidence {
  id: string;
  target: string;
  kind: TargetKind;
  status: TargetStatus;
  mcpSupported: boolean;
  mcpSurfaceValidated: boolean;
  configSchemaValidated: boolean;
  mockResults: SuiteCounts;
  liveResults: SuiteCounts;
  invariants: AdapterInvariants;
  ic1CategoriesCovered?: string[];
}

export interface BadgeEvaluation {
  id: string;
  target: string;
  tier: BadgeTier;
  label: string;
  reasons: string[];
}
