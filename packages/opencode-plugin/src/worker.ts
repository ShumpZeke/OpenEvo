import { spawn, type ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";
import { randomUUID } from "node:crypto";

export type Pending = {
  resolve: (v: unknown) => void;
  reject: (e: Error) => void;
};

export class WorkerManager {
  private proc: ChildProcess | null = null;
  private pending = new Map<string, Pending>();
  private brainPending = new Map<string, Pending>();
  private health: { alive: boolean; version?: string; brain?: string } = { alive: false };
  private events: ((evt: { event: string; data: unknown }) => void)[] = [];
  private dir: string;
  private brainMode: "inherit" | "legacy" | "null";

  constructor(opts: { directory: string; brainMode?: "inherit" | "legacy" | "null" }) {
    this.dir = opts.directory;
    this.brainMode = opts.brainMode ?? "inherit";
  }

  onEvent(fn: (evt: { event: string; data: unknown }) => void) {
    this.events.push(fn);
  }

  isAlive() {
    return this.health.alive && this.proc !== null && this.proc.exitCode === null;
  }

  getInfo() {
    return { ...this.health, pid: this.proc?.pid ?? null, alive: this.isAlive() };
  }

  async start(): Promise<void> {
    if (this.isAlive()) return;
    await this.spawn();
  }

  private async spawn(): Promise<void> {
    const brainFlag = this.brainMode === "legacy" ? "legacy" : this.brainMode === "null" ? "null" : "stdio";
    const python = process.env.OPENEVO_PYTHON ?? "python";
    this.proc = spawn(python, ["-m", "oe_max.brain.worker", "--brain", brainFlag], {
      cwd: this.dir,
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
    });
    if (!this.proc.stdout || !this.proc.stdin || !this.proc.stderr) {
      throw new Error("worker stdio not available");
    }
    this.proc.stderr.on("data", (d) => {
      console.error(`[openevo-worker:stderr] ${d.toString().trim()}`);
    });
    this.proc.on("exit", (code, signal) => {
      this.health.alive = false;
      for (const [id, p] of this.pending) {
        p.reject(new Error(`worker exited code=${code} signal=${signal} (pending rpc ${id})`));
      }
      this.pending.clear();
      for (const [id, p] of this.brainPending) {
        p.reject(new Error(`worker exited while brain_request ${id} pending`));
      }
      this.brainPending.clear();
    });
    const rl = createInterface({ input: this.proc.stdout });
    const helloPromise = new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("worker hello timeout (10s)")), 10000);
      const onLine = (line: string) => {
        if (!line.trim()) return;
        let msg: Record<string, unknown>;
        try {
          msg = JSON.parse(line);
        } catch {
          return;
        }
        if (msg.type === "hello") {
          clearTimeout(timer);
          this.health = {
            alive: true,
            version: msg.worker_version as string | undefined,
            brain: msg.brain as string | undefined,
          };
          rl.off("line", onLine);
          rl.on("line", (l) => this.dispatchLine(l));
          resolve();
        }
      };
      rl.on("line", onLine);
    });
    await helloPromise;
  }

  private dispatchLine(line: string) {
    if (!line.trim()) return;
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(line);
    } catch (e) {
      console.error(`[openevo-worker] invalid json: ${line.slice(0, 200)} ${e}`);
      return;
    }
    const type = msg.type as string | undefined;
    if (type === "rpc_response") {
      const id = msg.id as string | undefined;
      if (!id) return;
      const p = this.pending.get(id);
      if (!p) return;
      this.pending.delete(id);
      if (msg.error) {
        const err = msg.error as { message?: string } | string;
        const m = typeof err === "string" ? err : (err.message ?? JSON.stringify(err));
        p.reject(new Error(m));
      } else {
        p.resolve(msg.result as unknown);
      }
      return;
    }
    if (type === "brain_request") {
      const id = msg.id as string | undefined;
      if (!id) return;
      if (this.brainHandler) {
        this.brainHandler(msg.request as unknown, id).catch((e) => {
          this.sendBrainResponse(id, {
            content: "",
            ok: false,
            error: String((e as Error)?.message ?? e),
          });
        });
      } else {
        this.sendBrainResponse(id, {
          content: "",
          ok: false,
          error: "no brain handler registered",
        });
      }
      return;
    }
    if (type === "event") {
      for (const fn of this.events) fn({ event: msg.event as string, data: msg.data });
      return;
    }
    if (type === "hello" || type === "error") {
      console.error(`[openevo-worker:${type}] ${line.slice(0, 500)}`);
      return;
    }
    console.error(`[openevo-worker] unknown message type=${type} ${line.slice(0, 500)}`);
  }

  private brainHandler: ((request: unknown, id: string) => Promise<void>) | null = null;

  onBrainRequest(handler: (request: unknown, id: string) => Promise<void>) {
    this.brainHandler = handler;
  }

  sendBrainResponse(id: string, response: unknown) {
    if (!this.proc?.stdin?.writable) return;
    const line = JSON.stringify({ type: "brain_response", id, response });
    this.proc.stdin.write(line + "\n");
  }

  async request(method: string, params: Record<string, unknown> = {}, timeoutMs = 30000): Promise<unknown> {
    if (!this.isAlive()) await this.start();
    const id = randomUUID();
    const line = JSON.stringify({ type: "rpc_request", id, method, params });
    const p = new Promise<unknown>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`rpc timeout ${method} (${timeoutMs}ms)`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (v) => {
          clearTimeout(timer);
          resolve(v);
        },
        reject: (e) => {
          clearTimeout(timer);
          reject(e);
        },
      });
    });
    if (!this.proc?.stdin?.writable) throw new Error("worker stdin not writable");
    this.proc.stdin.write(line + "\n");
    return await p;
  }

  async healthCheck(): Promise<boolean> {
    try {
      const res = (await this.request("brain/health", {}, 5000)) as { healthy?: boolean } | undefined;
      return !!res?.healthy;
    } catch {
      return this.isAlive();
    }
  }

  async shutdown(): Promise<void> {
    const p = this.proc;
    this.proc = null;
    this.health.alive = false;
    if (p && p.exitCode === null) {
      try {
        p.kill();
      } catch {}
      await new Promise((r) => setTimeout(r, 300));
      try {
        if (p.exitCode === null) p.kill("SIGKILL");
      } catch {}
    }
    for (const [, pending] of this.pending) pending.reject(new Error("worker shutdown"));
    this.pending.clear();
  }

  async restart(): Promise<void> {
    await this.shutdown();
    await this.spawn();
  }
}
