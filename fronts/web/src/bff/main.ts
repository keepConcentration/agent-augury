/** BFF command line: ``node --import tsx src/bff/main.ts`` (spawned by ``agent-augury --web``).
 *
 * The one-time token arrives as the first stdin line — never through env,
 * which the child and its ``run_command`` shells would inherit (SRS §3.5.1).
 * Without one (``npm start`` by hand) the BFF makes its own and runs in dev mode.
 *
 * Launch it with node directly, not ``npm`` (banner on stdout) or the ``tsx``
 * binary (a wrapper process: killing it can orphan the real BFF).
 */

import {startBff, type Bff} from "./bff.js";
import {newSecret} from "./http.js";
import {maskSensitive} from "./wire.js";

const TOKEN_RE = /^[A-Za-z0-9_-]{22,512}$/;
/** How long to wait for the token line before assuming dev mode ([가정]). */
const TOKEN_WAIT_MS = 3000;

/** First stdin line, or null on a TTY, empty stdin or no line in time. Stops reading after it. */
function readToken(): Promise<string | null> {
  if (process.stdin.isTTY) {
    return Promise.resolve(null);
  }
  return new Promise((resolve) => {
    let buf = "";
    const done = (value: string | null) => {
      clearTimeout(timer);
      process.stdin.off("data", onData);
      process.stdin.off("end", onEnd);
      process.stdin.pause();
      resolve(value);
    };
    const onData = (chunk: Buffer) => {
      buf += chunk.toString("utf8");
      const nl = buf.indexOf("\n");
      if (nl !== -1) {
        done(buf.slice(0, nl).trim() || null);
      } else if (buf.length > 1024) {
        done(null);
      }
    };
    const onEnd = () => done(buf.trim() || null);
    // A parent that holds stdin open without writing (IDE runner, container) must not hang us.
    const timer = setTimeout(() => done(null), TOKEN_WAIT_MS);
    process.stdin.on("data", onData);
    process.stdin.on("end", onEnd);
  });
}

const say = (line: string) => process.stderr.write(`augury web: ${line}\n`);

async function main(): Promise<void> {
  const given = await readToken();
  if (given !== null && !TOKEN_RE.test(given)) {
    say("the token on stdin is malformed");
    process.exit(2);
  }
  const devMode = given === null;
  if (devMode) {
    say("dev mode — not started through `agent-augury --web`; secret scrubbing is not guaranteed");
  }

  let bff: Bff;
  try {
    bff = await startBff({token: given ?? newSecret(), devMode});
  } catch (err) {
    say(maskSensitive(err instanceof Error ? err.message : String(err)));
    process.exit(1);
  }

  let exiting = false;
  const stop = (why: string, code: number) => {
    if (exiting) {
      process.exit(130); // a second Ctrl+C means "now"
    }
    exiting = true;
    say(`${why}, shutting down`);
    bff
      .shutdown()
      .then(() => process.exit(code))
      .catch(() => process.exit(1));
  };
  for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"] as const) {
    process.on(signal, () => stop(signal, 0));
  }
  process.on("uncaughtException", (err) => {
    say(`crashed: ${maskSensitive(err.message)}`);
    stop("uncaught exception", 1);
  });
  process.on("unhandledRejection", (err) => {
    say(`crashed: ${maskSensitive(err instanceof Error ? err.message : String(err))}`);
    stop("unhandled rejection", 1);
  });
  // Last resort on any exit path: the child has its own process group, so take it down.
  process.on("exit", () => {
    if (bff.gateway.status !== "exited" && bff.gateway.pid !== undefined) {
      try {
        process.kill(-bff.gateway.pid, "SIGKILL");
      } catch {
        // already gone
      }
    }
  });
  // If the CLI closed its end of stdout, nobody can read the URL: shut down.
  process.stdout.on("error", () => stop("stdout closed", 1));

  // The only stdout line: the CLI relays it to the user's terminal.
  process.stdout.write(`${bff.url}\n`);
  say("this link works once — open it in your browser now");
  say(`gateway pid ${bff.gateway.pid ?? "?"}`);
}

void main();
