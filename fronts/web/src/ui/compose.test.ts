import assert from "node:assert/strict";
import {test} from "node:test";
import {STALE_TEXT, gateSignal, hasSignal, outcome, plan, startHold, stripGateSignal, type Context} from "./compose.js";

const ctx = (extra: Partial<Context> = {}): Context => ({agents: ["alice", "bob"], question: null, gateThread: null, ...extra});

test("plan: empty, /quit opens the dialog, /skip needs a question", () => {
  assert.deepEqual(plan("   ", ctx()), {kind: "empty"});
  assert.deepEqual(plan("/quit", ctx()), {kind: "quit"});
  assert.deepEqual(plan("/exit", ctx()), {kind: "quit"});
  assert.equal(plan("/skip", ctx()).kind, "blocked");
  const skip = plan("/skip", ctx({question: {question_id: "q1", thread_id: "t", agent_id: "bob"}}));
  assert.deepEqual(skip.kind === "send" && skip.body, {type: "human.skip", question_id: "q1"});
});

test("plan: mentions — known kept, unknown warned, only-unknown blocked (Ink parity)", () => {
  const ok = plan("@alice look", ctx());
  assert.deepEqual(ok.kind === "send" && ok.body, {type: "human.send", content: "@alice look", mentions: ["alice"]});
  assert.equal(ok.kind === "send" && ok.echo, "> @alice look → @alice");
  const mixed = plan("@alice @carol hi", ctx());
  assert.ok(mixed.kind === "send" && mixed.warning?.includes("@carol"));
  assert.deepEqual(mixed.kind === "send" && mixed.body.mentions, ["alice"]);
  const blocked = plan("@carol hi", ctx());
  assert.equal(blocked.kind, "blocked");
  // Empty roster (still planning): every well-formed @id passes.
  const any = plan("@zed hi", ctx({agents: []}));
  assert.deepEqual(any.kind === "send" && any.body.mentions, ["zed"]);
  // An email address is not a mention.
  assert.deepEqual(plan("mail a@b.com", ctx()).kind === "send" && (plan("mail a@b.com", ctx()) as {body: object}).body, {type: "human.send", content: "mail a@b.com"});
});

test("plan: a pending question turns input into human.answer with its ids and mentions (CP-6)", () => {
  const p = plan("@bob yes", ctx({question: {question_id: "q7", thread_id: "t1", agent_id: "bob"}}));
  assert.deepEqual(p.kind === "send" && p.body, {type: "human.answer", content: "@bob yes", question_id: "q7", thread_id: "t1", mentions: ["bob"]});
});

test("plan: APPROVE:/REJECT: go to the gate's thread; other text does not (F-013)", () => {
  const g = ctx({gateThread: "t-gate"});
  assert.deepEqual(plan("APPROVE: looks good", g).kind === "send" && (plan("APPROVE: looks good", g) as {body: object}).body, {
    type: "human.send",
    content: "APPROVE: looks good",
    thread_id: "t-gate",
  });
  assert.equal((plan("approve it", g) as {body: Record<string, unknown>}).body.thread_id, undefined);
  assert.equal((plan("APPROVE: x", ctx()) as {body: Record<string, unknown>}).body.thread_id, undefined);
  // A bare prefix would pass the gate with no reason: blocked.
  assert.equal(plan("APPROVE:", g).kind, "blocked");
  assert.equal(plan("  REJECT:  ", g).kind, "blocked");
});

test("outcome: success clears; idle turn is flagged; Core refusal keeps the text", () => {
  assert.deepEqual(outcome("human.send", {kind: "ok", result: {ok: true, queued: true}, failed: false, failure: null}), {clear: true, toast: null, asTurn: false});
  assert.equal(outcome("human.send", {kind: "ok", result: {as_turn: true}, failed: false, failure: null}).asTurn, true);
  const refused = outcome("human.answer", {kind: "ok", result: {}, failed: true, failure: "no pending question"});
  assert.deepEqual([refused.clear, refused.toast?.text], [false, "전송 실패: no pending question"]);
  assert.equal(outcome("human.send", {kind: "network"}).clear, false);
});

test("outcome: 504 — keyed commands wait for the result, keyless ones warn about duplicates (08 §6.2)", () => {
  const keyed = outcome("approval.resolve", {kind: "http", status: 504, code: "timeout", cmdId: "web-4"});
  assert.deepEqual([keyed.clear, keyed.toast?.text], [true, "결과 확인 중 — 이미 처리됐을 수 있습니다 (web-4)"]);
  const keyless = outcome("human.send", {kind: "http", status: 504, code: "timeout", cmdId: "web-5"});
  assert.equal(keyless.clear, false, "keep the text: resending may duplicate");
  assert.match(keyless.toast!.text, /중복/);
});

test("outcome: quit racing the child's exit is not an error; other 503s are", () => {
  assert.deepEqual(outcome("session.quit", {kind: "http", status: 503, code: "gateway_unavailable", cmdId: null}), {clear: true, toast: null, asTurn: false});
  assert.equal(outcome("session.interrupt", {kind: "http", status: 503, code: "gateway_unavailable", cmdId: null}).toast?.level, "error");
  assert.equal(outcome("human.send", {kind: "http", status: 409, code: "not_controller", cmdId: null}).toast?.level, "warn");
  assert.equal(outcome("human.skip", {kind: "http", status: 409, code: "not_pending", cmdId: null}).clear, true);
});

test("gate prefix: same rule as Core has_signal — decoration and case ignored (signals.py)", () => {
  assert.ok(hasSignal("approve: ok", "APPROVE:"));
  assert.ok(hasSignal("**APPROVE:** ok", "APPROVE:"));
  assert.ok(hasSignal("  [#-`_(REJECT: no", "REJECT:"));
  assert.ok(!hasSignal("> APPROVE: quoted", "APPROVE:"), "blockquote is a citation, not a vote");
  assert.ok(!hasSignal("APPROVED", "APPROVE:"), "the colon is required");
  assert.ok(!hasSignal("x", ""));
  assert.equal(gateSignal("**reject:** why"), "REJECT:");
  assert.equal(gateSignal("hello"), null);
  assert.equal(stripGateSignal("**APPROVE:** ok"), "ok");
  assert.equal(stripGateSignal("approve: ok"), "ok");
  assert.equal(stripGateSignal("plain"), "plain");
});

test("plan: decorated or lower-case gate prefixes go to the gate thread; a bare one is blocked", () => {
  const g = ctx({gateThread: "t-gate"});
  for (const text of ["approve: ok", "**APPROVE:** ok", "# REJECT: needs tests"]) {
    const p = plan(text, g);
    assert.equal(p.kind === "send" && p.body.thread_id, "t-gate", text);
  }
  for (const text of ["**APPROVE:**", "approve:", "[REJECT:]", "`APPROVE:`"]) {
    assert.equal(plan(text, g).kind, "blocked", text);
  }
});

test("outcome: 502 gateway_exited — quit is done; other commands may have landed", () => {
  const r = {kind: "http", status: 502, code: "gateway_exited", cmdId: null} as const;
  assert.deepEqual(outcome("session.quit", r), {clear: true, toast: null, asTurn: false});
  assert.equal(outcome("human.send", r).toast?.level, "warn");
  assert.equal(outcome("human.send", r).clear, false);
});

test("outcome: stale_revision keeps nothing decided and asks to look again", () => {
  const o = outcome("approval.resolve", {kind: "http", status: 409, code: "stale_revision", cmdId: null});
  assert.deepEqual(o, {clear: false, toast: {level: "warn", text: STALE_TEXT}, asTurn: false});
  assert.match(STALE_TEXT, /바뀌었습니다/);
});

test("startHold: Start stays locked while sent or maybe sent, released on a known failure", () => {
  assert.equal(startHold({kind: "ok", result: {}, failed: false, failure: null}), true);
  assert.equal(startHold({kind: "http", status: 504, code: "timeout", cmdId: "web-1"}), true, "504: result unknown");
  assert.equal(startHold({kind: "ok", result: {}, failed: true, failure: "x"}), false);
  assert.equal(startHold({kind: "http", status: 503, code: "gateway_unavailable", cmdId: null}), false);
  assert.equal(startHold({kind: "network"}), false);
});
