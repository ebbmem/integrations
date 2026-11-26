import { EbbmemIntegrationCore, EbbmemTransport } from "ebbmem-integration-core";
import { registerEbbmemCli, registerEbbmemSetupCommand } from "./cli.js";
import { EbbmemOpenClawConfig, parsePluginConfig } from "./config.js";
import { registerEbbmemHooks } from "./hooks.js";
import { OpenClawPluginApi, definePluginEntry } from "./openclaw-types.js";
import { EbbmemOpenClawRuntime } from "./runtime.js";
import { registerEbbmemTools } from "./tools.js";

const registeredApis = new WeakSet<object>();
let activeRuntime: EbbmemOpenClawRuntime | null = null;

export interface RegisterEbbmemOptions {
  transport?: EbbmemTransport;
  core?: EbbmemIntegrationCore;
  configOverrides?: Partial<EbbmemOpenClawConfig>;
  env?: Record<string, string | undefined>;
}

export interface RegisterEbbmemResult {
  runtime: EbbmemOpenClawRuntime;
  reused: boolean;
}

export function register(
  api: OpenClawPluginApi,
  options: RegisterEbbmemOptions = {},
): RegisterEbbmemResult {
  if (registeredApis.has(api) && activeRuntime && !activeRuntime.isClosed()) {
    return { runtime: activeRuntime, reused: true };
  }

  if (activeRuntime && !activeRuntime.isClosed()) {
    void activeRuntime.close();
  }

  const parsed = parsePluginConfig(
    {
      ...(api.pluginConfig ?? {}),
      ...(options.configOverrides ?? {}),
    },
    options.env,
  );

  const runtime = new EbbmemOpenClawRuntime({
    config: parsed,
    transport: options.transport,
    core: options.core,
    logger: api.logger,
  });

  api.registerMemoryCapability({
    providerRuntime: runtime.providerRuntime,
    runtime: runtime.providerRuntime,
    promptBuilder: runtime.promptBuilder,
    flushPlanResolver: runtime.flushPlanResolver,
  });

  registerEbbmemTools(api, runtime);
  registerEbbmemHooks(api, runtime);
  registerEbbmemCli(api, runtime);
  registerEbbmemSetupCommand(api, runtime);

  registeredApis.add(api);
  activeRuntime = runtime;

  return { runtime, reused: false };
}

export function getActiveEbbmemRuntime(): EbbmemOpenClawRuntime | null {
  return activeRuntime;
}

export async function resetActiveEbbmemRuntime(): Promise<void> {
  if (activeRuntime) {
    await activeRuntime.close();
    activeRuntime = null;
  }
}

export default definePluginEntry({
  id: "ebbmem",
  name: "EbbMem Cognitive Memory",
  description:
    "Remote and self-hosted EbbMem cognitive memory slot provider for OpenClaw with read-only recall, decay reinforcement, secret redaction, and per-agent namespace isolation.",
  kind: "memory",
  register: (api: OpenClawPluginApi) => register(api),
});

export * from "./openclaw-types.js";
export * from "./config.js";
export * from "./runtime.js";
export * from "./tools.js";
export * from "./hooks.js";
export * from "./cli.js";
