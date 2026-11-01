/** What the composer sends, and what the UI does with the answer (WBS 3.4, 09 §3.7). */

import {parseHumanMentions} from "../../../ink/src/mentions.js";
import type {CmdResult} from "./connection.js";

export type Pending = {question_id: string; thread_id: string | null; agent_id: string | null};

export type Plan =
  | {kind: "empty"}
  | {kind: "quit"} // opens the Quit dialog (09 §3.7)
  | {kind: "blocked"; warning: string}
  | {kind: "send"; body: Record<string, unknown>; echo: string; warning: string | null};

export type Context = {
  agents: readonly string[];
  /** The question being answered (first pending, FIFO), if any. */
  question: Pending | null;
  /** Thread a human-approval gate waits on (APPROVE:/REJECT: go there — F-013). */
  gateThread: string | null;
};

/** Ink ``onSubmit`` parity, minus approvals: the Web approves on the card only (N-012). */
export function plan(text: string, ctx: Context): Plan {
  const trimmed = text.trim();
  if (!trimmed) {
    return {kind: "empty"};
  }
  if (trimmed === "/quit" || trimmed === "/exit") {
    return {kind: "quit"};
  }
  // A bare gate prefix would pass a human-approval gate with no reason given.
  if (/^(APPROVE|REJECT):$/.test(trimmed)) {
    return {kind: "blocked", warning: "APPROVE:/REJECT: 뒤에 내용을 적어 주세요"};
  }
  if (trimmed === "/skip") {
    return ctx.question
      ? {kind: "send", body: {type: "human.skip", question_id: ctx.question.question_id}, echo: "> /skip", warning: null}
      : {kind: "blocked", warning: "건너뛸 질문이 없습니다"};
  }
  const {mentions, unknown} = parseHumanMentions(trimmed, ctx.agents);
  const warning =
    unknown.length > 0
      ? `모르는 이름: ${unknown.map((u) => `@${u}`).join(", ")}${ctx.agents.length ? ` (알려진 이름: ${ctx.agents.join(", ")})` : ""}`
      : null;
  // Typed @… but none matched: do not broadcast by accident (Ink App.tsx).
  if (unknown.length > 0 && mentions.length === 0) {
    return {kind: "blocked", warning: warning!};
  }
  const to = mentions.length ? ` → ${mentions.map((m) => `@${m}`).join(" ")}` : "";
  const echo = `> ${trimmed}${to}`;
  if (ctx.question) {
    return {
      kind: "send",
      body: {
        type: "human.answer",
        content: trimmed,
        question_id: ctx.question.question_id,
        thread_id: ctx.question.thread_id,
        ...(mentions.length ? {mentions} : {}),
      },
      echo,
      warning,
    };
  }
  const gate = ctx.gateThread && /^(APPROVE|REJECT):/.test(trimmed) ? {thread_id: ctx.gateThread} : {};
  return {kind: "send", body: {type: "human.send", content: trimmed, ...gate, ...(mentions.length ? {mentions} : {})}, echo, warning};
}

/** Commands whose retry is safe after a 504: they answer a pending item (08 §6.2). */
const KEYED = new Set(["human.answer", "human.skip", "approval.resolve"]);

export type Outcome = {
  /** Clear the composer (the command is known to have landed). */
  clear: boolean;
  toast: {level: "info" | "warn" | "error"; text: string} | null;
  /** Idle input became a new turn: mentions/thread were not delivered (DW-6). */
  asTurn: boolean;
};

/** One place that turns an API-05 reply into UI feedback (F-017, 09 B-8/B-9). */
export function outcome(type: string, r: CmdResult): Outcome {
  if (r.kind === "network") {
    return {clear: false, toast: {level: "error", text: "전송 실패: BFF 에 연결할 수 없습니다"}, asTurn: false};
  }
  if (r.kind === "ok") {
    if (r.failed) {
      return {clear: false, toast: {level: "error", text: `전송 실패: ${r.failure ?? "rejected"}`}, asTurn: false};
    }
    return {clear: true, toast: null, asTurn: r.result.as_turn === true};
  }
  switch (r.code) {
    case "timeout":
      return KEYED.has(type)
        ? {clear: true, toast: {level: "warn", text: `결과 확인 중 — 이미 처리됐을 수 있습니다 (${r.cmdId ?? "?"})`}, asTurn: false}
        : {clear: false, toast: {level: "warn", text: "이미 전달됐을 수 있습니다 — 다시 보내면 중복될 수 있습니다"}, asTurn: false};
    case "gateway_unavailable":
      // quit races the child's exit: the gateway state shows the real outcome (08 §6.4).
      return type === "session.quit"
        ? {clear: true, toast: null, asTurn: false}
        : {clear: false, toast: {level: "error", text: "세션이 실행 중이 아닙니다"}, asTurn: false};
    case "not_controller":
      return {clear: false, toast: {level: "warn", text: "이 탭은 읽기 전용입니다"}, asTurn: false};
    case "in_flight":
      return {clear: true, toast: {level: "info", text: "결과 확인 중입니다"}, asTurn: false};
    case "not_pending":
      return {clear: true, toast: {level: "info", text: "이미 처리된 항목입니다"}, asTurn: false};
    default:
      return {clear: false, toast: {level: "error", text: `전송 실패: ${r.code}`}, asTurn: false};
  }
}
