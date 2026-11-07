import assert from "node:assert/strict";
import {test} from "node:test";
import {formatGate} from "../../../ink/src/wire.js";
import {RESUME_HINT, announcement, applyNotice, applyState, applyWire, freshNotice, gateSummary, initialModel, status, type BffState, type Model} from "./model.js";

const ev = (type: string, extra: Record<string, unknown> = {}) => ({dir: "event" as const, type, ...extra});

function state(extra: Partial<BffState> = {}): BffState {
  return {
    seq_last: 0,
    gateway: {status: "running", exit_code: null, signal: null},
    agents: [],
    phase: null,
    gate: null,
    human_approval_pending: null,
    pending_approvals: [],
    pending_questions: [],
    ended: null,
    replay_gap: null,
    is_controller: true,
    has_controller: true,
    ...extra,
  };
}

function run(events: Array<ReturnType<typeof ev>>): Model {
  return events.reduce((m, e, i) => applyWire(m, i + 1, e), initialModel());
}

test("status follows Ink's transitions (F-003)", () => {
  assert.equal(status(initialModel()), "connected");
  assert.equal(status(run([ev("session.started", {agents: ["a"]})])), "ready");
  assert.equal(status(run([ev("session.started"), ev("agent.step", {agent_id: "a", result: {text: "x"}})])), "running");
  assert.equal(status(run([ev("human.question", {question: "?"})])), "ask_user");
  assert.equal(status(run([ev("approval.request", {approval_id: "a1"}), ev("human.question", {question: "?"})])), "approval");
  assert.equal(status(run([ev("approval.request", {approval_id: "a1"}), ev("tool.denied", {reason: "no"})])), "running");
  assert.equal(status(run([ev("session.turn_done", {reason: "interrupted", steps: 1})])), "interrupted");
  assert.equal(status(run([ev("session.turn_done", {reason: "max_steps", steps: 1})])), "idle");
  assert.equal(status(run([ev("log", {text: "run interrupted"})])), "interrupted");
  assert.equal(status(run([ev("session.ended", {reason: "quit"})])), "ended");
});

test("bff.state has the last word: exit, ended, pending (08 §4.3)", () => {
  const asked = run([ev("human.question", {question: "?"})]);
  // Answered from another tab: nothing pending any more → not stuck on ask_user.
  assert.equal(status(applyState(asked, state({seq_last: 1}))), "running");
  assert.equal(status(applyState(initialModel(), state({pending_questions: [{question_id: "q"}]}))), "ask_user");
  assert.equal(status(applyState(initialModel(), state({pending_approvals: [{approval_id: "a"}], pending_questions: [{}]}))), "approval");
  assert.equal(status(applyState(initialModel(), state({ended: {reason: "quit", steps: 1}}))), "ended");
  assert.equal(status(applyState(initialModel(), state({gateway: {status: "exited", exit_code: 1, signal: null}}))), "exited");
});

test("timeline rows use Ink's formatEvent text; message and agent.step are Markdown", () => {
  const m = run([
    ev("session.started", {agents: ["a"]}),
    ev("message", {author: "alice", thread_id: "t1", content: "**hi**"}),
    ev("agent.step", {agent_id: "bob", result: {text: "thinking"}}),
    ev("session.phase", {phase: "P2"}),
    ev("error", {message: "backend down"}),
    ev("made.up"),
  ]);
  assert.deepEqual(
    m.rows.map((r) => [r.kind, r.tone, r.text]),
    [
      ["markdown", "normal", "💬 [alice -> broadcast][t1]\n**hi**"],
      ["markdown", "normal", "💭 bob:\nthinking"],
      ["text", "dim", "[phase] P2"],
      ["text", "error", "[error] backend down"],
      ["text", "normal", "[made.up]"],
    ],
  );
});

test("replayed frames (seq ≤ last) are dropped; a smaller seq_last resets the space", () => {
  let m = run([ev("log", {text: "a"}), ev("log", {text: "b"})]);
  m = applyWire(m, 2, ev("log", {text: "b again"}));
  m = applyNotice(m, 1, {kind: "stderr", level: "warn", text: "old", ts: 0});
  assert.deepEqual(m.rows.map((r) => r.text), ["a", "b"]);
  m = applyState(m, state({seq_last: 1}));
  assert.deepEqual([m.rows.length, m.lastSeq], [0, 0]);
});

test("replay gap becomes one 'skipped' row", () => {
  const m = applyState(initialModel(), state({seq_last: 10, replay_gap: {from: 1, to: 312}}));
  assert.deepEqual(m.rows.map((r) => [r.kind, r.text]), [["gap", "… 이전 312건 생략 (버퍼 초과)"]]);
});

test("resume hint after a finished turn, not after interrupt/planning, never twice in a row", () => {
  const m = run([
    ev("session.turn_done", {reason: "max_steps", steps: 1}),
    ev("session.turn_done", {reason: "max_steps", steps: 1}),
    ev("session.turn_done", {reason: "interrupted", steps: 1}),
    ev("session.turn_done", {reason: "planning", steps: 0}),
  ]);
  assert.deepEqual(m.rows.map((r) => r.text), [RESUME_HINT]);
  assert.doesNotMatch(RESUME_HINT, /Ctrl\+C/);
});

test("notices stay in the timeline with their level", () => {
  const m = applyNotice(initialModel(), 1, {kind: "orphan_result", level: "error", text: "web-9", ts: 0});
  assert.deepEqual(m.rows.map((r) => [r.tone, r.text]), [["error", "[orphan_result] web-9"]]);
});

test("gate summary is Ink's formatGate text, character for character (F-012)", () => {
  const gate = {phase: "P2_SPLIT", thread_name: "plan", approvals: ["a", "b"], pending: ["c"], human_pending: false};
  const m = applyState(initialModel(), state({gate}));
  assert.equal(gateSummary(m), formatGate({dir: "event", type: "session.gate", ...gate}));
  assert.equal(gateSummary(applyState(initialModel(), state())), null);
});

test("announcement: approval first, then a new question, then a gate step", () => {
  const ap = {agent_id: "bob", tool: "run_command"};
  const q = {agent_id: "alice", question_id: "q1"};
  assert.equal(announcement(ap, q, {phase: "P2"}), "bob 이 run_command 승인을 요청했습니다");
  assert.equal(announcement(null, q, {phase: "P2"}), "alice 이 질문했습니다 (q1)");
  assert.equal(announcement(null, {agent_id: null, question_id: "q2"}, null), "에이전트 이 질문했습니다 (q2)");
  assert.equal(announcement(null, null, {phase: "P2"}), "사람 승인이 필요한 단계입니다 · P2");
  assert.equal(announcement(null, null, {phase: null}), "사람 승인이 필요한 단계입니다");
  assert.equal(announcement(null, null, null), "");
});

test("rows already pushed keep their identity, so a memoized row does not re-render", () => {
  const before = run([ev("message", {from: "a", text: "one"}), ev("log", {text: "two"})]);
  const after = applyWire(before, 3, ev("message", {from: "b", text: "three"}));
  assert.equal(after.rows.length, before.rows.length + 1);
  before.rows.forEach((row, i) => assert.equal(after.rows[i], row));
});

test("toast/auth de-dup: nothing before the first state; a gap replay does not pop old notices again", () => {
  const shown = {current: Number.POSITIVE_INFINITY};
  assert.equal(freshNotice(shown, 3), false); // replay before bff.state
  shown.current = 5; // first bff.state: seq_last = 5
  assert.equal(freshNotice(shown, 5), false);
  assert.equal(freshNotice(shown, 7), true);
  assert.equal(freshNotice(shown, 9), true);
  // Gap replay re-sends everything from the start, then continues.
  for (const seq of [6, 7, 8, 9]) assert.equal(freshNotice(shown, seq), false, String(seq));
  assert.equal(freshNotice(shown, 10), true);
  assert.equal(shown.current, 10);
});
