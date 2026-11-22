import { ChildProcess, spawn, spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import * as readline from "node:readline";
import { fileURLToPath } from "node:url";

const CURRENT_DIR = path.dirname(fileURLToPath(import.meta.url));

const BRIDGE_PY = `
import json
import sys

conformance_src = sys.argv[1]
sys.path.insert(0, conformance_src)

from ebbmem_conformance.mock_server import MockEbbmemServer

srv = MockEbbmemServer(host="127.0.0.1", port=0)
srv.start()
print(json.dumps({"base_url": srv.base_url}), flush=True)

try:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        cmd = req.get("cmd")
        if cmd == "quit":
            print(json.dumps({"ok": True}), flush=True)
            break
        elif cmd == "set_exposure_logging":
            srv.set_exposure_logging(req["namespace"], bool(req["enabled"]))
            print(json.dumps({"ok": True}), flush=True)
        elif cmd == "get_memory_raw":
            item = srv.get_memory_raw(req["namespace"], req["memory_id"])
            if item is None:
                print(json.dumps({"item": None}), flush=True)
            else:
                print(json.dumps({"item": {
                    "memory_id": item.memory_id,
                    "namespace": item.namespace,
                    "content": item.content,
                    "tier": item.tier,
                    "tags": item.tags,
                    "storage_strength": item.storage_strength,
                    "recall_count": item.recall_count,
                    "reinforce_count": item.reinforce_count,
                    "tombstoned": item.tombstoned,
                }}), flush=True)
        elif cmd == "get_logged_requests":
            print(json.dumps({"logged_requests": list(srv.logged_requests)}), flush=True)
        else:
            print(json.dumps({"error": f"unknown cmd: {cmd}"}), flush=True)
finally:
    srv.stop()
`;

function resolvePythonExecutable(): string {
  const candidates: string[] = [];
  if (process.env.PYTHON && process.env.PYTHON.trim()) {
    candidates.push(process.env.PYTHON.trim());
  }
  if (process.platform === "win32") {
    candidates.push("python", "python3");
  } else {
    candidates.push("python3", "python");
  }

  for (const cmd of candidates) {
    const res = spawnSync(
      cmd,
      ["-c", "import sys; sys.stdout.write(str(sys.version_info[0]))"],
      { encoding: "utf8", timeout: 5000 },
    );
    if (res.status === 0 && res.stdout.trim() === "3") {
      return cmd;
    }
  }
  throw new Error(
    `Unable to locate a working Python 3 executable (probed: ${candidates.join(", ")})`,
  );
}

function findConformanceSrc(): string {
  for (const startDir of [CURRENT_DIR, process.cwd()]) {
    let current = path.resolve(startDir);
    while (true) {
      const candidateSrc = path.join(current, "conformance", "src");
      const marker = path.join(
        candidateSrc,
        "ebbmem_conformance",
        "mock_server.py",
      );
      if (fs.existsSync(marker)) {
        return candidateSrc;
      }
      const parent = path.dirname(current);
      if (parent === current) break;
      current = parent;
    }
  }
  throw new Error("Unable to locate conformance/src/ebbmem_conformance/mock_server.py");
}

export interface RawMemorySnapshot {
  memory_id: string;
  namespace: string;
  content: string;
  tier: string;
  tags: string[];
  storage_strength: number;
  recall_count: number;
  reinforce_count: number;
  tombstoned: boolean;
}

export class MockServerBridge {
  readonly baseUrl: string;
  private readonly proc: ChildProcess;
  private readonly lineQueue: string[] = [];
  private readonly waiters: Array<(line: string) => void> = [];
  private readonly rl: readline.Interface;
  private closed = false;

  private constructor(
    baseUrl: string,
    proc: ChildProcess,
    rl: readline.Interface,
  ) {
    this.baseUrl = baseUrl;
    this.proc = proc;
    this.rl = rl;
    rl.on("line", (line) => {
      const waiter = this.waiters.shift();
      if (waiter) {
        waiter(line);
      } else {
        this.lineQueue.push(line);
      }
    });
  }

  static async start(): Promise<MockServerBridge> {
    const pythonBin = resolvePythonExecutable();
    const conformanceSrc = findConformanceSrc();
    const proc = spawn(pythonBin, ["-u", "-c", BRIDGE_PY, conformanceSrc], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    const stderrChunks: string[] = [];
    proc.stderr.on("data", (chunk: Buffer | string) => {
      stderrChunks.push(chunk.toString());
    });
    const rl = readline.createInterface({ input: proc.stdout });
    const firstLine = await new Promise<string>((resolve, reject) => {
      const onLine = (line: string) => {
        rl.off("line", onLine);
        proc.off("exit", onExit);
        resolve(line);
      };
      const onExit = (code: number | null) => {
        rl.off("line", onLine);
        proc.off("exit", onExit);
        reject(
          new Error(
            `Mock bridge exited prematurely (code=${code}): ${stderrChunks.join("")}`,
          ),
        );
      };
      rl.once("line", onLine);
      proc.once("exit", onExit);
    });
    const parsed = JSON.parse(firstLine) as { base_url?: string };
    if (!parsed.base_url) {
      proc.kill();
      throw new Error(`Invalid startup payload from mock bridge: ${firstLine}`);
    }
    return new MockServerBridge(parsed.base_url, proc, rl);
  }

  private async rpc(
    payload: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    if (this.closed) {
      throw new Error("MockServerBridge is already closed");
    }
    this.proc.stdin?.write(`${JSON.stringify(payload)}\n`);
    const queued = this.lineQueue.shift();
    const line =
      queued ??
      (await new Promise<string>((resolve, reject) => {
        const timer = setTimeout(() => {
          reject(new Error("Timed out waiting for response from mock bridge"));
        }, 5000);
        this.waiters.push((val) => {
          clearTimeout(timer);
          resolve(val);
        });
      }));
    const parsed = JSON.parse(line) as Record<string, unknown>;
    if (parsed.error) {
      throw new Error(`Mock bridge error: ${String(parsed.error)}`);
    }
    return parsed;
  }

  async setExposureLogging(namespace: string, enabled: boolean): Promise<void> {
    await this.rpc({ cmd: "set_exposure_logging", namespace, enabled });
  }

  async getMemoryRaw(
    namespace: string,
    memoryId: string,
  ): Promise<RawMemorySnapshot | null> {
    const res = await this.rpc({
      cmd: "get_memory_raw",
      namespace,
      memory_id: memoryId,
    });
    return (res.item as RawMemorySnapshot | null) ?? null;
  }

  async getLoggedRequests(): Promise<
    Array<{ method: string; path: string; status: number; header_names: string[] }>
  > {
    const res = await this.rpc({ cmd: "get_logged_requests" });
    return (
      (res.logged_requests as Array<{
        method: string;
        path: string;
        status: number;
        header_names: string[];
      }>) ?? []
    );
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    try {
      this.proc.stdin?.write(`${JSON.stringify({ cmd: "quit" })}\n`);
      this.proc.stdin?.end();
    } catch {
      // Ignore shutdown write errors
    }
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => {
        this.proc.kill();
        resolve();
      }, 2000);
      this.proc.once("exit", () => {
        clearTimeout(timer);
        resolve();
      });
    });
    this.rl.close();
  }
}
