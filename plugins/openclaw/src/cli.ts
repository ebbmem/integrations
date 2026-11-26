import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
  ValidationError,
  validateAuthCredentials,
  validateBaseUrl,
  validateNamespace,
} from "ebbmem-integration-core";
import {
  EbbmemOpenClawConfig,
  parsePluginConfig,
  redactPluginSecrets,
  resolveScopeForContext,
} from "./config.js";
import { OpenClawPluginApi } from "./openclaw-types.js";
import { EbbmemOpenClawRuntime } from "./runtime.js";

export const DEFAULT_LOCAL_DOCKER_URL = "http://127.0.0.1:8080";
const SETUP_PHRASE_RE = /\b(?:setup|set\s+up|configure|connect|init(?:ialize)?)\s+ebbmem\b/i;

export interface InitWizardOptions {
  mode?: "remote" | "docker";
  url?: string;
  apiKey?: string;
  bearerToken?: string;
  tenant?: string;
  userId?: string;
  namespace?: string;
  agentNamespaces?: Record<string, string>;
  configPath?: string;
  yes?: boolean;
  promptFn?: (question: string, fallback?: string) => Promise<string>;
  readFileFn?: (filePath: string) => string;
  writeFileFn?: (filePath: string, content: string) => void;
  healthProbeFn?: (config: EbbmemOpenClawConfig) => Promise<{ ok: boolean; status: string; error?: string }>;
  stdout?: (line: string) => void;
}

export interface InitWizardResult {
  mode: "remote" | "docker";
  configPath: string;
  pluginConfig: Record<string, unknown>;
  health: { ok: boolean; status: string; error?: string };
}

export function defaultOpenClawConfigPath(): string {
  return path.join(os.homedir(), ".openclaw", "openclaw.json");
}

export function matchSetupPhrase(text: string | undefined): boolean {
  if (!text) return false;
  return SETUP_PHRASE_RE.test(text);
}

export function buildSetupGuidance(config?: EbbmemOpenClawConfig): string {
  const configuredUrl = config?.baseUrl ? `Current baseUrl: ${config.baseUrl}` : "Currently unconfigured (no baseUrl set).";
  return [
    "To configure EbbMem cognitive memory for OpenClaw:",
    `1. Status: ${configuredUrl}`,
    "2. Run `openclaw ebbmem init` (or `openclaw ebbmem init --mode docker` for local Docker at http://127.0.0.1:8080).",
    "3. For remote EbbMem, provide your HTTPS URL and either an X-API-Key (`--api-key`) or Authorization Bearer token (`--bearer-token`), never in URL query strings.",
    "4. Set `plugins.slots.memory = \"ebbmem\"` and keep `injected: false` and `telemetryEnabled: false` (default).",
  ].join("\n");
}

function parseAgentNamespaceFlags(raw: string[] | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  for (const item of raw ?? []) {
    const eq = item.indexOf("=");
    if (eq <= 0 || eq === item.length - 1) {
      throw new ValidationError(`Invalid --agent-namespace entry '${item}'; expected agentId=namespace`);
    }
    const agentId = item.slice(0, eq).trim();
    const ns = validateNamespace(item.slice(eq + 1).trim());
    out[agentId] = ns;
  }
  return out;
}

export async function runInitWizard(options: InitWizardOptions = {}): Promise<InitWizardResult> {
  const prompt = options.promptFn;
  const log = options.stdout ?? (() => {});

  let mode: "remote" | "docker" = options.mode ?? "remote";
  if (!options.mode && prompt && !options.yes) {
    const modeInput = (await prompt("Deployment mode (remote/docker)", "remote")).trim().toLowerCase();
    mode = modeInput === "docker" || modeInput === "local" ? "docker" : "remote";
  }

  let rawUrl = options.url;
  if (!rawUrl) {
    if (mode === "docker") {
      rawUrl = prompt && !options.yes
        ? await prompt("Local EbbMem Docker URL", DEFAULT_LOCAL_DOCKER_URL)
        : DEFAULT_LOCAL_DOCKER_URL;
    } else if (prompt && !options.yes) {
      rawUrl = await prompt("Remote EbbMem Base URL (https://...)");
    } else {
      throw new ValidationError("Remote mode requires --url (or interactive prompt)");
    }
  }
  const baseUrl = validateBaseUrl(rawUrl);

  let apiKey = options.apiKey;
  let bearerToken = options.bearerToken;
  if (!apiKey && !bearerToken && prompt && !options.yes) {
    const authChoice = (await prompt("Auth header type (api-key/bearer/none)", mode === "remote" ? "api-key" : "none"))
      .trim()
      .toLowerCase();
    if (authChoice === "api-key" || authChoice === "apikey") {
      apiKey = (await prompt("X-API-Key value")).trim() || undefined;
    } else if (authChoice === "bearer") {
      bearerToken = (await prompt("Bearer token value")).trim() || undefined;
    }
  }
  validateAuthCredentials({ apiKey, bearerToken });

  const tenant = options.tenant?.trim() || "default";
  const userId = options.userId?.trim() || undefined;
  const appNamespace = options.namespace ? validateNamespace(options.namespace.trim()) : undefined;
  const agentNamespaces = options.agentNamespaces ?? {};

  const pluginEntryConfig: Record<string, unknown> = {
    baseUrl,
    ...(apiKey ? { apiKey } : {}),
    ...(bearerToken ? { bearerToken } : {}),
    tenant,
    ...(userId ? { userId } : {}),
    ...(appNamespace ? { appNamespace } : {}),
    ...(Object.keys(agentNamespaces).length > 0 ? { agentNamespaces } : {}),
    autoRecall: true,
    autoCapture: true,
    autoReinforce: true,
    preCompactionFlush: true,
    injected: false,
    redactBeforeRemember: true,
    failOpen: true,
    telemetryEnabled: false,
  };

  const validatedConfig = parsePluginConfig(pluginEntryConfig, {});

  const configPath = options.configPath ?? defaultOpenClawConfigPath();
  const readFn =
    options.readFileFn ??
    ((p: string) => (fs.existsSync(p) ? fs.readFileSync(p, "utf8") : "{}"));
  const writeFn =
    options.writeFileFn ??
    ((p: string, content: string) => {
      fs.mkdirSync(path.dirname(p), { recursive: true });
      fs.writeFileSync(p, content, "utf8");
    });

  let existing: Record<string, unknown> = {};
  try {
    const rawJson = readFn(configPath);
    if (rawJson.trim()) {
      const parsed = JSON.parse(rawJson);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        existing = parsed as Record<string, unknown>;
      }
    }
  } catch (err) {
    throw new ValidationError(
      `Failed to parse existing OpenClaw config at ${configPath}: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  const plugins = (existing.plugins && typeof existing.plugins === "object" && !Array.isArray(existing.plugins)
    ? { ...(existing.plugins as Record<string, unknown>) }
    : {}) as Record<string, unknown>;
  const slots = (plugins.slots && typeof plugins.slots === "object" && !Array.isArray(plugins.slots)
    ? { ...(plugins.slots as Record<string, unknown>) }
    : {}) as Record<string, unknown>;
  const entries = (plugins.entries && typeof plugins.entries === "object" && !Array.isArray(plugins.entries)
    ? { ...(plugins.entries as Record<string, unknown>) }
    : {}) as Record<string, unknown>;

  slots.memory = "ebbmem";
  entries.ebbmem = {
    enabled: true,
    config: pluginEntryConfig,
  };
  plugins.slots = slots;
  plugins.entries = entries;
  const updated = { ...existing, plugins };

  writeFn(configPath, `${JSON.stringify(updated, null, 2)}\n`);

  const probe =
    options.healthProbeFn ??
    (async (cfg: EbbmemOpenClawConfig) => {
      const tempRuntime = new EbbmemOpenClawRuntime({ config: cfg });
      try {
        const res = await tempRuntime.health();
        return { ok: res.ok, status: res.status, ...(res.error ? { error: res.error } : {}) };
      } finally {
        await tempRuntime.close();
      }
    });

  const health = await probe(validatedConfig);
  const authSummary = apiKey ? "X-API-Key (configured)" : bearerToken ? "Authorization Bearer (configured)" : "none";
  log(`Configured EbbMem memory plugin in ${configPath} (mode=${mode}, url=${baseUrl}, auth=${authSummary}, health=${health.status})`);

  return {
    mode,
    configPath,
    pluginConfig: pluginEntryConfig,
    health,
  };
}

export function registerEbbmemCli(
  api: OpenClawPluginApi,
  runtimeOrGetter: EbbmemOpenClawRuntime | (() => EbbmemOpenClawRuntime),
): void {
  const getRuntime =
    typeof runtimeOrGetter === "function" ? runtimeOrGetter : () => runtimeOrGetter;

  api.registerCli(
    ({ program, configPath, logger }) => {
      const root = program
        .command("ebbmem")
        .description("Configure and inspect the EbbMem cognitive memory plugin");

      root
        .command("init")
        .description("Configure EbbMem remote or local Docker endpoint in openclaw.json")
        .option("--mode <mode>", "Connection mode: remote or docker", "remote")
        .option("--url <url>", "EbbMem HTTP base URL")
        .option("--api-key <key>", "EbbMem X-API-Key header credential")
        .option("--bearer-token <token>", "EbbMem Authorization Bearer token")
        .option("--tenant <tenant>", "Default tenant prefix", "default")
        .option("--user-id <userId>", "Default user ID")
        .option("--namespace <namespace>", "Explicit fallback namespace")
        .option(
          "--agent-namespace <mapping...>",
          "Per-agent namespace override (agentId=namespace)",
          (val: string, prev: string[] = []) => [...prev, val],
          [],
        )
        .option("--config-path <path>", "Path to openclaw.json")
        .option("-y, --yes", "Non-interactive mode")
        .action(async (cmdOpts: Record<string, unknown>) => {
          const agentNsRaw = Array.isArray(cmdOpts.agentNamespace)
            ? (cmdOpts.agentNamespace as string[])
            : undefined;
          const result = await runInitWizard({
            mode: cmdOpts.mode === "docker" ? "docker" : "remote",
            url: typeof cmdOpts.url === "string" ? cmdOpts.url : undefined,
            apiKey: typeof cmdOpts.apiKey === "string" ? cmdOpts.apiKey : undefined,
            bearerToken: typeof cmdOpts.bearerToken === "string" ? cmdOpts.bearerToken : undefined,
            tenant: typeof cmdOpts.tenant === "string" ? cmdOpts.tenant : undefined,
            userId: typeof cmdOpts.userId === "string" ? cmdOpts.userId : undefined,
            namespace: typeof cmdOpts.namespace === "string" ? cmdOpts.namespace : undefined,
            agentNamespaces: parseAgentNamespaceFlags(agentNsRaw),
            configPath:
              typeof cmdOpts.configPath === "string" ? cmdOpts.configPath : configPath,
            yes: Boolean(cmdOpts.yes),
            stdout: (msg) => logger?.info(msg),
          });
          logger?.info(`EbbMem init complete (health=${result.health.status})`);
        });

      root
        .command("status")
        .description("Check EbbMem runtime health and resolved namespace configuration")
        .option("--agent-id <agentId>", "OpenClaw agentId to resolve namespace for")
        .action(async (cmdOpts: Record<string, unknown>) => {
          const runtime = getRuntime();
          const agentId = typeof cmdOpts.agentId === "string" ? cmdOpts.agentId : undefined;
          const scope = resolveScopeForContext(runtime.config, { agentId });
          const health = await runtime.health();
          const summary = {
            provider: "ebbmem",
            configured: Boolean(runtime.config.baseUrl),
            baseUrl: runtime.config.baseUrl ?? null,
            namespace: scope.namespace,
            injected: runtime.config.injected,
            telemetryEnabled: runtime.config.telemetryEnabled,
            redactBeforeRemember: runtime.config.redactBeforeRemember,
            failOpen: runtime.config.failOpen,
            health,
          };
          logger?.info(redactPluginSecrets(JSON.stringify(summary)));
        });
    },
    {
      commands: ["ebbmem"],
      descriptors: [
        {
          name: "ebbmem",
          description: "Configure and inspect the EbbMem cognitive memory plugin",
          hasSubcommands: true,
        },
      ],
    },
  );
}

export function registerEbbmemSetupCommand(
  api: OpenClawPluginApi,
  runtimeOrGetter: EbbmemOpenClawRuntime | (() => EbbmemOpenClawRuntime),
): void {
  if (typeof api.registerCommand !== "function") {
    return;
  }
  const getRuntime =
    typeof runtimeOrGetter === "function" ? runtimeOrGetter : () => runtimeOrGetter;
  api.registerCommand({
    name: "ebbmem",
    description: "Show EbbMem setup instructions or check status (/ebbmem setup | /ebbmem status)",
    acceptsArgs: true,
    async handler(ctx) {
      const runtime = getRuntime();
      const arg = (ctx.args ?? "").trim().toLowerCase();
      if (!arg || arg === "setup" || arg === "init" || arg === "help") {
        return { text: buildSetupGuidance(runtime.config) };
      }
      if (arg === "status") {
        const health = await runtime.health();
        const scope = resolveScopeForContext(runtime.config, { agentId: ctx.agentId });
        return {
          text: `EbbMem status: health=${health.status} ok=${String(health.ok)} namespace=${scope.namespace} injected=${String(runtime.config.injected)} telemetry=${String(runtime.config.telemetryEnabled)}`,
        };
      }
      return {
        text: `Unknown /ebbmem subcommand '${arg}'. Try '/ebbmem setup' or '/ebbmem status'.`,
      };
    },
  });
}
