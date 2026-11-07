/** Assemble the BFF: HTTP server, SSE hub, state, command bus and Gateway child.
 *
 * Kept free of process-level concerns (stdin, signals, exit) so tests can run
 * whole BFFs side by side; ``main.ts`` is the thin CLI around it.
 */

import {existsSync, rmSync} from "node:fs";
import {join} from "node:path";
import {CommandBus} from "./cmd.js";
import {GatewayChild, childArgs, type Env} from "./gateway.js";
import {startServer} from "./http.js";
import {repoRoot} from "./paths.js";
import {EventHub} from "./sse.js";
import {BffState, handleChildExit, handleChildLine, type BffParts, type Flags} from "./state.js";
import {handleStderrLine} from "./stderr.js";
import {maskSensitive} from "./wire.js";

const isOn = (value: string | undefined) => value === "1" || value === "true";

/** API-03 ``flags``, from the env the CLI hands over (SRS §3.5.1). */
export function flagsFromEnv(env: Env): Flags {
  return {
    demo: isOn(env.AUGURY_DEMO),
    no_auto_start: isOn(env.AUGURY_NO_AUTO_START),
    plan_first: isOn(env.AUGURY_PLAN_FIRST),
    session: env.AGENT_AUGURY_SESSION || null,
  };
}

export type BffOptions = {
  token: string;
  /** True when not launched through ``agent-augury --web`` (N-003). */
  devMode: boolean;
  env?: Env;
  python?: string;
  /** Child argv override (tests). */
  args?: string[];
  staticRoot?: string;
  /** BFF log (stderr by default). */
  log?: (line: string) => void;
};

export type Bff = BffParts & {
  port: number;
  url: string;
  gateway: GatewayChild;
  /** quit → SIGTERM → SIGKILL the child, end every stream, close the server. Idempotent. */
  shutdown: () => Promise<void>;
};

export async function startBff(options: BffOptions): Promise<Bff> {
  const env = options.env ?? process.env;
  const log = options.log ?? ((line: string) => process.stderr.write(`${line}\n`));
  // Fail before listening if the child cannot be started as asked (e.g. --plan).
  const args = options.args ?? childArgs(env);

  let hub!: EventHub;
  let gateway: GatewayChild | undefined;
  let stopping: Promise<void> | undefined;
  const state = new BffState({
    broadcast: () => hub.broadcastState(),
    notice: (n) => hub.publishNotice(n),
    isConnected: (tab) => hub.connectedTabs().includes(tab),
  });
  hub = new EventHub({
    snapshot: (tab) => state.snapshot(tab),
    onConnect: (tab) => state.tabConnected(tab),
    onDisconnect: (tab) => state.tabDisconnected(tab),
  });
  const bus = new CommandBus({
    // Once shutdown starts, commands get 503 instead of racing the quit.
    sink: {
      alive: () => stopping === undefined && gateway !== undefined && gateway.status !== "exited",
      send: (m) => gateway?.send(m) ?? false,
    },
    state,
    hub,
    log,
  });
  const parts: BffParts = {hub, state, bus};

  const staticRoot = options.staticRoot ?? join(repoRoot, "fronts", "web", "dist");
  if (!existsSync(join(staticRoot, "index.html"))) {
    log("augury web: the UI is not built yet — run `npm run build` in fronts/web (the API still works)");
  }
  const server = await startServer({
    token: options.token,
    staticRoot,
    onError: (err) => log(`augury web: ${maskSensitive(err.message)}`),
    routes: [
      hub.route(),
      bus.route(),
      state.claimRoute(),
      state.sessionRoute({
        flags: flagsFromEnv(env),
        devMode: options.devMode,
        seqLast: () => hub.seqLast,
        streamsAvailable: () => hub.available(),
      }),
    ],
  });
  if (options.devMode) {
    hub.publishNotice({
      kind: "dev_mode",
      level: "warn",
      text: "dev mode: not started through agent-augury --web, so secret scrubbing is not guaranteed",
    });
  }

  try {
    gateway = new GatewayChild(
      {
        onLine: (line) => handleChildLine(line, parts),
        onStderr: (line) => handleStderrLine(line, {notice: (n) => hub.publishNotice(n), log}),
        // The BFF stays up after the child exits so the tabs can show why (N-007).
        onExit: (code, signal) => handleChildExit(code, signal, parts),
        onError: (err) => log(`augury web: gateway: ${maskSensitive(err.message)}`),
      },
      {env, python: options.python, args},
    );
  } catch (err) {
    hub.close();
    await server.close();
    throw err;
  }
  const child = gateway;

  // Every step runs even if an earlier one throws, so the port and streams never leak.
  const shutdown = () =>
    (stopping ??= (async () => {
      try {
        await child.stop();
      } finally {
        // The child deletes the secrets file once loaded; if it never got that
        // far (the CLI was killed early), do not leave API keys in the temp dir.
        // A failure here (EACCES, ...) must not skip the rest of the cleanup.
        if (env.AUGURY_GATEWAY_SECRETS_FILE) {
          try {
            rmSync(env.AUGURY_GATEWAY_SECRETS_FILE, {force: true});
          } catch (err) {
            log(`augury web: could not remove the secrets file: ${maskSensitive(err instanceof Error ? err.message : String(err))}`);
          }
        }
        state.close();
        hub.close();
        await server.close();
      }
    })());

  return {...parts, port: server.port, url: server.url, gateway: child, shutdown};
}
