import {
  CommanderCommandLike,
  MemoryCapabilityRegistration,
  OpenClawCliContext,
  OpenClawCliDescriptor,
  OpenClawCommandContext,
  OpenClawCommandDefinition,
  OpenClawCommandResult,
  OpenClawHookContext,
  OpenClawLogger,
  OpenClawPluginApi,
  OpenClawPluginConfig,
  OpenClawToolDefinition,
  OpenClawToolExecContext,
  OpenClawToolResult,
} from "../../src/openclaw-types.js";

function flagToCamelCase(flags: string): string {
  const longMatch = flags.match(/--([a-zA-Z0-9-]+)/);
  if (!longMatch) {
    throw new Error(`Invalid option flags '${flags}'; expected a --long-flag`);
  }
  return longMatch[1].replace(/-([a-zA-Z0-9])/g, (_m, c: string) => c.toUpperCase());
}

class MockCommanderNode implements CommanderCommandLike {
  readonly children = new Map<string, MockCommanderNode>();
  readonly registeredOptions = new Map<
    string,
    { defaultValue?: unknown; parser?: (value: string, previous: unknown) => unknown }
  >();
  private handler?: (opts: Record<string, unknown>) => void | Promise<void>;

  constructor(readonly name: string) {}

  command(nameAndArgs: string): CommanderCommandLike {
    const cmdName = nameAndArgs.trim().split(/\s+/)[0];
    const child = new MockCommanderNode(cmdName);
    this.children.set(cmdName, child);
    return child;
  }

  description(_str: string): CommanderCommandLike {
    return this;
  }

  option(
    flags: string,
    _description?: string,
    defaultValueOrParser?: string | boolean | string[] | ((value: string, previous: any) => any),
    defaultValue?: unknown,
  ): CommanderCommandLike {
    const optName = flagToCamelCase(flags);
    if (typeof defaultValueOrParser === "function") {
      this.registeredOptions.set(optName, {
        parser: defaultValueOrParser,
        defaultValue,
      });
    } else {
      this.registeredOptions.set(optName, {
        defaultValue: defaultValueOrParser,
      });
    }
    return this;
  }

  action(fn: (...args: any[]) => void | Promise<void>): CommanderCommandLike {
    this.handler = fn;
    return this;
  }

  async invoke(pathSegments: string[], opts: Record<string, unknown>): Promise<void> {
    if (pathSegments.length === 0) {
      if (!this.handler) {
        throw new Error(`CLI command '${this.name}' has no action handler`);
      }
      const resolvedOpts: Record<string, unknown> = {};
      for (const [key, spec] of this.registeredOptions.entries()) {
        if (spec.defaultValue !== undefined) {
          resolvedOpts[key] = Array.isArray(spec.defaultValue)
            ? [...spec.defaultValue]
            : spec.defaultValue;
        }
      }
      for (const [key, val] of Object.entries(opts)) {
        const spec = this.registeredOptions.get(key);
        if (!spec) {
          throw new Error(`Unknown CLI option '${key}' on command '${this.name}'`);
        }
        if (spec.parser && val !== undefined) {
          if (Array.isArray(val)) {
            resolvedOpts[key] = val.reduce(
              (acc, item) => spec.parser!(String(item), acc),
              spec.defaultValue !== undefined && Array.isArray(spec.defaultValue)
                ? [...spec.defaultValue]
                : spec.defaultValue,
            );
          } else {
            resolvedOpts[key] = spec.parser(
              String(val),
              spec.defaultValue !== undefined && Array.isArray(spec.defaultValue)
                ? [...spec.defaultValue]
                : spec.defaultValue,
            );
          }
        } else if (val !== undefined) {
          resolvedOpts[key] = val;
        }
      }
      await this.handler(resolvedOpts);
      return;
    }
    const [head, ...rest] = pathSegments;
    const child = this.children.get(head);
    if (!child) {
      throw new Error(`Unknown CLI subcommand '${head}' under '${this.name}'`);
    }
    await child.invoke(rest, opts);
  }
}

export class MockOpenClawApi implements OpenClawPluginApi {
  readonly id = "ebbmem";
  readonly pluginConfig: OpenClawPluginConfig;
  readonly logs: { info: string[]; warn: string[]; error: string[] } = {
    info: [],
    warn: [],
    error: [],
  };
  readonly logger: OpenClawLogger;

  readonly capabilities: MemoryCapabilityRegistration[] = [];
  readonly toolEntries: Array<{
    name: string;
    entry:
      | OpenClawToolDefinition<any>
      | ((ctx: OpenClawToolExecContext) => OpenClawToolDefinition<any>);
  }> = [];
  readonly hooks = new Map<string, Array<(event: any, ctx: OpenClawHookContext) => unknown>>();
  readonly cliRegistrars: Array<{
    registrar: (ctx: OpenClawCliContext) => void | Promise<void>;
    commands: string[];
    descriptors?: OpenClawCliDescriptor[];
  }> = [];
  readonly commands = new Map<string, OpenClawCommandDefinition>();

  constructor(pluginConfig: OpenClawPluginConfig = {}) {
    this.pluginConfig = pluginConfig;
    this.logger = {
      info: (m) => this.logs.info.push(m),
      warn: (m) => this.logs.warn.push(m),
      error: (m) => this.logs.error.push(m),
      debug: (m) => this.logs.info.push(m),
    };
  }

  registerMemoryCapability(cap: MemoryCapabilityRegistration): void {
    this.capabilities.push(cap);
  }

  registerTool(
    toolOrFactory:
      | OpenClawToolDefinition<any>
      | ((ctx: OpenClawToolExecContext) => OpenClawToolDefinition<any>),
    opts?: { name?: string },
  ): void {
    const resolvedName =
      opts?.name ?? (typeof toolOrFactory === "function" ? toolOrFactory({}).name : toolOrFactory.name);
    this.toolEntries.push({ name: resolvedName, entry: toolOrFactory });
  }

  on(
    event: string,
    handler: (event: any, ctx: OpenClawHookContext) => unknown,
  ): void {
    const list = this.hooks.get(event) ?? [];
    list.push(handler);
    this.hooks.set(event, list);
  }

  registerCli(
    registrar: (ctx: OpenClawCliContext) => void | Promise<void>,
    opts?: { commands: string[]; descriptors?: OpenClawCliDescriptor[] },
  ): void {
    this.cliRegistrars.push({
      registrar,
      commands: opts?.commands ?? [],
      descriptors: opts?.descriptors,
    });
  }

  registerCommand(def: OpenClawCommandDefinition): void {
    this.commands.set(def.name, def);
  }

  get registeredToolNames(): string[] {
    return this.toolEntries.map((t) => t.name);
  }

  getToolDefinition(name: string, ctx: OpenClawToolExecContext = {}): OpenClawToolDefinition<any> {
    const match = this.toolEntries.find((t) => t.name === name);
    if (!match) {
      throw new Error(`Tool not registered: ${name}`);
    }
    return typeof match.entry === "function" ? match.entry(ctx) : match.entry;
  }

  async invokeTool(
    name: string,
    params: Record<string, unknown>,
    ctx: OpenClawToolExecContext = {},
  ): Promise<OpenClawToolResult> {
    const def = this.getToolDefinition(name, ctx);
    return def.execute(`call-${name}-${Date.now()}`, params, ctx);
  }

  async dispatchHook(
    eventName: string,
    event: Record<string, unknown>,
    ctx: OpenClawHookContext = {},
  ): Promise<unknown[]> {
    const handlers = this.hooks.get(eventName) ?? [];
    const results: unknown[] = [];
    for (const h of handlers) {
      results.push(await h(event, ctx));
    }
    return results;
  }

  async runCli(
    pathSegments: string[],
    opts: Record<string, unknown> = {},
    configPath?: string,
  ): Promise<void> {
    const root = new MockCommanderNode("openclaw");
    for (const reg of this.cliRegistrars) {
      await reg.registrar({ program: root, configPath, logger: this.logger });
    }
    await root.invoke(pathSegments, opts);
  }

  async runSlashCommand(
    name: string,
    ctx: OpenClawCommandContext = {},
  ): Promise<OpenClawCommandResult> {
    const cmd = this.commands.get(name);
    if (!cmd) {
      throw new Error(`Slash command not registered: ${name}`);
    }
    return cmd.handler(ctx);
  }
}
