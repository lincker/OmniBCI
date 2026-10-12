import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomBytes, randomUUID } from "node:crypto";
import { EventEmitter } from "node:events";
import { createInterface } from "node:readline";

export interface ControlResult { ok: boolean; errors?: string[]; sessions?: Array<Record<string, unknown>>; forced?: boolean }
export interface BackendReady { url: string; instance_id: string }
const shutdownStages = {
  waiting_for_requests: "Waiting for active requests",
  waiting_for_configuration: "Waiting for configuration changes",
  stopping_stimulation: "Stopping stimulation",
  stopping_stimulation_process: "Stopping the stimulation process",
  stopping_actuation_process: "Stopping the actuation process",
  draining_recording: "Draining recorded results",
  stopping_engine: "Stopping the engine",
  stopped: "Backend stopped; waiting for exit",
} as const;

/** A child is owned only after this object has spawned it; never discover/kill by name. */
export class LocalBackend extends EventEmitter {
  readonly token = randomBytes(32).toString("hex");
  private readonly launchId = randomUUID();
  private child?: ChildProcessWithoutNullStreams;
  private pending = new Map<string, { resolve: (result: ControlResult) => void; reject: (error: Error) => void }>();
  private exited: Promise<void> = Promise.resolve();
  private phase?: keyof typeof shutdownStages;

  get shutdownStage(): string { return this.phase ? shutdownStages[this.phase] : "Waiting for backend acknowledgement"; }

  async start(python: string, cwd: string, packaged: boolean, log: (text: string) => void): Promise<BackendReady> {
    if (this.child) throw new Error("Local backend is already owned.");
    const env: NodeJS.ProcessEnv = { ...process.env, PYTHONUTF8: "1", PYTHONUNBUFFERED: "1" };
    // Do not let the user's shell redirect the bundled interpreter or its imports.
    for (const name of ["PYTHONPATH", "PYTHONHOME", "OMNIBCI_API_TOKEN"]) delete env[name];
    const child = spawn(python, [...(packaged ? ["-I"] : []), "-B", "-u", "-X", "utf8", "-m", "omnibci.presentation.server", "--desktop"], {
      cwd, env, detached: process.platform !== "win32", windowsHide: true, stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;
    this.exited = new Promise(resolve => child.once("close", () => resolve()));
    child.stderr.on("data", data => log(data.toString()));
    // Kill descendants as soon as the direct child exits, then wait for close
    // to consume final control replies already buffered in stdout.
    child.once("exit", () => {
      if (process.platform !== "win32" && child.pid) {
        try { process.kill(-child.pid, "SIGKILL"); } catch { /* Group already gone. */ }
      }
    });
    const ready = new Promise<BackendReady>((resolve, reject) => {
      let received = false;
      let broken = false;
      const timer = setTimeout(() => reject(new Error("Local backend startup exceeded 60 seconds.")), 60_000);
      const fail = (error: Error) => {
        clearTimeout(timer);
        if (!received) reject(error);
        for (const item of this.pending.values()) item.reject(error);
        this.pending.clear();
        if (received && !broken) {
          broken = true;
          this.emit("protocol-error", error);
          // EOF tells the private backend to run its normal shutdown cleanup.
          child.stdin.end();
        }
      };
      child.once("error", fail);
      child.once("close", (code, signal) => {
        const error = new Error(`Local backend exited (${code ?? signal ?? "unknown"}).`);
        clearTimeout(timer);
        if (!received) reject(error);
        for (const item of this.pending.values()) item.reject(error);
        this.pending.clear();
        this.child = undefined;
        this.emit("exit", error);
      });
      let buffered = 0;
      child.stdout.on("data", data => {
        buffered += data.length;
        if (buffered > 1024 * 1024) fail(new Error("Invalid backend control output."));
      });
      const lines = createInterface({ input: child.stdout });
      lines.on("line", line => {
        buffered = 0;
        if (broken) return;
        try {
          const message = JSON.parse(line);
          if (message.event === "ready") {
            if (received || message.launch_id !== this.launchId || typeof message.instance_id !== "string" || !message.instance_id) throw new Error("Backend identity mismatch.");
            const url = new URL(message.url);
            if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !url.port
              || url.username || url.password || url.pathname !== "/" || url.search || url.hash) throw new Error("Invalid local backend address.");
            received = true; clearTimeout(timer);
            resolve({ url: url.origin, instance_id: message.instance_id });
          } else if (message.event === "shutdown_progress") {
            if (typeof message.phase !== "string" || !Object.prototype.hasOwnProperty.call(shutdownStages, message.phase)) throw new Error("Invalid backend shutdown stage.");
            this.phase = message.phase as keyof typeof shutdownStages;
          } else if (message.event === "result" && typeof message.id === "string") {
            if (typeof message.ok !== "boolean" || message.errors !== undefined
              && (!Array.isArray(message.errors) || message.errors.some((item: unknown) => typeof item !== "string"))) throw new Error("Invalid backend control result.");
            const item = this.pending.get(message.id);
            if (item) { this.pending.delete(message.id); item.resolve(message); }
          } else if (message.event === "error") fail(new Error(String(message.message ?? "Backend startup failed.")));
          else throw new Error("Invalid backend control message.");
        } catch (error) { fail(error as Error); }
      });
    });
    child.stdin.on("error", error => {
      for (const item of this.pending.values()) item.reject(error);
      this.pending.clear();
    });
    child.stdin.write(JSON.stringify({ op: "bootstrap", token: this.token, launch_id: this.launchId }) + "\n");
    try { return await ready; }
    catch (error) { await this.force(); throw error; }
  }

  command(op: "prepare_shutdown" | "resume" | "shutdown"): Promise<ControlResult> {
    const child = this.child;
    if (!child || !child.stdin.writable) return Promise.reject(new Error("Local backend is not running."));
    if (op !== "shutdown") this.phase = undefined;
    const id = randomUUID();
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      child.stdin.write(JSON.stringify({ op, id }) + "\n", error => {
        if (error) { this.pending.delete(id); reject(error); }
      });
    });
  }

  waitForExit(): Promise<void> { return this.exited; }

  async force(): Promise<void> {
    const child = this.child;
    if (!child?.pid) return this.exited;
    try {
      if (process.platform === "win32") child.kill("SIGKILL"); // Backend owns a kill-on-close Windows Job.
      else process.kill(-child.pid, "SIGKILL");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
    }
    await this.exited;
  }
}
