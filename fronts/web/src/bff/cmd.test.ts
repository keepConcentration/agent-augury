import assert from "node:assert/strict";
import {existsSync, mkdtempSync} from "node:fs";
import {request as httpRequest} from "node:http";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {test} from "node:test";
import {CommandBus, judge, type CmdKey, type CommandSink, type CommandState} from "./cmd.js";
import {GatewayChild, resolvePython} from "./gateway.js";
import {startServer} from "./http.js";
import {EventHub, forwardChildLine, type Notice} from "./sse.js";
import type {WireMessage} from "./wire.js";

const TAB = "3f0c9b1e-8d2a-4c41-9b7e-2a5d0e6f1c90";
const OTHER = "11111111-2222-4333-8444-555555555555";

class StubState implements CommandState {
  controller = TAB;
  pending = new Set<CmdKey>();
  resolving = new Set<CmdKey>();
  calls: string[] = [];
  isController(tab: string): boolean {
    return tab === this.controller;
  }
  check(key: CmdKey) {
    if (!this.pending.has(key)) {
      return "not_pending" as const;
    }
    return this.resolving.has(key) ? ("in_flight" as const) : null;
  }
  // Unknown keys are no-ops (CommandState contract).
  setResolving(key: CmdKey, on: boolean): void {
    if (!this.pending.has(key)) {
      return;
    }
    this.calls.push(`${on ? "resolving" : "answerable"} ${key}`);
    if (on) {
      this.resolving.add(key);
    } else {
      this.resolving.delete(key);
    }
  }
  release(key: CmdKey): void {
    if (!this.pending.has(key)) {
      return;
    }
    this.calls.push(`release ${key}`);
    this.pending.delete(key);
    this.resolving.delete(key);
  }
}

class FakeSink implements CommandSink {
  sent: WireMessage[] = [];
  up = true;
  accepts = true;
  alive(): boolean {
    return this.up;
  }
  send(m: WireMessage): boolean {
    this.sent.push(m);
    return this.accepts;
  }
}

class NoticeHub extends EventHub {
  notices: Notice[] = [];
  override publishNotice(n: Notice): void {
    this.notices.push(n);
    super.publishNotice(n);
  }
}

function setup(opts: {timeoutMs?: number; unknownMs?: number; maxUnknown?: number} = {}) {
  const state = new StubState();
  const sink = new FakeSink();
  const hub = new NoticeHub();
  const logs: string[] = [];
  const bus = new CommandBus({sink, state, hub, log: (l) => logs.push(l), ...opts});
  return {state, sink, hub, bus, logs};
}

const code = (r: {body?: unknown}) => (r.body as {error?: {code: string}}).error?.code;
const tick = () => new Promise((r) => setTimeout(r, 0));

test("judge (08 §6.3 table)", () => {
  assert.deepEqual(judge({ok: true, queued: true, thread_id: "t"}), {failed: false, failure: null});
  assert.deepEqual(judge({ok: true, queued: true, as_turn: true}), {failed: false, failure: null});
  assert.deepEqual(judge({ok: true}), {failed: false, failure: null});
  assert.deepEqual(judge({ok: true, queued: false, error: "no pending question"}), {failed: true, failure: "no pending question"});
  assert.deepEqual(judge({ok: true, skipped: false, reason: "no pending question"}), {failed: true, failure: "no pending question"});
  assert.deepEqual(judge({ok: false, error: "decision must be granted|denied"}), {failed: true, failure: "decision must be granted|denied"});
  assert.deepEqual(judge({ok: true, queued: true, error: null}), {failed: false, failure: null});
  assert.deepEqual(judge({ok: false}), {failed: true, failure: "rejected"});
});

test("step 0 comes first: a read-only or headerless tab always gets not_controller", async () => {
  const {bus, sink} = setup();
  for (const tab of [undefined, "", "not-a-uuid", OTHER, TAB.toUpperCase()]) {
    const r = await bus.handle(tab, {type: "nope"});
    assert.equal(r.status, 409);
    assert.equal(code(r), "not_controller");
  }
  assert.equal(sink.sent.length, 0);
});

test("steps 1–3: unknown type, schema, answers need a question_id", async () => {
  const {bus, state, sink} = setup();
  state.pending.add("q:q1");
  assert.equal(code(await bus.handle(TAB, {type: "nope"})), "unknown_command");
  assert.equal(code(await bus.handle(TAB, {type: "toString"})), "unknown_command");
  assert.equal(code(await bus.handle(TAB, {type: "human.send"})), "schema_invalid");
  assert.equal(code(await bus.handle(TAB, {type: "human.send", content: 3})), "schema_invalid");
  assert.equal(code(await bus.handle(TAB, {type: "human.answer", content: "x"})), "schema_invalid");
  assert.equal(code(await bus.handle(TAB, {type: "human.skip", question_id: ""})), "schema_invalid");
  assert.equal(code(await bus.handle(TAB, {type: "approval.resolve", approval_id: "a1"})), "schema_invalid");
  assert.equal(sink.sent.length, 0);
});

test("step 2: only allowlisted fields leave; dir, id and source are the BFF's", async () => {
  const {bus, state, sink} = setup();
  state.pending.add("q:q1");
  const pending = bus.handle(TAB, {
    type: "human.answer",
    content: "yes",
    question_id: "q1",
    thread_id: "t1",
    mentions: ["alice"],
    approval_id: "a9",
    decision: "granted",
    source: {surface: "ink", mode: "observe"},
    dir: "event",
    id: "mine",
    extra: "x",
  });
  await tick();
  assert.deepEqual(sink.sent[0], {
    dir: "cmd",
    type: "human.answer",
    content: "yes",
    question_id: "q1",
    thread_id: "t1",
    mentions: ["alice"],
    id: "web-1",
    source: {surface: "web", mode: "interact"},
  });
  bus.onResult({dir: "result", id: "web-1", ok: true, queued: true});
  await pending;
  const start = bus.handle(TAB, {type: "session.start", initial_prompt: "do evil"});
  await tick();
  assert.deepEqual(sink.sent[1], {dir: "cmd", type: "session.start", id: "web-2"});
  bus.onResult({dir: "result", id: "web-2", ok: true, started: true});
  await start;
});

test("steps 4–5: not_pending and in_flight come from the state module", async () => {
  const {bus, state} = setup();
  assert.equal(code(await bus.handle(TAB, {type: "human.skip", question_id: "q1"})), "not_pending");
  state.pending.add("a:a1");
  state.resolving.add("a:a1");
  assert.equal(code(await bus.handle(TAB, {type: "approval.resolve", approval_id: "a1", decision: "denied"})), "in_flight");
});

test("step 6/7: dead child or failed write → 503 at once", async () => {
  const {bus, sink, state} = setup();
  sink.up = false;
  const a = await bus.handle(TAB, {type: "session.interrupt"});
  assert.equal(a.status, 503);
  assert.equal(code(a), "gateway_unavailable");
  sink.up = true;
  sink.accepts = false;
  state.pending.add("q:q1");
  const b = await bus.handle(TAB, {type: "human.skip", question_id: "q1"});
  assert.equal(b.status, 503);
  assert.deepEqual(state.calls, []);
});

test("step 8: success releases the key; 200 carries the verdict", async () => {
  const {bus, state} = setup();
  state.pending.add("a:a1");
  const r = bus.handle(TAB, {type: "approval.resolve", approval_id: "a1", decision: "denied"});
  await tick();
  assert.ok(state.resolving.has("a:a1"));
  bus.onResult({dir: "result", id: "web-1", ok: true, queued: true, approval_id: "a1", decision: "denied"});
  const reply = await r;
  assert.equal(reply.status, 200);
  assert.deepEqual(reply.body, {
    id: "web-1",
    result: {dir: "result", id: "web-1", ok: true, queued: true, approval_id: "a1", decision: "denied"},
    failed: false,
    failure: null,
  });
  assert.deepEqual(state.calls, ["resolving a:a1", "release a:a1"]);
});

test("step 8: 'no pending question' releases; other Core refusals keep the card answerable", async () => {
  const {bus, state} = setup();
  state.pending.add("q:q1");
  const a = bus.handle(TAB, {type: "human.skip", question_id: "q1"});
  await tick();
  bus.onResult({dir: "result", id: "web-1", ok: true, skipped: false, reason: "no pending question"});
  assert.equal((await a).status, 200);
  state.pending.add("a:a2");
  const b = bus.handle(TAB, {type: "approval.resolve", approval_id: "a2", decision: "granted"});
  await tick();
  bus.onResult({dir: "result", id: "web-2", ok: false, error: "no session.resolve_approval installed"});
  const body = (await b).body as {failed: boolean; failure: string};
  assert.deepEqual([body.failed, body.failure], [true, "no session.resolve_approval installed"]);
  assert.deepEqual(state.calls, ["resolving q:q1", "release q:q1", "resolving a:a2", "answerable a:a2"]);
  assert.ok(state.pending.has("a:a2"));
});

test("504 = result unknown: key stays resolving; a late result settles it and is announced", async () => {
  const {bus, state, hub} = setup({timeoutMs: 20});
  state.pending.add("q:q1");
  const r = await bus.handle(TAB, {type: "human.answer", content: "a", question_id: "q1"});
  assert.equal(r.status, 504);
  assert.deepEqual((r.body as {error: unknown}).error, {
    code: "timeout",
    message: "no result in time — it may already have been applied",
    cmd_id: "web-1",
  });
  assert.equal(state.check("q:q1"), "in_flight");
  bus.onResult({dir: "result", id: "web-1", ok: true, queued: true});
  assert.equal(state.pending.has("q:q1"), false);
  assert.equal(hub.notices[0].kind, "late_result");
  assert.equal(hub.notices[0].cmd_id, "web-1");
  assert.equal(hub.notices[0].failed, false);
  // A second late copy is just an orphan.
  bus.onResult({dir: "result", id: "web-1", ok: true});
  assert.equal(hub.notices[1].kind, "orphan_result");
  hub.close();
});

test("UNKNOWN cap evicts keyless entries only; keyed ones expire on time", async () => {
  const {bus, state, hub} = setup({timeoutMs: 5, unknownMs: 60, maxUnknown: 1});
  state.pending.add("q:q1");
  state.pending.add("q:q2");
  await bus.handle(TAB, {type: "human.skip", question_id: "q1"}); // web-1, keyed
  await bus.handle(TAB, {type: "human.send", content: "hi"}); // web-2, keyless
  await bus.handle(TAB, {type: "human.send", content: "again"}); // web-3 evicts web-2, not web-1
  await bus.handle(TAB, {type: "human.skip", question_id: "q2"}); // web-4 evicts web-3, never web-1
  assert.equal(state.check("q:q1"), "in_flight", "keyed entry kept past the cap");
  // Tombstones share the cap: web-3's pushed web-2's out.
  bus.onResult({dir: "result", id: "web-3", ok: true, queued: true});
  bus.onResult({dir: "result", id: "web-2", ok: true, queued: true});
  assert.deepEqual(hub.notices.map((n) => [n.kind, n.level, n.cmd_id]), [
    ["late_result", "warn", "web-3"],
    ["orphan_result", "error", "web-2"],
  ]);
  await new Promise((r) => setTimeout(r, 120));
  assert.equal(state.check("q:q1"), null, "expired");
  assert.equal(state.check("q:q2"), null, "expired");
  hub.close();
});

test("after expiry a late success still releases the key and is a late_result, not an orphan", async () => {
  const {bus, state, hub} = setup({timeoutMs: 5, unknownMs: 20});
  state.pending.add("a:a1");
  await bus.handle(TAB, {type: "approval.resolve", approval_id: "a1", decision: "denied"});
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(state.check("a:a1"), null);
  bus.onResult({dir: "result", id: "web-1", ok: true, queued: true});
  assert.equal(state.pending.has("a:a1"), false);
  assert.deepEqual(hub.notices.map((n) => [n.kind, n.level]), [["late_result", "warn"]]);
  hub.close();
});

test("late failures: Core refusal makes the key answerable, 'no pending question' releases it", async () => {
  const {bus, state, hub} = setup({timeoutMs: 5});
  state.pending.add("a:a1");
  state.pending.add("q:q1");
  await bus.handle(TAB, {type: "approval.resolve", approval_id: "a1", decision: "granted"});
  await bus.handle(TAB, {type: "human.answer", content: "x", question_id: "q1"});
  bus.onResult({dir: "result", id: "web-1", ok: false, error: `boom ${"e".repeat(500)}`});
  bus.onResult({dir: "result", id: "web-2", ok: true, queued: false, error: "no pending question"});
  assert.equal(state.check("a:a1"), null);
  assert.equal(state.pending.has("q:q1"), false);
  const first = hub.notices[0];
  assert.equal(first.text, "web-1 result arrived late: failed");
  assert.equal(String(first.failure).length, 200);
  hub.close();
});

test("keyless 504 touches no state; a retry of a keyed 504 is in_flight", async () => {
  const {bus, state, hub} = setup({timeoutMs: 5});
  assert.equal((await bus.handle(TAB, {type: "human.send", content: "hi"})).status, 504);
  assert.deepEqual(state.calls, []);
  state.pending.add("q:q1");
  assert.equal((await bus.handle(TAB, {type: "human.skip", question_id: "q1"})).status, 504);
  const retry = await bus.handle(TAB, {type: "human.skip", question_id: "q1"});
  assert.equal(retry.status, 409);
  assert.equal(code(retry), "in_flight");
  hub.close();
});

test("child exit makes every waiting or unknown key answerable again", async () => {
  const {bus, state, hub} = setup({timeoutMs: 5});
  state.pending.add("q:q1");
  state.pending.add("a:a1");
  await bus.handle(TAB, {type: "human.skip", question_id: "q1"}); // becomes UNKNOWN
  const waiting = bus.handle(TAB, {type: "approval.resolve", approval_id: "a1", decision: "denied"});
  await tick();
  bus.onChildExit();
  assert.equal((await waiting).status, 502);
  assert.equal(state.check("q:q1"), null);
  assert.equal(state.check("a:a1"), null);
  // Nothing is remembered: a result now is an orphan.
  bus.onResult({dir: "result", id: "web-1", ok: true});
  assert.equal(hub.notices.at(-1)?.kind, "orphan_result");
  hub.close();
});

test("no timers are left behind once commands settle and the child is gone", async () => {
  const timers = () => process.getActiveResourcesInfo().filter((r) => r === "Timeout").length;
  const before = timers();
  const {bus, state, hub} = setup({timeoutMs: 5, unknownMs: 10_000});
  state.pending.add("q:q1");
  await bus.handle(TAB, {type: "human.skip", question_id: "q1"});
  const done = bus.handle(TAB, {type: "session.interrupt"});
  await tick();
  bus.onResult({dir: "result", id: "web-2", ok: true, interrupted: true});
  await done;
  bus.onChildExit();
  hub.close();
  await tick();
  assert.ok(timers() <= before, `timers ${timers()} > ${before}`);
});

test("__proto__ and other stray body keys never reach the child", async () => {
  const {bus, sink, hub} = setup();
  const body = JSON.parse('{"type":"human.send","content":"x","__proto__":{"polluted":1},"constructor":{"x":1}}');
  const r = bus.handle(TAB, body);
  await tick();
  assert.deepEqual(Object.keys(sink.sent[0]).sort(), ["content", "dir", "id", "source", "type"]);
  assert.equal(({} as Record<string, unknown>).polluted, undefined);
  bus.onResult({dir: "result", id: "web-1", ok: true, queued: true});
  await r;
  hub.close();
});

test("human.answer accepts thread_id: null", async () => {
  const {bus, state, sink, hub} = setup();
  state.pending.add("q:q1");
  const r = bus.handle(TAB, {type: "human.answer", content: "x", question_id: "q1", thread_id: null});
  await tick();
  assert.equal(sink.sent[0].thread_id, null);
  bus.onResult({dir: "result", id: "web-1", ok: true, queued: true});
  assert.equal((await r).status, 200);
  hub.close();
});

test("child exit fails waiting commands at once with 502 'may have been applied', not the unsent 503", async () => {
  const {bus, hub, sink} = setup();
  const r = bus.handle(TAB, {type: "session.interrupt"});
  await tick();
  bus.onChildExit();
  const reply = await r;
  assert.equal(reply.status, 502);
  const error = (reply.body as {error: {code: string; message: string; cmd_id: string}}).error;
  assert.equal(error.code, "gateway_exited");
  assert.match(error.message, /may already have been applied/);
  assert.equal(error.cmd_id, "web-1");
  // Never written: still the plain 503.
  sink.up = false;
  const unsent = await bus.handle(TAB, {type: "session.interrupt"});
  assert.equal(unsent.status, 503);
  assert.equal(code(unsent), "gateway_unavailable");
  hub.close();
});

test("late result of an expired N1 never touches the newer N2 on the same key", async () => {
  for (const late of [
    {ok: false, error: "boom"}, // would make N2's card answerable
    {ok: true, queued: true}, // would release N2's card
  ]) {
    const {bus, state, hub} = setup({timeoutMs: 5, unknownMs: 20});
    state.pending.add("q:q1");
    assert.equal((await bus.handle(TAB, {type: "human.answer", content: "a", question_id: "q1"})).status, 504); // N1
    await new Promise((r) => setTimeout(r, 50)); // N1 expires
    assert.equal(state.check("q:q1"), null);
    const n2 = bus.handle(TAB, {type: "human.answer", content: "b", question_id: "q1"}); // N2 in flight
    await tick();
    assert.equal(state.check("q:q1"), "in_flight");
    bus.onResult({dir: "result", id: "web-1", ...late});
    assert.equal(state.check("q:q1"), "in_flight", "N1's late result left N2 alone");
    const notice = hub.notices.at(-1)!;
    assert.deepEqual([notice.kind, notice.level, notice.cmd_id, notice.superseded], ["late_result", "warn", "web-1", true]);
    assert.match(notice.text, /newer command/);
    bus.onResult({dir: "result", id: "web-2", ok: true, queued: true});
    assert.equal((await n2).status, 200);
    assert.equal(state.pending.has("q:q1"), false, "N2 still settles its own key");
    hub.close();
  }
});

test("N1's unknown timer does not reopen a key a newer command owns", async () => {
  const {bus, state, hub} = setup({timeoutMs: 40, unknownMs: 10});
  state.pending.add("a:a1");
  await bus.handle(TAB, {type: "approval.resolve", approval_id: "a1", decision: "denied"}); // N1 → UNKNOWN
  // The state module dropped and re-offered a1 (CommandState may do this), and N2 answers it.
  state.resolving.delete("a:a1");
  const n2 = bus.handle(TAB, {type: "approval.resolve", approval_id: "a1", decision: "granted"});
  await new Promise((r) => setTimeout(r, 20)); // N1's expiry fires while N2 is in flight
  assert.equal(state.check("a:a1"), "in_flight", "only N2 may reopen it");
  assert.equal(state.calls.filter((c) => c === "answerable a:a1").length, 0);
  bus.onResult({dir: "result", id: "web-2", ok: true, queued: true});
  assert.equal((await n2).status, 200);
  assert.equal(state.pending.has("a:a1"), false);
  hub.close();
});

test("an undecodable-line result (id '?') settles the only waiting command", async () => {
  const {bus, state, hub} = setup();
  state.pending.add("q:q1");
  const r = bus.handle(TAB, {type: "human.answer", content: "x", question_id: "q1"});
  await tick();
  bus.onResult({dir: "result", id: "?", ok: false, error: "Expecting value"});
  const reply = await r;
  assert.equal(reply.status, 200);
  const body = reply.body as {id: string; failed: boolean; failure: string};
  assert.deepEqual([body.id, body.failed, body.failure], ["web-1", true, "Expecting value"]);
  assert.equal(state.check("q:q1"), null, "answerable again at once, not after 10s + 60s");
  assert.deepEqual(hub.notices, []);
  hub.close();
});

test("an id '?' result with several waiting commands stays an orphan, with a hint", async () => {
  const {bus, hub} = setup();
  const a = bus.handle(TAB, {type: "session.interrupt"});
  const b = bus.handle(TAB, {type: "human.send", content: "hi"});
  await tick();
  bus.onResult({dir: "result", id: "?", ok: false, error: "bad json"});
  const notice = hub.notices.at(-1)!;
  assert.equal(notice.kind, "orphan_result");
  assert.match(notice.text, /could not read a command line; 2 waiting/);
  bus.onResult({dir: "result", id: "web-1", ok: true});
  bus.onResult({dir: "result", id: "web-2", ok: true, queued: true});
  await Promise.all([a, b]);
  hub.close();
});

test("orphan results are reported; the shutdown quit's result is not", () => {
  const {bus, hub} = setup();
  bus.onResult({dir: "result", id: "?", ok: false, error: "bad json"});
  bus.onResult({dir: "result", id: "web-shutdown", ok: true});
  assert.deepEqual(
    hub.notices.map((n) => [n.kind, n.level, n.cmd_id]),
    [["orphan_result", "error", "?"]],
  );
  hub.close();
});

test("idle human.send that became a turn is logged with mentions, never content", async () => {
  const {bus, logs, hub} = setup();
  const r = bus.handle(TAB, {type: "human.send", content: "secret plan", mentions: ["bob"]});
  await tick();
  bus.onResult({dir: "result", id: "web-1", ok: true, queued: true, as_turn: true});
  await r;
  assert.equal(logs.length, 1);
  assert.match(logs[0], /surface=web, mentions=\["bob"\]/);
  assert.doesNotMatch(logs[0], /secret plan/);
  hub.close();
});

test("HTTP: POST /api/cmd reads X-Augury-Tab and needs the cookie", async () => {
  const {bus, hub, sink} = setup();
  const token = "c".repeat(43);
  const bff = await startServer({token, staticRoot: mkdtempSync(join(tmpdir(), "augury-cmd-")), routes: [bus.route()], onRevoke: () => {}});
  const origin = `http://127.0.0.1:${bff.port}`;
  const call = (path: string, headers: Record<string, string>, body: unknown) =>
    new Promise<{status: number; body: string; headers: Record<string, unknown>}>((resolve, reject) => {
      const req = httpRequest(
        {host: "127.0.0.1", port: bff.port, method: "POST", path, headers: {"Content-Type": "application/json", Origin: origin, ...headers}},
        (res) => {
          let data = "";
          res.setEncoding("utf8");
          res.on("data", (c) => (data += c));
          res.on("end", () => resolve({status: res.statusCode ?? 0, body: data, headers: res.headers}));
        },
      );
      req.on("error", reject);
      req.end(JSON.stringify(body));
    });
  assert.equal((await call("/api/cmd", {"X-Augury-Tab": TAB}, {type: "session.quit"})).status, 401);
  const ex = await call("/api/auth/exchange", {}, {token});
  const cookie = String(ex.headers["set-cookie"]).split(";")[0];
  assert.equal((await call("/api/cmd", {Cookie: cookie}, {type: "session.quit"})).status, 409);
  const pending = call("/api/cmd", {Cookie: cookie, "X-Augury-Tab": TAB}, {type: "session.quit"});
  while (sink.sent.length === 0) {
    await tick();
  }
  bus.onResult({dir: "result", id: sink.sent[0].id, ok: true});
  const done = await pending;
  assert.equal(done.status, 200);
  assert.equal(JSON.parse(done.body).failed, false);
  hub.close();
  await bff.close();
});

const python = resolvePython(process.env);

test("end to end with hello_demo: skip the demo question through the bus", {skip: !existsSync(python) && "no repo .venv"}, async () => {
  const state = new StubState();
  const hub = new NoticeHub();
  const lines: WireMessage[] = [];
  let bus: CommandBus | undefined;
  const gw = new GatewayChild(
    {
      onLine: (line) => {
        const result = forwardChildLine(hub, line);
        if (result) {
          bus!.onResult(result);
        } else if (line.kind === "event") {
          lines.push(line.message);
          if (line.message.type === "human.question") {
            state.pending.add(`q:${String(line.message.question_id)}`);
          }
        }
      },
      onStderr: () => {},
      onExit: () => bus!.onChildExit(),
      onError: () => {},
    },
    {env: {PATH: process.env.PATH}, python},
  );
  bus = new CommandBus({sink: {alive: () => gw.status !== "exited", send: (m) => gw.send(m)}, state, hub, log: () => {}});
  const end = Date.now() + 10_000;
  while (!state.pending.has("q:q-demo-1")) {
    assert.ok(Date.now() < end, "demo question never arrived");
    await new Promise((r) => setTimeout(r, 20));
  }
  const reply = await bus.handle(TAB, {type: "human.skip", question_id: "q-demo-1"});
  assert.equal(reply.status, 200);
  const body = reply.body as {failed: boolean; result: WireMessage};
  assert.equal(body.failed, false);
  assert.equal(body.result.skipped, true);
  assert.equal(state.pending.has("q:q-demo-1"), false);
  assert.equal(await gw.stop(), true);
  assert.deepEqual(hub.notices, []);
  hub.close();
});
