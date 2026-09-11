/** Spawn Python Gateway hello_demo and speak Wire JSONL over stdio. */

import {spawn, type ChildProcessWithoutNullStreams} from "node:child_process";
import {createInterface} from "node:readline";
import {existsSync} from "node:fs";
import {dirname, join, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {decodeLine, encodeLine, type WireMessage} from "./wire.js";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../../..");

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

export class GatewayChild {
  private child: ChildProcessWithoutNullStreams;
  private closed = false;

  constructor(handlers: WireHandlers, python = resolvePython()) {
    this.child = spawn(
      python,
      ["-m", "agent_augury.gateway.hello_demo"],
      {
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
      try {
        handlers.onMessage(decodeLine(line));
      } catch (err) {
        handlers.onError(err instanceof Error ? err : new Error(String(err)));
      }
    });

    this.child.stderr.on("data", (buf: Buffer) => {
      const text = buf.toString("utf8").trim();
      if (text) {
        handlers.onError(new Error(`[gateway stderr] ${text}`));
      }
    });

    this.child.on("error", (err) => handlers.onError(err));
    this.child.on("exit", (code) => {
      this.closed = true;
      handlers.onExit(code);
    });
  }

  send(message: WireMessage): void {
    if (this.closed || !this.child.stdin.writable) {
      return;
    }
    this.child.stdin.write(encodeLine(message) + "\n");
  }

  kill(): void {
    if (!this.closed) {
      this.child.kill();
    }
  }
}
