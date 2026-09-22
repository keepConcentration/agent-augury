/** Spawn Python Gateway child and speak Wire JSONL over stdio. */

import {spawn, type ChildProcessWithoutNullStreams} from "node:child_process";
import {createInterface} from "node:readline";
import {appendFileSync, existsSync} from "node:fs";
import {dirname, join, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {decodeLine, encodeLine, maskSensitive, type WireMessage} from "./wire.js";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../../..");

/** Where to dump the Wire traffic, or null when debugging is off.
 *
 * ``AUGURY_INK_DEBUG=1`` writes ``.augury-ink-debug.log``; any other value is
 * taken as the path itself. The default directory is the launch directory the
 * CLI hands us in ``AUGURY_FILE_ROOT`` — our own cwd is the Ink package dir,
 * which for a wheel install is the user cache and nobody will ever look there.
 */
export function debugLogPath(): string | null {
  const flag = process.env.AUGURY_INK_DEBUG;
  if (!flag || flag === "0" || flag === "false") {
    return null;
  }
  if (flag !== "1" && flag !== "true") {
    return flag;
  }
  return join(process.env.AUGURY_FILE_ROOT || process.cwd(), ".augury-ink-debug.log");
}

export type WireHandlers = {
  onMessage: (msg: WireMessage) => void;
  onExit: (code: number | null) => void;
  onError: (err: Error) => void;
};

export function resolvePython(): string {
  if (process.env.AUGURY_PYTHON) {
    return process.env.AUGURY_PYTHON;
  }
  const win = process.platform === "win32";
  const candidates = [
    join(repoRoot, ".venv", win ? "Scripts/python.exe" : "bin/python"),
    join(repoRoot, "venv", win ? "Scripts/python.exe" : "bin/python"),
  ];
  for (const c of candidates) {
    if (existsSync(c)) {
      return c;
    }
  }
  return win ? "python" : "python3";
}

/** M2 hello_demo vs M7 real session_stdio (AUGURY_CONFIG). */
export function resolveGatewayArgs(): string[] {
  const mode = (process.env.AUGURY_GATEWAY_MODE || "hello").toLowerCase();
  if (mode === "session") {
    const config = process.env.AUGURY_CONFIG;
    if (!config) {
      throw new Error("AUGURY_GATEWAY_MODE=session requires AUGURY_CONFIG");
    }
    const args = ["-m", "agent_augury.gateway.session_stdio", "--config", config];
    if (process.env.AUGURY_DEMO === "1" || process.env.AUGURY_DEMO === "true") {
      args.push("--demo");
    }
    if (process.env.AUGURY_QUIET === "1" || process.env.AUGURY_QUIET === "true") {
      args.push("--quiet");
    }
    if (
      process.env.AUGURY_NO_AUTO_START === "1" ||
      process.env.AUGURY_NO_AUTO_START === "true"
    ) {
      args.push("--no-auto-start");
    }
    if (
      process.env.AGENT_AUGURY_NEW_SESSION === "1" ||
      process.env.AGENT_AUGURY_NEW_SESSION === "true"
    ) {
      args.push("--new-session");
    }
    const sessionId = process.env.AGENT_AUGURY_SESSION;
    if (sessionId) {
      args.push("--session", sessionId);
    }
    return args;
  }
  return ["-m", "agent_augury.gateway.hello_demo"];
}

export class GatewayChild {
  private child: ChildProcessWithoutNullStreams;
  private closed = false;
  private handlers: WireHandlers;
  private debugPath = debugLogPath();

  constructor(handlers: WireHandlers, python = resolvePython()) {
    this.handlers = handlers;
    this.child = spawn(python, resolveGatewayArgs(), {
        cwd: repoRoot,
        env: {
          ...process.env,
          PYTHONUTF8: "1",
          PYTHONIOENCODING: "utf-8",
          PYTHONPATH: [join(repoRoot, "src"), process.env.PYTHONPATH]
            .filter(Boolean)
            .join(process.platform === "win32" ? ";" : ":"),
        },
        stdio: ["pipe", "pipe", "pipe"],
      },
    );

    const rl = createInterface({input: this.child.stdout});
    rl.on("line", (line) => {
      this.trace("<<", line);
      try {
        handlers.onMessage(decodeLine(line));
      } catch (err) {
        handlers.onError(err instanceof Error ? err : new Error(String(err)));
      }
    });

    this.child.stderr.on("data", (buf: Buffer) => {
      const text = buf.toString("utf8").trim();
      if (!text) {
        return;
      }
      // Drop known non-fatal noise (policy hints, OAuth helpers) — do not
      // surface in the Ink log. Real gateway failures still go to onError.
      if (
        text.includes("tools.shell.enabled=true with empty") ||
        text.includes("To authenticate, enter code:") ||
        text.includes("Verification URL:")
      ) {
        return;
      }
      handlers.onError(new Error(`[gateway stderr] ${text}`));
    });

    this.child.on("error", (err) => handlers.onError(err));
    this.child.on("exit", (code) => {
      this.closed = true;
      handlers.onExit(code);
    });
  }

  /** Append one masked Wire line to the debug log; never throws. */
  private trace(dir: string, line: string): void {
    if (!this.debugPath) {
      return;
    }
    try {
      appendFileSync(
        this.debugPath,
        `${new Date().toISOString()} ${dir} ${maskSensitive(line)}\n`,
      );
    } catch {
      // Debugging must never take the session down with it.
    }
  }

  /** Send a command. Returns false when it could not leave this process.
   *
   * A silent drop is the worst outcome: the human types a message, sees their
   * own echo, and waits forever for agents that never got it. Report it.
   */
  send(message: WireMessage): boolean {
    const line = encodeLine(message);
    this.trace(">>", line);
    if (this.closed || !this.child.stdin.writable) {
      this.handlers.onError(
        new Error(`! gateway is gone — dropped ${message.type}`),
      );
      return false;
    }
    try {
      this.child.stdin.write(line + "\n");
      return true;
    } catch (err) {
      this.handlers.onError(
        new Error(
          `! could not reach gateway — dropped ${message.type}: ${
            err instanceof Error ? err.message : String(err)
          }`,
        ),
      );
      return false;
    }
  }

  kill(): void {
    if (!this.closed) {
      this.child.kill();
    }
  }
}
