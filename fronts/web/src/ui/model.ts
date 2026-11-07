/** UI state model (WBS 3.1/3.2): pure functions, no DOM — unit-tested in Node.
 *
 * Timeline rows and the run-status chip follow Ink (``fronts/ink/src/App.tsx``):
 * the same ``formatEvent`` text, the same status set (F-003, F-015). Pending
 * questions/approvals, ``ended`` and the gateway always come from ``bff.state``
 * (08 §4.3 — replayed frames never resurrect a card).
 */

import {formatEvent, formatGate, type WireMessage} from "../../../ink/src/wire.js";

export type Status =
  | "connected"
  | "ready"
  | "running"
  | "idle"
  | "approval"
  | "ask_user"
  | "interrupted"
  | "ended"
  | "exited";

export type Tone = "normal" | "dim" | "warn" | "error";

export type Row = {
  /** Unique key: the SSE seq, or a negative local id for rows the BFF never sent. */
  key: number;
  kind: "text" | "markdown" | "gap";
  text: string;
  tone: Tone;
};

export type Notice = {kind: string; level: "info" | "warn" | "error"; text: string; ts: number; [k: string]: unknown};

/** ``bff.state`` as sent by the BFF (08 §4.4); unknown extra fields are ignored. */
export type BffState = {
  seq_last: number;
  gateway: {status: "starting" | "running" | "exited"; exit_code: number | null; signal: string | null};
  agents: string[];
  phase: string | null;
  gate: Record<string, unknown> | null;
  human_approval_pending: {phase: string | null; thread_id: string | null; text: string | null} | null;
  pending_approvals: Array<Record<string, unknown>>;
  pending_questions: Array<Record<string, unknown>>;
  ended: {reason: string | null; steps: number | null} | null;
  replay_gap: {from: number; to: number} | null;
  is_controller: boolean;
  has_controller: boolean;
};

export type Model = {
  rows: Row[];
  /** Agents have run at least once (agent.step / turn_done) — hides Start (F-011). */
  started: boolean;
  /** Highest seq applied; frames at or below it are replay duplicates (F-015). */
  lastSeq: number;
  /** Status from the event stream alone; ``status()`` overlays ``bff.state``. */
  base: Status;
  state: BffState | null;
  nextLocal: number;
};

// No Ctrl+C here (CP-7): the Web ends a session with the Quit button.
export const RESUME_HINT = "에이전트가 멈췄습니다. 메시지를 보내면 이어서 진행하고, 끝내려면 종료 버튼을 누르세요.";

export function initialModel(): Model {
  return {rows: [], started: false, lastSeq: 0, base: "connected", state: null, nextLocal: -1};
}

function push(m: Model, row: Omit<Row, "key">, key?: number): Model {
  const k = key ?? m.nextLocal;
  return {...m, rows: [...m.rows, {...row, key: k}], nextLocal: key === undefined ? m.nextLocal - 1 : m.nextLocal};
}

/** Ink's status transitions for one event (App.tsx), before the bff.state overlay. */
function nextBase(base: Status, e: WireMessage): Status {
  switch (e.type) {
    case "session.started":
      return "ready";
    case "session.turn_done":
      return String(e.reason ?? "") === "interrupted" ? "interrupted" : "idle";
    case "session.ended":
      return "ended";
    case "approval.request":
      return "approval";
    case "approval.resolved":
    case "approval.granted":
    case "approval.expired":
    case "tool.denied":
      return base === "approval" ? "running" : base;
    case "human.question":
      return base === "approval" ? base : "ask_user";
    case "log":
      // Ink compatibility rule (CP-3): turn_done(reason=interrupted) is the real signal.
      return String(e.text ?? "").includes("interrupted") ? "interrupted" : base;
    case "agent.step":
    case "message":
    case "tool":
      // Agents are visibly working (Ink only learns this from its own input).
      return base === "connected" || base === "ready" || base === "idle" || base === "interrupted" ? "running" : base;
    default:
      return base;
  }
}

/** Apply one ``wire`` frame (08 §4.2). Duplicates by seq are dropped. */
export function applyWire(m: Model, seq: number, e: WireMessage): Model {
  if (seq <= m.lastSeq) {
    return m;
  }
  const started = m.started || e.type === "agent.step" || e.type === "session.turn_done";
  let out: Model = {...m, lastSeq: seq, base: nextBase(m.base, e), started};
  const line = formatEvent(e);
  if (line) {
    const markdown = e.type === "agent.step" || e.type === "message";
    const tone: Tone = e.type === "error" ? "error" : e.type === "log" || e.type === "session.phase" ? "dim" : "normal";
    out = push(out, {kind: markdown ? "markdown" : "text", text: line, tone}, seq);
  }
  if (e.type === "session.turn_done") {
    const reason = String(e.reason ?? "");
    const last = out.rows[out.rows.length - 1];
    if (reason !== "interrupted" && reason !== "planning" && last?.text !== RESUME_HINT) {
      out = push(out, {kind: "text", text: RESUME_HINT, tone: "dim"});
    }
  }
  return out;
}

/** Apply one ``bff.notice`` frame: it also stays in the timeline (09 §3.2). */
export function applyNotice(m: Model, seq: number, n: Notice): Model {
  if (seq <= m.lastSeq) {
    return m;
  }
  const tone: Tone = n.level === "error" ? "error" : n.level === "warn" ? "warn" : "dim";
  return push({...m, lastSeq: seq}, {kind: "text", text: `[${n.kind}] ${n.text}`, tone}, seq);
}

/** The user's own input (Ink echoes it too). Only this tab sees it: Wire has no echo (09 §3.3). */
export function applyEcho(m: Model, text: string, note: string | null): Model {
  const row = push(m, {kind: "text", text: note ? `${text}    ⓘ ${note}` : text, tone: "normal"});
  // Ink: sending input means the agents run (App.tsx onSubmit).
  const base = m.base === "connected" || m.base === "ready" || m.base === "idle" || m.base === "interrupted" ? "running" : m.base;
  return {...row, base};
}

/** Apply a ``bff.state`` frame: the authority for pending, ended and gateway. */
export function applyState(m: Model, s: BffState): Model {
  let out = m;
  // A smaller seq_last than what we have means another seq space (BFF restarted).
  if (s.seq_last < m.lastSeq) {
    out = {...initialModel()};
  }
  if (s.replay_gap) {
    const missed = s.replay_gap.to - s.replay_gap.from + 1;
    out = push(out, {kind: "gap", text: `… 이전 ${missed}건 생략 (버퍼 초과)`, tone: "dim"});
  }
  return {...out, state: s};
}

/** The chip value (F-003): events give the trend, ``bff.state`` has the last word. */
export function status(m: Model): Status {
  const s = m.state;
  if (s?.gateway.status === "exited") {
    return "exited";
  }
  if (s?.ended) {
    return "ended";
  }
  if (s && s.pending_approvals.length > 0) {
    return "approval";
  }
  if (s && s.pending_questions.length > 0) {
    return "ask_user";
  }
  if (s && (m.base === "approval" || m.base === "ask_user")) {
    return "running"; // answered elsewhere; nothing pending any more
  }
  return m.base;
}

/** Status-bar gate summary — Ink's ``formatGate`` text, character for character (F-012). */
export function gateSummary(m: Model): string | null {
  const gate = m.state?.gate;
  return gate ? formatGate({dir: "event", type: "session.gate", ...gate}) : null;
}

export const STATUS_LABEL: Record<Status, string> = {
  connected: "○ connected",
  ready: "◐ ready",
  running: "● running",
  idle: "◐ idle",
  approval: "⚑ approval",
  ask_user: "? ask_user",
  interrupted: "‖ interrupted",
  ended: "■ ended",
  exited: "✕ gateway exit",
};

/** The sr-only live region's text: the item that now waits for the human, top first (09 §6). */
export function announcement(
  approval: {agent_id: string; tool: string} | null,
  question: {agent_id: string | null; question_id: string} | null,
  gate: {phase: string | null} | null,
): string {
  if (approval) return `${approval.agent_id} 이 ${approval.tool} 승인을 요청했습니다`;
  if (question) return `${question.agent_id ?? "에이전트"} 이 질문했습니다 (${question.question_id})`;
  if (gate) return `사람 승인이 필요한 단계입니다${gate.phase ? ` · ${gate.phase}` : ""}`;
  return "";
}
