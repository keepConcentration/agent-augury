import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {existsSync, mkdtempSync, writeFileSync} from "node:fs";
import {request as httpRequest, type IncomingMessage} from "node:http";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {test} from "node:test";
import {flagsFromEnv, startBff, type Bff} from "./bff.js";
import {resolvePython} from "./gateway.js";
import {repoRoot} from "./paths.js";

const TOKEN = "m".repeat(43);
const TAB = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const python = resolvePython(process.env);
const hasVenv = existsSync(python);

function alive(pid: number | undefined): boolean {
  if (pid === undefined) {
    return false;
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

async function until(cond: () => boolean, ms = 10_000): Promise<void> {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) {
      throw new Error("timed out");
    }
    await new Promise((r) => setTimeout(r, 20));
  }
}

/** Exchange the token and open one SSE stream; collects bff.state / bff.notice. */
async function connect(port: number, token: string) {
  const origin = `http://127.0.0.1:${port}`;
  const cookie = await new Promise<string>((resolve, reject) => {
    const req = httpRequest(
      {host: "127.0.0.1", port, method: "POST", path: "/api/auth/exchange", headers: {"Content-Type": "application/json", Origin: origin}},
      (res) => {
        res.resume();
        res.statusCode === 204 ? resolve(String(res.headers["set-cookie"]).split(";")[0]) : reject(new Error(`exchange ${res.statusCode}`));
      },
    );
    req.on("error", reject);
    req.end(JSON.stringify({token}));
  });
  const frames: Array<{event?: string; data?: string}> = [];
  const res = await new Promise<IncomingMessage>((resolve, reject) => {
    const req = httpRequest({host: "127.0.0.1", port, path: `/api/events?tab=${TAB}`, headers: {Cookie: cookie}}, resolve);
    req.on("error", reject);
    req.end();
  });
  let buf = "";
  res.setEncoding("utf8");
  res.on("data", (chunk: string) => {
    buf += chunk;
    let i: number;
    while ((i = buf.indexOf("\n\n")) !== -1) {
      const f: Record<string, string> = {};
      for (const line of buf.slice(0, i).split("\n")) {
        const [k, ...v] = line.split(": ");
        f[k] = v.join(": ");
      }
      buf = buf.slice(i + 2);
      frames.push(f);
    }
  });
  res.on("error", () => {});
  const of = (event: string) => frames.filter((f) => f.event === event).map((f) => JSON.parse(f.data!));
  return {cookie, res, states: () => of("bff.state"), notices: () => of("bff.notice")};
}

test("flagsFromEnv mirrors the CLI env (API-03 flags)", () => {
  assert.deepEqual(flagsFromEnv({AUGURY_DEMO: "1", AUGURY_NO_AUTO_START: "true", AGENT_AUGURY_SESSION: "s1"}), {
    no_auto_start: true, plan_first: false, session: "s1",
  });
  assert.deepEqual(flagsFromEnv({}), {no_auto_start: false, plan_first: false, session: null});
});

test("an unstartable child (team planner) fails before the server listens", async () => {
  await assert.rejects(
    startBff({token: TOKEN, devMode: false, env: {AUGURY_GATEWAY_MODE: "session", AUGURY_CONFIG: "/c.yaml", AUGURY_PLAN_FIRST: "1"}, log: () => {}}),
    /Phase 2/,
  );
});

test("startBff with hello_demo: a tab sees the session; shutdown leaves nothing behind", {skip: !hasVenv && "no repo .venv"}, async () => {
  const logs: string[] = [];
  const bff: Bff = await startBff({token: TOKEN, devMode: true, env: {PATH: process.env.PATH}, python, log: (l) => logs.push(l)});
  assert.equal(bff.url, `http://127.0.0.1:${bff.port}/#token=${TOKEN}`);
  const tab = await connect(bff.port, TOKEN);
  await until(() => tab.states().at(-1)?.pending_questions?.length === 1);
  const last = tab.states().at(-1)!;
  assert.deepEqual([last.gateway.status, last.agents, last.is_controller], ["running", ["demo-agent"], true]);
  assert.equal(tab.notices()[0].kind, "dev_mode");
  // The notice passes publishNotice's masking verbatim ("secret scrubbing" is prose, not a pair).
  assert.equal(tab.notices()[0].text, "dev mode: not started through agent-augury --web, so secret scrubbing is not guaranteed");

  const pid = bff.gateway.pid;
  await Promise.all([bff.shutdown(), bff.shutdown()]);
  assert.equal(alive(pid), false);
  await until(() => tab.res.destroyed || tab.res.complete);
});

test("child stderr reaches the tabs as notices and the BFF log masked", {skip: !hasVenv && "no repo .venv"}, async () => {
  const logs: string[] = [];
  const script = [
    "import sys, time",
    "print('To authenticate, enter code: WXYZ-1234', file=sys.stderr, flush=True)",
    "print('boom Authorization: Bearer sk-secret-1', file=sys.stderr, flush=True)",
    "print('tools.shell.enabled=true with empty allowed list', file=sys.stderr, flush=True)",
    "time.sleep(30)",
  ].join("\n");
  const bff = await startBff({token: TOKEN, devMode: false, env: {PATH: process.env.PATH}, python, args: ["-c", script], log: (l) => logs.push(l)});
  const tab = await connect(bff.port, TOKEN);
  await until(() => tab.notices().length === 2);
  assert.deepEqual(tab.notices().map((n) => [n.kind, n.level]), [["auth", "info"], ["stderr", "warn"]]);
  assert.equal(tab.notices()[0].text, "To authenticate, enter code: WXYZ-1234");
  assert.doesNotMatch(JSON.stringify(tab.notices()), /sk-secret-1/);
  await until(() => logs.filter((l) => l.startsWith("[gateway]")).length === 3);
  assert.doesNotMatch(logs.join("\n"), /sk-secret-1/);
  await bff.shutdown();
});

test("the BFF outlives its child: exit is shown, not fatal (N-007)", {skip: !hasVenv && "no repo .venv"}, async () => {
  const bff = await startBff({token: TOKEN, devMode: false, env: {PATH: process.env.PATH}, python, args: ["-c", "import sys; sys.exit(3)"], log: () => {}});
  const tab = await connect(bff.port, TOKEN);
  await until(() => tab.states().at(-1)?.gateway?.status === "exited");
  assert.equal(tab.states().at(-1)!.gateway.exit_code, 3);
  assert.equal(tab.res.destroyed, false, "the stream stays open");
  await bff.shutdown();
});

// ---- main.ts as a real process --------------------------------------------------------

const mainTs = join(repoRoot, "fronts", "web", "src", "bff", "main.ts");

/** Launch exactly as the CLI should: one node process, no npm, no tsx wrapper. */
function runMain(stdin: string | null, keepStdinOpen = false, extraEnv: Record<string, string> = {}) {
  const child = spawn(process.execPath, ["--import", "tsx", mainTs], {
    cwd: join(repoRoot, "fronts", "web"),
    env: {PATH: process.env.PATH, ...extraEnv},
    stdio: ["pipe", "pipe", "pipe"],
  });
  let out = "";
  let err = "";
  child.stdout.setEncoding("utf8").on("data", (c: string) => (out += c));
  child.stderr.setEncoding("utf8").on("data", (c: string) => (err += c));
  if (stdin !== null) {
    child.stdin.write(stdin);
  }
  if (!keepStdinOpen) {
    child.stdin.end();
  }
  // A BFF that never exits must fail the test, not hang it: kill it after 30 s.
  const exited = Promise.race([
    new Promise<number | null>((resolve) => child.on("exit", (code) => resolve(code))),
    new Promise<never>((_, reject) =>
      setTimeout(() => {
        child.kill("SIGKILL");
        reject(new Error("BFF did not exit in time"));
      }, 30_000).unref(),
    ),
  ]);
  /** This BFF's own gateway child, from its "gateway pid N" line. */
  const gatewayPid = () => Number(/gateway pid (\d+)/.exec(err)?.[1] ?? NaN);
  return {child, out: () => out, err: () => err, exited, gatewayPid};
}

test("main: token from stdin, URL on stdout, token never on stderr; SIGTERM cleans up", {skip: !hasVenv && "no repo .venv"}, async () => {
  const m = runMain(`${TOKEN}\n`, true);
  await until(() => !Number.isNaN(m.gatewayPid()), 20_000);
  const url = m.out().trim();
  assert.match(url, new RegExp(`^http://127\\.0\\.0\\.1:\\d+/#token=${TOKEN}$`));
  assert.doesNotMatch(m.err(), /dev mode/);
  const tab = await connect(Number(new URL(url).port), TOKEN);
  await until(() => tab.states().at(-1)?.gateway?.status === "running");
  const pid = m.gatewayPid();
  assert.ok(alive(pid));
  m.child.kill("SIGTERM");
  assert.equal(await m.exited, 0);
  await until(() => !alive(pid), 5000);
  assert.ok(!m.err().includes(TOKEN), "token must not reach stderr");
});

test("main: SIGHUP (terminal closed) also shuts down cleanly", {skip: !hasVenv && "no repo .venv"}, async () => {
  const m = runMain(`${TOKEN}\n`, true);
  await until(() => !Number.isNaN(m.gatewayPid()), 20_000);
  const pid = m.gatewayPid();
  m.child.kill("SIGHUP");
  assert.equal(await m.exited, 0);
  await until(() => !alive(pid), 5000);
});

test("main: when the CLI's stdin closes, the BFF shuts down cleanly", {skip: !hasVenv && "no repo .venv"}, async () => {
  const m = runMain(`${TOKEN}\n`, true);
  await until(() => !Number.isNaN(m.gatewayPid()), 20_000);
  const pid = m.gatewayPid();
  m.child.stdin.end();
  assert.equal(await m.exited, 0);
  assert.match(m.err(), /stdin closed/);
  await until(() => !alive(pid), 5000);
});

test("main: if the BFF is SIGKILLed, its child still exits (stdin EOF)", {skip: !hasVenv && "no repo .venv"}, async () => {
  const m = runMain(`${TOKEN}\n`, true);
  await until(() => !Number.isNaN(m.gatewayPid()), 20_000);
  const pid = m.gatewayPid();
  m.child.kill("SIGKILL");
  await m.exited;
  await until(() => !alive(pid), 10_000);
});

test("main: empty stdin or a silent open stdin means dev mode; a malformed token is refused", {skip: !hasVenv && "no repo .venv"}, async () => {
  const dev = runMain(null);
  await until(() => dev.out().includes("\n"), 20_000);
  assert.match(dev.out().trim(), /#token=[A-Za-z0-9_-]{43}$/);
  assert.match(dev.err(), /dev mode/);
  dev.child.kill("SIGINT");
  assert.equal(await dev.exited, 0);

  const started = Date.now();
  const silent = runMain(null, true);
  await until(() => silent.out().includes("\n"), 20_000);
  assert.ok(Date.now() - started >= 2500, "waited for the token first");
  assert.match(silent.err(), /dev mode/);
  silent.child.kill("SIGINT");
  assert.equal(await silent.exited, 0);

  const bad = runMain("not a token!\n");
  assert.equal(await bad.exited, 2);
  assert.equal(bad.out(), "");
});

test("main: launched by the CLI but no token (CLI gone, stdin closed) exits instead of dev mode", async () => {
  const cli = {AUGURY_LAUNCHED_BY_CLI: "1"};
  const closed = runMain(null, false, cli);
  assert.equal(await closed.exited, 3);
  assert.equal(closed.out(), "");
  assert.match(closed.err(), /no token arrived on stdin/);
  assert.doesNotMatch(closed.err(), /dev mode/);

  const silent = runMain(null, true, cli);
  assert.equal(await silent.exited, 3);
  assert.equal(silent.out(), "");
});

test("startBff: a child that cannot even spawn leaves no listening server behind", async () => {
  const servers = () => process.getActiveResourcesInfo().filter((r) => r === "TCPServerWrap").length;
  const before = servers();
  await assert.rejects(startBff({token: TOKEN, devMode: false, env: {PATH: process.env.PATH}, python: "", args: [], log: () => {}}), /cannot be empty/);
  // The handle leaves the resource list a moment after close() resolves.
  await until(() => servers() === before, 1000);
});

test("shutdown removes a secrets file the child never consumed", {skip: !hasVenv && "no repo .venv"}, async () => {
  const dir = mkdtempSync(join(tmpdir(), "augury-secrets-test-"));
  const file = join(dir, "augury-secrets-x.json");
  writeFileSync(file, JSON.stringify({OPENAI_API_KEY: "sk-x"}), {mode: 0o600});
  const bff = await startBff({
    token: TOKEN,
    devMode: false,
    env: {PATH: process.env.PATH, AUGURY_GATEWAY_SECRETS_FILE: file},
    python,
    args: ["-c", "import time; time.sleep(30)"], // never loads it
    log: () => {},
  });
  assert.ok(existsSync(file));
  await bff.shutdown();
  assert.equal(existsSync(file), false);
});

test("shutdown still closes everything when the secrets file cannot be removed", {skip: !hasVenv && "no repo .venv"}, async () => {
  // A directory makes rmSync throw (EISDIR) the way EACCES would.
  const dir = mkdtempSync(join(tmpdir(), "augury-secrets-test-"));
  const logs: string[] = [];
  const bff = await startBff({
    token: TOKEN,
    devMode: false,
    env: {PATH: process.env.PATH, AUGURY_GATEWAY_SECRETS_FILE: dir},
    python,
    args: ["-c", "import time; time.sleep(30)"],
    log: (l) => logs.push(l),
  });
  const pid = bff.gateway.pid;
  await bff.shutdown();
  assert.ok(logs.some((l) => /could not remove the secrets file/.test(l)), logs.join("\n"));
  assert.ok(!alive(pid));
  await assert.rejects(
    new Promise((resolve, reject) => httpRequest({host: "127.0.0.1", port: bff.port, path: "/"}, resolve).on("error", reject).end()),
    /ECONNREFUSED/,
  );
});

test("startBff: commands sent during shutdown get 503", {skip: !hasVenv && "no repo .venv"}, async () => {
  const bff = await startBff({token: TOKEN, devMode: false, env: {PATH: process.env.PATH}, python, args: ["-c", "import time; time.sleep(30)"], log: () => {}});
  const tab = await connect(bff.port, TOKEN);
  await until(() => tab.states().length >= 1);
  const stopping = bff.shutdown();
  const reply = await bff.bus.handle(TAB, {type: "session.interrupt"});
  assert.equal(reply.status, 503);
  await stopping;
});
