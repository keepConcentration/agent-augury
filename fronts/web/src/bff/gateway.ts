/** Spawn and supervise the Python Gateway child for the BFF (WBS 2.2).
 *
 * Same topology as Ink (``fronts/ink/src/gateway.ts``) with two deliberate
 * differences from SRS §3.5.2: the child env is an allowlist instead of
 * ``...process.env``, and shutdown escalates quit → SIGTERM → SIGKILL so no
 * Python process outlives the BFF (N-007).
 */

import {spawn, type ChildProcessWithoutNullStreams} from "node:child_process";
import {existsSync, readFileSync} from "node:fs";
import {join} from "node:path";
import {repoRoot} from "./paths.js";
import {encodeLine, lineSplitter, parseChildLine, type ChildLine, type WireMessage} from "./wire.js";

export type Env = Record<string, string | undefined>;

/** Grace period between shutdown steps (08 §6.4, [가정]). */
export const STOP_GRACE_MS = 3000;

function isOn(value: string | undefined): boolean {
  return value === "1" || value === "true";
}

/** Python interpreter: ``AUGURY_PYTHON`` → repo ``.venv``/``venv`` → PATH. */
export function resolvePython(env: Env, exists: (path: string) => boolean = existsSync): string {
  if (env.AUGURY_PYTHON) {
    return env.AUGURY_PYTHON;
  }
  const win = process.platform === "win32";
  for (const dir of [".venv", "venv"]) {
    const candidate = join(repoRoot, dir, win ? "Scripts/python.exe" : "bin/python");
    if (exists(candidate)) {
      return candidate;
    }
  }
  return win ? "python" : "python3";
}

/** Child argv: hello demo by default, ``session_stdio`` when the CLI asks for it. */
export function childArgs(env: Env): string[] {
  const mode = (env.AUGURY_GATEWAY_MODE || "hello").toLowerCase();
  if (mode !== "session") {
    return ["-m", "agent_augury.gateway.hello_demo"];
  }
  if (!env.AUGURY_CONFIG) {
    throw new Error("AUGURY_GATEWAY_MODE=session requires AUGURY_CONFIG");
  }
  // Team planner needs a temp copy of the YAML and planning-aware UI (WBS 3.10).
  if (isOn(env.AUGURY_PLAN_FIRST)) {
    throw new Error("--plan is not supported by the Web Surface yet (Phase 2, W-007)");
  }
  const args = ["-m", "agent_augury.gateway.session_stdio", "--config", env.AUGURY_CONFIG];
  if (isOn(env.AUGURY_QUIET)) {
    args.push("--quiet");
  }
  if (isOn(env.AUGURY_NO_AUTO_START)) {
    args.push("--no-auto-start");
  }
  if (isOn(env.AGENT_AUGURY_NEW_SESSION)) {
    args.push("--new-session");
  }
  if (env.AGENT_AUGURY_SESSION) {
    args.push("--session", env.AGENT_AUGURY_SESSION);
  }
  return args;
}

// What the child needs to run, reach the network and find its own state.
const PASS_THROUGH = new Set([
  "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TZ", "TERM",
  "TMPDIR", "TMP", "TEMP",
  "SystemRoot", "SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
  "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
  "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
  "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
  "AUGURY_PYTHON", "AUGURY_PROJECT_ROOT", "AUGURY_FILE_ROOT", "AUGURY_GATEWAY_MODE",
  "AUGURY_CONFIG", "AUGURY_QUIET", "AUGURY_NO_AUTO_START", "AUGURY_PLAN_FIRST",
  "AGENT_AUGURY_NEW_SESSION", "AGENT_AUGURY_SESSION", "AUGURY_GATEWAY_SECRETS_FILE", "AUGURY_CACHE_DIR",
]);

// Mirror of gateway/secrets.py _SECRET_NAME_RE: such names reach the child only
// through AUGURY_GATEWAY_SECRETS_FILE, never through its env.
const SECRET_NAME_RE = /(?:^|.+_)(API_KEY|ACCESS_TOKEN|AUTH_TOKEN|BOT_TOKEN|SECRET|PASSWORD|PRIVATE_KEY|WEBHOOK_URL|WEBHOOK)$/i;

/** ``${NAME}`` references in the YAML; ``config.py`` expands them from the child env. */
export function configEnvNames(configText: string): string[] {
  return [...configText.matchAll(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g)].map((m) => m[1]);
}

/** Child env built from an allowlist (SRS §3.5.2, N-003).
 *
 * A ``${NAME}`` the YAML references passes unless it looks like a secret — the
 * same rule as Python's ``is_secret_env_name``, so e.g. ``GITHUB_TOKEN`` does
 * pass while ``OPENAI_API_KEY`` only ever arrives through the secrets file.
 * Allowlisted names held back for that reason are pushed onto ``dropped``
 * (names only, never values) so the caller can say why a ``${NAME}`` is empty.
 */
export function childEnv(env: Env, configText = "", dropped?: string[]): Record<string, string> {
  // Windows env names are case-insensitive (``Path``); compare them uppercased.
  const fold = (name: string) => (process.platform === "win32" ? name.toUpperCase() : name);
  const allowed = new Set([...PASS_THROUGH].map(fold));
  for (const name of configEnvNames(configText)) {
    allowed.add(fold(name));
  }
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(env)) {
    if (value === undefined) {
      continue;
    }
    if (allowed.has(fold(key)) || fold(key).startsWith("LC_")) {
      if (SECRET_NAME_RE.test(key)) {
        dropped?.push(key);
      } else {
        out[key] = value;
      }
    }
  }
  out.PYTHONUTF8 = "1";
  out.PYTHONIOENCODING = "utf-8";
  out.PYTHONPATH = [join(repoRoot, "src"), env.PYTHONPATH]
    .filter(Boolean)
    .join(process.platform === "win32" ? ";" : ":");
  return out;
}

export type GatewayStatus = "starting" | "running" | "exited";

export type GatewayHandlers = {
  onLine: (line: ChildLine) => void;
  onStderr: (line: string) => void;
  onExit: (code: number | null, signal: NodeJS.Signals | null) => void;
  onError: (err: Error) => void;
};

export type GatewayOptions = {
  env?: Env;
  python?: string;
  /** Override the child argv (tests). */
  args?: string[];
};

/** How long to wait for stdout to drain after ``exit`` before giving up on ``close`` ([가정]). */
const CLOSE_AFTER_EXIT_MS = 1000;

const posix = process.platform !== "win32";

export class GatewayChild {
  status: GatewayStatus = "starting";
  exitCode: number | null = null;
  signal: NodeJS.Signals | null = null;
  readonly pid: number | undefined;
  private child: ChildProcessWithoutNullStreams;
  private handlers: GatewayHandlers;
  private exited: Promise<void>;
  private markExited!: () => void;
  /** Resolves on the child's ``exit`` — possibly before ``exited`` (pipes still open). */
  private gone: Promise<void>;
  private markGone!: () => void;
  private stopping: Promise<boolean> | undefined;
  /** When the child's ``exit`` fired; after it the group id may be reused. */
  private exitAt: number | undefined;
  private droppedLines = 0;
  private reported = new WeakSet<Error>();
  private onProcessExit: (() => void) | undefined;

  constructor(handlers: GatewayHandlers, options: GatewayOptions = {}) {
    this.handlers = handlers;
    const env = options.env ?? process.env;
    const args = options.args ?? childArgs(env);
    let configText = "";
    if (env.AUGURY_CONFIG) {
      try {
        configText = readFileSync(env.AUGURY_CONFIG, "utf8");
      } catch (err) {
        // The child reports the config error itself; say why ${VAR}s may be missing.
        this.report(new Error(`could not read AUGURY_CONFIG for the env allowlist: ${String(err)}`));
      }
    }
    this.exited = new Promise((resolve) => {
      this.markExited = resolve;
    });
    this.gone = new Promise((resolve) => {
      this.markGone = resolve;
    });
    const dropped: string[] = [];
    const childEnvironment = childEnv(env, configText, dropped);
    if (dropped.length > 0) {
      this.report(
        new Error(
          `not forwarded to the gateway env (secret-looking names arrive only via ` +
            `AUGURY_GATEWAY_SECRETS_FILE): ${dropped.join(", ")}`,
        ),
      );
    }
    this.child = spawn(options.python ?? resolvePython(env), args, {
      cwd: repoRoot,
      env: childEnvironment,
      stdio: ["pipe", "pipe", "pipe"],
      // Own process group, so stop() also reaches run_command grandchildren (N-007).
      detached: posix,
    });
    this.pid = this.child.pid;
    if (posix && this.pid !== undefined) {
      // A BFF that dies without stop() must not orphan the detached group (N-007).
      // Only synchronous work runs in an ``exit`` listener.
      this.onProcessExit = () => this.kill("SIGTERM", STOP_GRACE_MS);
      process.on("exit", this.onProcessExit);
    }

    const stdout = lineSplitter(
      (line) => {
        if (this.status === "exited") {
          // Arrived after the close fallback gave up on the pipe (see ``exit`` below).
          this.droppedLines++;
          return;
        }
        this.status = "running";
        this.guard(() => handlers.onLine(parseChildLine(line)));
      },
      (bytes) =>
        this.guard(() => handlers.onLine({kind: "decode_error", error: `line too long (${bytes} bytes)`, oversize: true})),
    );
    this.child.stdout.on("data", stdout.write);
    this.child.stdout.on("end", () => {
      stdout.end();
      if (this.droppedLines > 0) {
        this.report(new Error(`dropped ${this.droppedLines} stdout line(s) that arrived after the child exited`));
      }
    });
    const stderr = lineSplitter(
      (line) => this.guard(() => handlers.onStderr(line)),
      (bytes) => this.report(new Error(`dropped an oversized stderr line (${bytes} bytes)`)),
    );
    this.child.stderr.on("data", stderr.write);
    this.child.stderr.on("end", stderr.end);
    // EPIPE when the child dies mid-write must not take the BFF down (SRS API-05 6).
    this.child.stdin.on("error", (err) => this.report(err));
    this.child.on("error", (err) => {
      this.report(err);
      if (this.child.pid === undefined) {
        this.finish(null, null);
      }
    });
    // ``exit`` can fire before stdout is drained; ``close`` comes after the last
    // line, so the final events (session.ended) reach onLine before onExit.
    // A grandchild holding the pipe would delay ``close`` forever — hence the fallback.
    this.child.on("close", (code, signal) => this.finish(code, signal));
    this.child.on("exit", (code, signal) => {
      this.exitAt = Date.now();
      this.markGone();
      setTimeout(() => this.finish(code, signal), CLOSE_AFTER_EXIT_MS).unref();
    });
  }

  /** Handler errors stay inside: a throwing listener must not crash the BFF. */
  private guard(fn: () => void): void {
    try {
      fn();
    } catch (err) {
      this.report(err instanceof Error ? err : new Error(String(err)));
    }
  }

  private report(err: Error): void {
    // A failed write surfaces both in its callback and as stdin ``error``.
    if (this.reported.has(err)) {
      return;
    }
    this.reported.add(err);
    try {
      this.handlers.onError(err);
    } catch {
      // Nothing left to tell.
    }
  }

  private finish(code: number | null, signal: NodeJS.Signals | null): void {
    if (this.status === "exited") {
      return;
    }
    this.status = "exited";
    this.exitAt ??= Date.now();
    this.markGone();
    this.exitCode = code;
    this.signal = signal;
    this.markExited();
    this.guard(() => this.handlers.onExit(code, signal));
    // Past this window the exit hook could only hit a reused group id.
    setTimeout(() => this.dispose(), STOP_GRACE_MS).unref();
  }

  /** Drop the process ``exit`` hook; call when the instance is discarded. */
  dispose(): void {
    if (this.onProcessExit) {
      process.off("exit", this.onProcessExit);
      this.onProcessExit = undefined;
    }
  }

  /** Queue one command line on the child's stdin.
   *
   * False means it never left this process (caller → 503). True only means the
   * line was queued for writing, not that the child received it; a write that
   * fails later is reported through ``onError``.
   */
  send(message: WireMessage): boolean {
    if (this.status === "exited" || !this.child.stdin.writable) {
      return false;
    }
    try {
      this.child.stdin.write(encodeLine(message) + "\n", (err) => {
        if (err) {
          this.report(err);
        }
      });
      return true;
    } catch {
      return false;
    }
  }

  private waitFor(event: Promise<void>, ms: number): Promise<boolean> {
    let timer: NodeJS.Timeout | undefined;
    const timeout = new Promise<boolean>((resolve) => {
      timer = setTimeout(() => resolve(false), ms);
    });
    return Promise.race([event.then(() => true), timeout]).finally(() => clearTimeout(timer));
  }

  /** True until ``windowMs`` after the child's exit: past that the group id may be reused. */
  private groupInReach(windowMs: number): boolean {
    return this.exitAt === undefined || Date.now() - this.exitAt <= windowMs;
  }

  /** Whether anything is left in the child's process group (POSIX). */
  private groupAlive(): boolean {
    if (!posix || this.pid === undefined) {
      return false;
    }
    try {
      process.kill(-this.pid, 0);
      return true;
    } catch {
      return false;
    }
  }

  private signalGroup(signal: NodeJS.Signals): void {
    try {
      if (posix && this.pid !== undefined) {
        process.kill(-this.pid, signal);
      } else {
        this.child.kill(signal);
      }
    } catch {
      // ESRCH: nothing left in the group.
    }
  }

  /** Signal the child's whole process group (POSIX), but only while its id is still ours. */
  private kill(signal: NodeJS.Signals, windowMs: number): void {
    if (this.groupInReach(windowMs)) {
      this.signalGroup(signal);
    }
  }

  /** After the child exited: SIGTERM what is left of its group, then SIGKILL after the grace.
   *
   * The window is checked once up front. While the group still has members its
   * id cannot be reused, so the later signals are guarded by ``groupAlive``.
   */
  private async reapGroup(graceMs: number): Promise<void> {
    if (!posix || this.pid === undefined || !this.groupInReach(graceMs) || !this.groupAlive()) {
      return;
    }
    this.signalGroup("SIGTERM");
    const end = Date.now() + graceMs;
    while (this.groupAlive() && Date.now() < end) {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    if (this.groupAlive()) {
      this.signalGroup("SIGKILL");
    }
  }

  /** Ask the child to quit, then escalate to SIGTERM and SIGKILL (SRS §3.5.2).
   *
   * Every caller gets the same run. Resolves true once the child has exited;
   * false means even SIGKILL did not take within the grace period.
   */
  stop(graceMs = STOP_GRACE_MS): Promise<boolean> {
    this.stopping ??= this.runStop(graceMs);
    return this.stopping;
  }

  private async runStop(graceMs: number): Promise<boolean> {
    if (this.status !== "exited") {
      this.send({dir: "cmd", type: "session.quit", id: "web-shutdown"});
    }
    // Escalate on the child's own exit: a grandchild holding the pipes delays
    // ``close``, and the group must be reaped while its id is still ours.
    let gone = await this.waitFor(this.gone, graceMs);
    if (!gone) {
      this.kill("SIGTERM", graceMs);
      gone = await this.waitFor(this.gone, graceMs);
    }
    if (!gone) {
      this.kill("SIGKILL", graceMs);
      gone = await this.waitFor(this.gone, graceMs);
    }
    if (!gone) {
      return false;
    }
    // The child may be gone while its run_command children still hold on.
    await this.reapGroup(graceMs);
    this.dispose();
    // ``close`` or the fallback timer settles ``exited`` within CLOSE_AFTER_EXIT_MS.
    return this.waitFor(this.exited, CLOSE_AFTER_EXIT_MS + graceMs);
  }
}
