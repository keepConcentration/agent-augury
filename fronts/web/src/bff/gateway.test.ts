import assert from "node:assert/strict";
import {existsSync} from "node:fs";
import {join} from "node:path";
import {test} from "node:test";
import {GatewayChild, childArgs, childEnv, configEnvNames, resolvePython, type GatewayHandlers} from "./gateway.js";
import {repoRoot} from "./paths.js";
import type {ChildLine} from "./wire.js";

test("resolvePython: AUGURY_PYTHON, then repo venv, then PATH", () => {
  assert.equal(resolvePython({AUGURY_PYTHON: "/opt/py"}, () => true), "/opt/py");
  assert.equal(resolvePython({}, (p) => p.includes(".venv")), join(repoRoot, ".venv", "bin/python"));
  assert.equal(resolvePython({}, () => false), "python3");
});

test("childArgs: hello by default, session flags mirror Ink", () => {
  assert.deepEqual(childArgs({}), ["-m", "agent_augury.gateway.hello_demo"]);
  assert.deepEqual(
    childArgs({
      AUGURY_GATEWAY_MODE: "session",
      AUGURY_CONFIG: "/c.yaml",
      AUGURY_DEMO: "1",
      AUGURY_NO_AUTO_START: "true",
      AGENT_AUGURY_SESSION: "s1",
    }),
    // AUGURY_DEMO is gone with --demo: a stale one is ignored, never forwarded.
    ["-m", "agent_augury.gateway.session_stdio", "--config", "/c.yaml", "--no-auto-start", "--session", "s1"],
  );
  assert.throws(() => childArgs({AUGURY_GATEWAY_MODE: "session"}), /AUGURY_CONFIG/);
});

test("childArgs: team planner is refused in Phase 1", () => {
  assert.throws(
    () => childArgs({AUGURY_GATEWAY_MODE: "session", AUGURY_CONFIG: "/c.yaml", AUGURY_PLAN_FIRST: "1"}),
    /Phase 2/,
  );
});

test("childEnv is an allowlist and never forwards secret-looking names", () => {
  const env = childEnv(
    {
      PATH: "/bin",
      LC_ALL: "ko_KR.UTF-8",
      AUGURY_CONFIG: "/c.yaml",
      AUGURY_GATEWAY_SECRETS_FILE: "/tmp/s.json",
      OPENAI_API_KEY: "sk-1",
      DISCORD_BOT_TOKEN: "t",
      RANDOM_VAR: "x",
      OLLAMA_HOST: "http://127.0.0.1:11434",
      PYTHONPATH: "/extra",
    },
    "base_url: ${OLLAMA_HOST}\napi_key: ${OPENAI_API_KEY}\n",
  );
  assert.equal(env.PATH, "/bin");
  assert.equal(env.LC_ALL, "ko_KR.UTF-8");
  assert.equal(env.AUGURY_GATEWAY_SECRETS_FILE, "/tmp/s.json");
  assert.equal(env.OLLAMA_HOST, "http://127.0.0.1:11434");
  assert.equal(env.OPENAI_API_KEY, undefined);
  assert.equal(env.DISCORD_BOT_TOKEN, undefined);
  assert.equal(env.RANDOM_VAR, undefined);
  assert.equal(env.PYTHONUTF8, "1");
  assert.equal(env.PYTHONPATH, `${join(repoRoot, "src")}:/extra`);
});

test("childEnv: Ink launch keys pass; non-secret ${NAME} refs pass like Python's rule", () => {
  const env = childEnv(
    {AUGURY_PROJECT_ROOT: "/r", AUGURY_FILE_ROOT: "/w", AGENT_AUGURY_SESSION: "s", GITHUB_TOKEN: "g", SLACK_WEBHOOK_URL: "u"},
    "a: ${GITHUB_TOKEN}\nb: ${SLACK_WEBHOOK_URL}",
  );
  assert.equal(env.AUGURY_PROJECT_ROOT, "/r");
  assert.equal(env.AUGURY_FILE_ROOT, "/w");
  assert.equal(env.AGENT_AUGURY_SESSION, "s");
  assert.equal(env.GITHUB_TOKEN, "g");
  assert.equal(env.SLACK_WEBHOOK_URL, undefined);
});

test("configEnvNames finds ${NAME} references", () => {
  assert.deepEqual(configEnvNames("a: ${A}\nb: $B\nc: ${C_1}"), ["A", "C_1"]);
});

function collect() {
  const lines: ChildLine[] = [];
  const errors: Error[] = [];
  const order: string[] = [];
  let exits = 0;
  const handlers: GatewayHandlers = {
    onLine: (l) => {
      lines.push(l);
      order.push("line");
    },
    onStderr: () => {},
    onExit: () => {
      exits++;
      order.push("exit");
    },
    onError: (e) => errors.push(e),
  };
  return {lines, errors, order, handlers, exits: () => exits};
}

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

async function until(cond: () => boolean, ms = 10000): Promise<void> {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) {
      throw new Error("timed out");
    }
    await new Promise((r) => setTimeout(r, 20));
  }
}

const python = resolvePython(process.env);
const hasVenv = existsSync(python);

test("hello_demo child: events arrive valid, quit leaves no process", {skip: !hasVenv && "no repo .venv"}, async () => {
  const c = collect();
  const gw = new GatewayChild(c.handlers, {env: {PATH: process.env.PATH}, python});
  await until(() => c.lines.some((l) => l.kind === "event" && l.message.type === "human.question"));
  assert.equal(gw.status, "running");
  for (const l of c.lines) {
    assert.equal(l.kind, "event");
    assert.equal(l.kind === "event" && l.schemaError, null);
  }
  assert.equal(await gw.stop(), true);
  assert.equal(gw.status, "exited");
  assert.equal(gw.exitCode, 0);
  assert.equal(c.exits(), 1);
  assert.ok(!alive(gw.pid));
  assert.ok(c.lines.some((l) => l.kind === "event" && l.message.type === "session.ended"));
  assert.equal(gw.send({dir: "cmd", type: "session.quit", id: "late"}), false);
  assert.deepEqual(c.errors, []);
});

test("stop escalates to SIGTERM when the child ignores quit", {skip: !hasVenv && "no repo .venv"}, async () => {
  const c = collect();
  const gw = new GatewayChild(c.handlers, {
    env: {PATH: process.env.PATH},
    python,
    args: ["-c", "import time; time.sleep(30)"],
  });
  await gw.stop(300);
  assert.equal(gw.status, "exited");
  assert.equal(gw.signal, "SIGTERM");
  assert.ok(!alive(gw.pid));
});

test("stop escalates to SIGKILL when SIGTERM is ignored", {skip: !hasVenv && "no repo .venv"}, async () => {
  const c = collect();
  const gw = new GatewayChild(c.handlers, {
    env: {PATH: process.env.PATH},
    python,
    args: ["-c", "import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('{}', flush=True); time.sleep(30)"],
  });
  await until(() => gw.status === "running");
  assert.equal(await gw.stop(300), true);
  assert.equal(gw.signal, "SIGKILL");
  assert.ok(!alive(gw.pid));
});

test("concurrent stop() calls share one shutdown", {skip: !hasVenv && "no repo .venv"}, async () => {
  const c = collect();
  const gw = new GatewayChild(c.handlers, {env: {PATH: process.env.PATH}, python, args: ["-c", "import time; time.sleep(30)"]});
  const a = gw.stop(300);
  const b = gw.stop(300);
  assert.equal(a, b);
  assert.equal(await a, true);
  assert.equal(c.exits(), 1);
});

test("every stdout line is delivered before onExit", {skip: !hasVenv && "no repo .venv"}, async () => {
  const c = collect();
  const n = 3000;
  const script = `import json,sys\nfor i in range(${n}): sys.stdout.write(json.dumps({"dir":"event","type":"log","text":"x"*200})+"\\n")\nsys.stdout.flush()`;
  const gw = new GatewayChild(c.handlers, {env: {PATH: process.env.PATH}, python, args: ["-c", script]});
  await until(() => gw.status === "exited");
  assert.equal(c.lines.length, n);
  assert.equal(c.order.at(-1), "exit");
  assert.equal(c.order.indexOf("exit"), n);
});

test("stop also ends run_command-style grandchildren", {skip: (!hasVenv || process.platform === "win32") && "needs POSIX + .venv"}, async () => {
  const c = collect();
  const script =
    "import json,subprocess,time\n" +
    "p = subprocess.Popen(['sleep', '30'])\n" +
    "print(json.dumps({'dir':'event','type':'log','text':str(p.pid)}), flush=True)\n" +
    "time.sleep(30)";
  const gw = new GatewayChild(c.handlers, {env: {PATH: process.env.PATH}, python, args: ["-c", script]});
  await until(() => c.lines.length > 0);
  const first = c.lines[0];
  const grandchild = Number(first.kind === "event" ? first.message.text : NaN);
  assert.ok(alive(grandchild));
  assert.equal(await gw.stop(300), true);
  await until(() => !alive(grandchild), 3000);
});

test("send after the child died returns false and EPIPE stays contained", {skip: !hasVenv && "no repo .venv"}, async () => {
  const c = collect();
  const gw = new GatewayChild(c.handlers, {env: {PATH: process.env.PATH}, python, args: ["-c", "pass"]});
  // Race the exit: early writes may still be accepted, later ones must fail softly.
  for (let i = 0; i < 50 && gw.status !== "exited"; i++) {
    gw.send({dir: "cmd", type: "session.quit", id: `x${i}`});
    await new Promise((r) => setTimeout(r, 5));
  }
  await until(() => gw.status === "exited");
  assert.equal(gw.send({dir: "cmd", type: "session.quit", id: "late"}), false);
  assert.ok(c.errors.every((e) => /EPIPE|write after end|ECONNRESET/.test(String(e))));
});

test("a throwing handler is reported, not fatal", {skip: !hasVenv && "no repo .venv"}, async () => {
  const errors: Error[] = [];
  const gw = new GatewayChild(
    {
      onLine: () => {
        throw new Error("boom");
      },
      onStderr: () => {},
      onExit: () => {},
      onError: (e) => errors.push(e),
    },
    {env: {PATH: process.env.PATH}, python, args: ["-c", "print('{}')"]},
  );
  await until(() => gw.status === "exited");
  assert.ok(errors.some((e) => e.message === "boom"));
});

test("spawn failure is reported and the child counts as exited", async () => {
  const c = collect();
  const gw = new GatewayChild(c.handlers, {env: {}, python: "/nonexistent/python", args: []});
  await until(() => gw.status === "exited");
  assert.equal(c.errors.length, 1);
  assert.equal(gw.send({dir: "cmd", type: "session.quit", id: "x"}), false);
});

test("unreadable AUGURY_CONFIG is reported before spawning", {skip: !hasVenv && "no repo .venv"}, async () => {
  const c = collect();
  const gw = new GatewayChild(c.handlers, {
    env: {PATH: process.env.PATH, AUGURY_CONFIG: "/nonexistent.yaml"},
    python,
    args: ["-c", "pass"],
  });
  await until(() => gw.status === "exited");
  assert.match(String(c.errors[0]), /AUGURY_CONFIG/);
});
