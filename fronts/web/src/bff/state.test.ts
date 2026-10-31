import assert from "node:assert/strict";
import {existsSync, mkdtempSync} from "node:fs";
import {request as httpRequest, type IncomingMessage} from "node:http";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {test} from "node:test";
import {CommandBus} from "./cmd.js";
import {GatewayChild, resolvePython} from "./gateway.js";
import {startServer, type BffServer} from "./http.js";
import {EventHub, type Notice} from "./sse.js";
import {BffState, handleChildExit, handleChildLine, type BffParts} from "./state.js";
import {parseChildLine, type WireMessage} from "./wire.js";

const A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

function unit(opts: {grace?: number; now?: () => number} = {}) {
  let broadcasts = 0;
  const notices: Notice[] = [];
  const connected = new Set<string>();
  const state = new BffState({
    broadcast: () => broadcasts++,
    notice: (n) => notices.push(n),
    isConnected: (t) => connected.has(t),
    controllerGraceMs: opts.grace ?? 5000,
    now: opts.now,
  });
  return {state, notices, connected, broadcasts: () => broadcasts};
}

const ev = (type: string, extra: Record<string, unknown> = {}): WireMessage => ({dir: "event", type, ...extra});
const snap = (s: BffState, tab = A) => s.snapshot(tab) as Record<string, any>;

test("session events drive agents, phase, gate, human approval and ended (08 §4.4)", () => {
  const {state} = unit();
  state.applyEvent(ev("session.started", {agents: ["alice", "bob"]}));
  assert.deepEqual(snap(state).agents, ["alice", "bob"]);
  state.applyEvent(ev("session.phase", {phase: "P2"}));
  state.applyEvent(ev("session.gate", {phase: "P2", open: true, human_pending: true}));
  state.applyEvent(ev("session.human_approval_pending", {phase: "P2", thread_id: "t1", text: "approve?"}));
  let s = snap(state);
  assert.equal(s.phase, "P2");
  assert.deepEqual(s.gate, {phase: "P2", open: true, human_pending: true});
  assert.deepEqual(s.human_approval_pending, {phase: "P2", thread_id: "t1", text: "approve?"});
  // human_pending=false clears the approval banner, keeps the gate.
  state.applyEvent(ev("session.gate", {phase: "P2", human_pending: false}));
  assert.equal(snap(state).human_approval_pending, null);
  assert.ok(snap(state).gate);
  // turn_done clears both (Ink App.tsx parity).
  state.applyEvent(ev("session.human_approval_pending", {text: "again"}));
  state.applyEvent(ev("session.turn_done", {reason: "idle", steps: 3}));
  s = snap(state);
  assert.equal(s.gate, null);
  assert.equal(s.human_approval_pending, null);
  // COMPLETED clears the gate; a new phase clears the banner.
  state.applyEvent(ev("session.gate", {phase: "P5"}));
  state.applyEvent(ev("session.human_approval_pending", {text: "x"}));
  state.applyEvent(ev("session.phase", {phase: "COMPLETED"}));
  s = snap(state);
  assert.equal(s.gate, null);
  assert.equal(s.human_approval_pending, null);
  state.applyEvent(ev("session.resumed", {phase: "P3", agents: ["carol"]}));
  assert.deepEqual([snap(state).phase, snap(state).agents], ["P3", ["carol"]]);
  state.applyEvent(ev("session.ended", {reason: "quit", steps: 9}));
  assert.deepEqual(snap(state).ended, {reason: "quit", steps: 9});
  state.applyEvent(ev("session.started", {}));
  assert.deepEqual([snap(state).ended, snap(state).agents], [null, []]);
  assert.equal(state.applyEvent(ev("message", {content: "hi"})), false);
});

test("questions: FIFO, a question without id is not pending and raises an error notice", () => {
  const {state, notices} = unit();
  state.applyEvent(ev("human.question", {question_id: "q1", question: "first?", options: ["a", "b"], agent_id: "bob"}));
  state.applyEvent(ev("human.question", {question_id: "q2", question: "second?"}));
  assert.equal(state.applyEvent(ev("human.question", {question: "no id"})), false);
  assert.deepEqual(snap(state).pending_questions.map((q: any) => q.question_id), ["q1", "q2"]);
  assert.deepEqual(snap(state).pending_questions[0], {
    question_id: "q1", agent_id: "bob", thread_id: null, question: "first?", options: ["a", "b"], resolving: false,
  });
  assert.deepEqual(notices.map((n) => [n.kind, n.level]), [["schema_invalid", "error"]]);
});

test("approvals: revision only moves when tool or args change; long args are clipped", () => {
  let now = 100;
  const {state} = unit({now: () => now});
  state.applyEvent(ev("approval.request", {approval_id: "a1", agent_id: "bob", tool: "run_command", args_preview: {command: "ls"}, ttl_seconds: 60}));
  now = 200;
  state.applyEvent(ev("approval.request", {approval_id: "a1", agent_id: "bob", tool: "run_command", args_preview: {command: "ls"}, ttl_seconds: 60}));
  let a = snap(state).pending_approvals[0];
  assert.deepEqual([a.revision, a.received_at, a.ttl_seconds], [1, 200, 60]);
  state.applyEvent(ev("approval.request", {approval_id: "a1", agent_id: "bob", tool: "run_command", args_preview: {command: "rm -rf /"}}));
  a = snap(state).pending_approvals[0];
  assert.deepEqual([a.revision, a.ttl_seconds, a.args_preview.command], [2, null, "rm -rf /"]);
  state.applyEvent(ev("approval.request", {approval_id: "a1", agent_id: "bob", tool: "write_file", args_preview: {command: "rm -rf /"}}));
  assert.equal(snap(state).pending_approvals[0].revision, 3);
  state.applyEvent(ev("approval.request", {approval_id: "a2", agent_id: "x", tool: "write_file", args_preview: {content: "y".repeat(5000), n: 1}}));
  const big = snap(state).pending_approvals[1].args_preview;
  assert.equal(big.content, `${"y".repeat(4000)}… (+1000 chars)`);
  assert.equal(big.n, 1);
});

test("revision sees changes past the clip, ignores key order, survives while resolving", () => {
  const {state} = unit();
  const long = "x".repeat(5000);
  const req = (args: Record<string, unknown>, tool: unknown = "run_command") =>
    state.applyEvent(ev("approval.request", {approval_id: "a1", agent_id: "b", tool, args_preview: args}));
  req({command: `${long}SAFE`, cwd: "/w"});
  state.setResolving("a:a1", true);
  req({cwd: "/w", command: `${long}SAFE`}); // same content, other key order
  assert.equal(snap(state).pending_approvals[0].revision, 1);
  // Same length, differs only after 4000 chars: the clipped preview (and its
  // "(+N chars)" marker) is identical, so only the private fingerprint sees it.
  req({command: `${long}EVIL`, cwd: "/w"});
  const a = snap(state).pending_approvals[0];
  assert.equal(a.revision, 2);
  assert.equal(a.resolving, true, "a sent command cannot be recalled");
  assert.ok(!JSON.stringify(a).includes("EVIL"), "unclipped args stay private");
  // A non-string tool is compared as its string form, not "always different".
  state.applyEvent(ev("approval.request", {approval_id: "a2", agent_id: "b", tool: 7}));
  state.applyEvent(ev("approval.request", {approval_id: "a2", agent_id: "b", tool: 7}));
  assert.equal(snap(state).pending_approvals[1].revision, 1);
});

test("applyEvent reports a change only when the snapshot really changed", () => {
  const {state} = unit();
  assert.equal(state.applyEvent(ev("session.phase", {phase: "P1"})), true);
  assert.equal(state.applyEvent(ev("session.phase", {phase: "P1"})), false);
  assert.equal(state.applyEvent(ev("session.gate", {phase: "P1", open: true})), true);
  assert.equal(state.applyEvent(ev("session.gate", {phase: "P1", open: true})), false);
  assert.equal(state.applyEvent(ev("session.turn_done", {reason: "idle", steps: 1})), true);
  assert.equal(state.applyEvent(ev("session.turn_done", {reason: "idle", steps: 2})), false);
});

test("nothing becomes answerable after session.ended or child exit; empty ids are ignored", () => {
  const {state} = unit();
  state.applyEvent(ev("session.ended", {reason: "quit"}));
  assert.equal(state.applyEvent(ev("human.question", {question_id: "q1", question: "?"})), false);
  assert.equal(state.applyEvent(ev("approval.request", {approval_id: "a1", agent_id: "b", tool: "t"})), false);
  state.applyEvent(ev("session.started", {agents: ["x"]}));
  assert.equal(state.applyEvent(ev("human.question", {question_id: "q1", question: "?"})), true, "a new session reopens");

  const other = unit().state;
  other.gatewayExited(0, null);
  assert.equal(other.applyEvent(ev("approval.request", {approval_id: "a1", agent_id: "b", tool: "t"})), false);
  assert.equal(unit().state.applyEvent(ev("approval.request", {approval_id: "", agent_id: "b", tool: "t"})), false);
});

test("approval release events, and repeats are no-ops", () => {
  const {state} = unit();
  for (const id of ["a1", "a2", "a3", "a4"]) {
    state.applyEvent(ev("approval.request", {approval_id: id, agent_id: "b", tool: "t"}));
  }
  assert.equal(state.applyEvent(ev("approval.resolved", {approval_id: "a1", decision: "denied"})), true);
  assert.equal(state.applyEvent(ev("approval.granted", {approval_id: "a2"})), true);
  assert.equal(state.applyEvent(ev("approval.expired", {approval_id: "a3"})), true);
  assert.equal(state.applyEvent(ev("tool.denied", {approval_id: "a4", reason: "denied"})), true);
  assert.equal(state.applyEvent(ev("tool.denied", {approval_id: "a4", reason: "denied"})), false);
  assert.equal(state.applyEvent(ev("tool.denied", {reason: "policy"})), false);
  assert.deepEqual(snap(state).pending_approvals, []);
});

test("CommandState: check/setResolving/release; unknown keys are silent no-ops", () => {
  const {state, broadcasts} = unit();
  state.applyEvent(ev("human.question", {question_id: "q1", question: "?"}));
  assert.equal(state.check("q:q1"), null);
  assert.equal(state.check("a:nope"), "not_pending");
  state.setResolving("q:q1", true);
  assert.equal(state.check("q:q1"), "in_flight");
  assert.equal(snap(state).pending_questions[0].resolving, true);
  const before = broadcasts();
  state.setResolving("q:q1", true); // unchanged
  state.setResolving("a:nope", false);
  state.release("a:nope");
  assert.equal(broadcasts(), before);
  state.release("q:q1");
  assert.equal(broadcasts(), before + 1);
  assert.equal(state.check("q:q1"), "not_pending");
});

test("child exit and session.ended clear everything answerable (CP-2); the controller stays", () => {
  const {state} = unit();
  state.tabConnected(A);
  state.applyEvent(ev("human.question", {question_id: "q1", question: "?"}));
  state.applyEvent(ev("approval.request", {approval_id: "a1", agent_id: "b", tool: "t"}));
  state.applyEvent(ev("session.human_approval_pending", {text: "x"}));
  state.gatewayExited(1, null);
  const s = snap(state);
  assert.deepEqual([s.pending_questions, s.pending_approvals, s.human_approval_pending], [[], [], null]);
  assert.deepEqual(s.gateway, {status: "exited", exit_code: 1, signal: null});
  assert.equal(s.is_controller, true);

  const other = unit().state;
  other.applyEvent(ev("human.question", {question_id: "q1", question: "?"}));
  other.applyEvent(ev("session.ended", {reason: "quit"}));
  assert.deepEqual(snap(other).pending_questions, []);
});

test("controller: T1, T1', T2, T3→T4, T3→T5, T6 (08 §7.2)", async () => {
  const {state, broadcasts} = unit({grace: 30});
  state.tabConnected(A); // T1
  assert.deepEqual([state.isController(A), snap(state, B).has_controller, snap(state, B).is_controller], [true, true, false]);
  assert.equal(broadcasts(), 0, "T1 does not broadcast");
  state.tabConnected(B); // T2
  assert.equal(state.isController(A), true);
  state.tabDisconnected(B); // not the controller: nothing
  state.tabDisconnected(A); // T3
  assert.deepEqual([state.isController(A), snap(state, B).has_controller], [true, true]);
  state.tabConnected(C); // T6
  assert.equal(state.isController(C), false);
  state.tabConnected(A); // T4
  await new Promise((r) => setTimeout(r, 60));
  assert.equal(state.isController(A), true, "grace timer cancelled by T4");
  state.tabDisconnected(A); // T3
  await new Promise((r) => setTimeout(r, 60)); // T5
  assert.equal(snap(state, B).has_controller, false);
  assert.equal(broadcasts(), 1, "only T5 broadcast");
  state.tabConnected(A); // T1': no automatic promotion
  assert.equal(state.isController(A), false);
});

test("claim: T7 from INIT/NONE/ORPHANED, T8, T9, T10, T11", async () => {
  const {state, connected, broadcasts} = unit({grace: 20});
  assert.equal(state.claim(A, false), null); // T11
  connected.add(A);
  connected.add(B);
  assert.equal(state.claim(A, false), true); // T7 from INIT
  assert.equal(state.claim(A, false), true); // T8
  assert.equal(state.claim(B, false), false); // T9
  assert.equal(state.claim(B, true), true); // T10
  assert.deepEqual([state.isController(A), state.isController(B)], [false, true]);
  state.tabDisconnected(B); // ORPHANED(B)
  assert.equal(state.claim(A, false), true); // T7 from ORPHANED
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(state.isController(A), true, "B's grace timer was cancelled");
  state.tabDisconnected(A);
  await new Promise((r) => setTimeout(r, 40)); // NONE
  assert.equal(state.claim(B, false), true); // T7 from NONE
  assert.equal(broadcasts(), 5); // T7, T10, T7, T5, T7
  state.close();
});

// ---- HTTP + SSE integration ---------------------------------------------------------

type Frame = {event?: string; data?: string; id?: string};

class Stream {
  frames: Frame[] = [];
  ended = false;
  res!: IncomingMessage;
  private buf = "";
  static open(port: number, cookie: string, tab: string): Promise<Stream> {
    const s = new Stream();
    return new Promise((resolve, reject) => {
      const req = httpRequest({host: "127.0.0.1", port, path: `/api/events?tab=${tab}`, headers: {Cookie: cookie}}, (res) => {
        s.res = res;
        res.setEncoding("utf8");
        res.on("data", (chunk: string) => {
          s.buf += chunk;
          let i: number;
          while ((i = s.buf.indexOf("\n\n")) !== -1) {
            const f: Record<string, string> = {};
            for (const line of s.buf.slice(0, i).split("\n")) {
              const [k, ...v] = line.split(": ");
              f[k] = v.join(": ");
            }
            s.buf = s.buf.slice(i + 2);
            s.frames.push(f);
          }
        });
        res.on("close", () => (s.ended = true));
        resolve(s);
      });
      req.on("error", reject);
      req.end();
    });
  }
  states(): Array<Record<string, any>> {
    return this.frames.filter((f) => f.event === "bff.state").map((f) => JSON.parse(f.data!));
  }
  last(): Record<string, any> {
    return this.states().at(-1)!;
  }
}

async function until(cond: () => boolean, ms = 5000): Promise<void> {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) {
      throw new Error("timed out");
    }
    await new Promise((r) => setTimeout(r, 10));
  }
}

type Harness = BffParts & {bff: BffServer; cookie: string; sent: WireMessage[]; call: (method: string, path: string, body?: unknown, tab?: string) => Promise<{status: number; body: any}>};

async function harness(sink?: {alive: () => boolean; send: (m: WireMessage) => boolean}): Promise<Harness> {
  const sent: WireMessage[] = [];
  let hub!: EventHub;
  const state = new BffState({
    broadcast: () => hub.broadcastState(),
    notice: (n) => hub.publishNotice(n),
    isConnected: (t) => hub.connectedTabs().includes(t),
  });
  hub = new EventHub({
    snapshot: (t) => state.snapshot(t),
    onConnect: (t) => state.tabConnected(t),
    onDisconnect: (t) => state.tabDisconnected(t),
  });
  const bus = new CommandBus({
    sink: sink ?? {alive: () => true, send: (m) => (sent.push(m), true)},
    state,
    hub,
    log: () => {},
    timeoutMs: 3000,
  });
  const token = "z".repeat(43);
  const bff = await startServer({
    token,
    staticRoot: mkdtempSync(join(tmpdir(), "augury-state-")),
    onRevoke: () => {},
    routes: [
      hub.route(),
      bus.route(),
      state.claimRoute(),
      state.sessionRoute({
        flags: {demo: true, no_auto_start: false, plan_first: false, session: null},
        devMode: false,
        seqLast: () => hub.seqLast,
        streamsAvailable: () => hub.available(),
      }),
    ],
  });
  const origin = `http://127.0.0.1:${bff.port}`;
  let cookie = "";
  const call = (method: string, path: string, body?: unknown, tab?: string) =>
    new Promise<{status: number; body: any}>((resolve, reject) => {
      const req = httpRequest(
        {
          host: "127.0.0.1",
          port: bff.port,
          method,
          path,
          headers: {
            ...(body !== undefined ? {"Content-Type": "application/json", Origin: origin} : {}),
            ...(cookie ? {Cookie: cookie} : {}),
            ...(tab ? {"X-Augury-Tab": tab} : {}),
          },
        },
        (res) => {
          let data = "";
          res.setEncoding("utf8");
          res.on("data", (c) => (data += c));
          res.on("end", () => {
            if (res.headers["set-cookie"]) {
              cookie = String(res.headers["set-cookie"]).split(";")[0];
            }
            resolve({status: res.statusCode ?? 0, body: data ? JSON.parse(data) : null});
          });
        },
      );
      req.on("error", reject);
      req.end(body !== undefined ? JSON.stringify(body) : undefined);
    });
  await call("POST", "/api/auth/exchange", {token});
  return {hub, state, bus, bff, cookie, sent, call};
}

async function close(h: Harness): Promise<void> {
  h.state.close();
  h.hub.close();
  await h.bff.close();
}

test("two tabs: the first is controller; claim and force move it; the old tab is refused", async () => {
  const h = await harness();
  const a = await Stream.open(h.bff.port, h.cookie, A);
  await until(() => a.states().length === 1);
  const b = await Stream.open(h.bff.port, h.cookie, B);
  await until(() => b.states().length === 1);
  assert.deepEqual([a.last().is_controller, b.last().is_controller, b.last().has_controller], [true, false, true]);
  assert.doesNotMatch(JSON.stringify(b.frames), new RegExp(A), "controller UUID is never broadcast");

  assert.deepEqual(await h.call("POST", "/api/control/claim", {tab_id: B}), {status: 200, body: {controller: false}});
  assert.deepEqual(await h.call("POST", "/api/control/claim", {tab_id: B, force: true}), {status: 200, body: {controller: true}});
  await until(() => a.last().is_controller === false && b.last().is_controller === true);
  const refused = await h.call("POST", "/api/cmd", {type: "session.interrupt"}, A);
  assert.deepEqual([refused.status, refused.body.error.code], [409, "not_controller"]);

  assert.equal((await h.call("POST", "/api/control/claim", {tab_id: C})).body.error.code, "tab_not_connected");
  assert.equal((await h.call("POST", "/api/control/claim", {tab_id: B, force: "yes"})).status, 400);
  assert.equal((await h.call("POST", "/api/control/claim", {tab_id: B, force: null})).status, 400);
  a.res.destroy();
  b.res.destroy();
  await close(h);
});

test("API-03 session info", async () => {
  const h = await harness();
  const a = await Stream.open(h.bff.port, h.cookie, A);
  await until(() => a.states().length === 1);
  h.hub.publishWire(ev("log", {text: "x"}));
  const r = await h.call("GET", `/api/session?tab=${A}`);
  assert.deepEqual(r, {
    status: 200,
    body: {
      gateway: {status: "starting", exit_code: null, signal: null},
      seq_last: 1,
      flags: {demo: true, no_auto_start: false, plan_first: false, session: null},
      dev_mode: false,
      tab: {controller: true},
      streams_available: 3,
    },
  });
  assert.equal((await h.call("GET", `/api/session?tab=${B}`)).body.tab.controller, false);
  assert.equal((await h.call("GET", "/api/session")).body.tab.controller, false);
  assert.equal((await h.call("GET", "/api/session?tab=nope")).status, 400);
  a.res.destroy();
  await close(h);
});

test("handleChildLine: wire frame first, then one bff.state; invalid events never touch state", async () => {
  const h = await harness();
  const a = await Stream.open(h.bff.port, h.cookie, A);
  await until(() => a.states().length === 1);
  handleChildLine(parseChildLine('{"dir":"event","type":"human.question","question_id":"q1","question":"?"}'), h);
  await until(() => a.states().length === 2);
  const tail = a.frames.slice(-2).map((f) => f.event);
  assert.deepEqual(tail, ["wire", "bff.state"]);
  assert.equal(a.last().gateway.status, "running");
  assert.equal(a.last().pending_questions[0].question_id, "q1");

  handleChildLine(parseChildLine('{"dir":"event","type":"approval.request","approval_id":"a1"}'), h); // missing agent_id/tool
  handleChildLine(parseChildLine('{"dir":"event","type":"log","text":"plain"}'), h);
  await until(() => a.frames.filter((f) => f.event === "wire").length === 3);
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(a.states().length, 2, "no state broadcast for invalid or stateless events");
  assert.deepEqual(a.last().pending_approvals, []);
  a.res.destroy();
  await close(h);
});

test("handleChildExit: waiting command 503, pending cleared, exit broadcast", async () => {
  const h = await harness();
  const a = await Stream.open(h.bff.port, h.cookie, A);
  await until(() => a.states().length === 1);
  handleChildLine(parseChildLine('{"dir":"event","type":"human.question","question_id":"q1","question":"?"}'), h);
  const waiting = h.call("POST", "/api/cmd", {type: "human.skip", question_id: "q1"}, A);
  await until(() => h.sent.length === 1);
  handleChildExit(0, null, h);
  const r = await waiting;
  assert.deepEqual([r.status, r.body.error.code], [503, "gateway_unavailable"]);
  await until(() => a.last().gateway.status === "exited");
  assert.deepEqual(a.last().pending_questions, []);
  a.res.destroy();
  await close(h);
});

const python = resolvePython(process.env);

test("end to end with hello_demo: question arrives, controller skips it, every tab sees it go", {skip: !existsSync(python) && "no repo .venv"}, async () => {
  let gw!: GatewayChild;
  const h = await harness({alive: () => gw.status !== "exited", send: (m) => gw.send(m)});
  const a = await Stream.open(h.bff.port, h.cookie, A);
  const b = await Stream.open(h.bff.port, h.cookie, B);
  await until(() => a.states().length >= 1 && b.states().length >= 1);
  gw = new GatewayChild(
    {
      onLine: (line) => handleChildLine(line, h),
      onStderr: () => {},
      onExit: (code, signal) => handleChildExit(code, signal, h),
      onError: () => {},
    },
    {env: {PATH: process.env.PATH}, python},
  );
  await until(() => b.last().pending_questions?.length === 1, 10_000);
  assert.deepEqual(a.last().agents, ["demo-agent"]);
  assert.equal(b.last().pending_questions[0].question_id, "q-demo-1");

  const fromB = await h.call("POST", "/api/cmd", {type: "human.skip", question_id: "q-demo-1"}, B);
  assert.equal(fromB.body.error.code, "not_controller");
  const ok = await h.call("POST", "/api/cmd", {type: "human.skip", question_id: "q-demo-1"}, A);
  assert.deepEqual([ok.status, ok.body.failed, ok.body.result.skipped], [200, false, true]);
  await until(() => a.last().pending_questions.length === 0 && b.last().pending_questions.length === 0);

  assert.equal(await gw.stop(), true);
  await until(() => b.last().gateway.status === "exited");
  assert.deepEqual(b.last().ended, {reason: "quit", steps: null});
  a.res.destroy();
  b.res.destroy();
  await close(h);
});
