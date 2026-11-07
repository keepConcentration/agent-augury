/** Web Surface app shell (WBS 3.1) and timeline (3.2) — 09 §2·§3.1~3.3·§3.9. */

import {memo, useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState, type ReactNode} from "react";
import {authLink} from "./auth.js";
import {START_UNKNOWN_MS, STALE_TEXT, outcome, plan, startHold, type Outcome} from "./compose.js";
import {claim, openStream, sendCommand, session, startOnce, type CmdResult, type Session} from "./connection.js";
import {
  ApprovalCard,
  Composer,
  ControlBar,
  GateNotice,
  QuestionPanel,
  QuitDialog,
  type Approval,
  type Can,
  type ComposerHandle,
  type Question,
} from "./controls.js";
import {escAction, isCardShortcut} from "./keys.js";
import {Markdown} from "./markdown.js";
import {VirtualList, type ListHandle} from "./virtual.js";
import {
  STATUS_LABEL,
  announcement,
  applyEcho,
  applyNotice,
  applyState,
  applyWire,
  freshNotice,
  gateSummary,
  initialModel,
  status,
  type Model,
  type Notice,
  type Row,
} from "./model.js";

type Action =
  | {type: "wire"; seq: number; e: Parameters<typeof applyWire>[2]}
  | {type: "notice"; seq: number; n: Notice}
  | {type: "state"; s: NonNullable<Model["state"]>}
  | {type: "echo"; text: string; note: string | null};

function reduce(m: Model, a: Action): Model {
  if (a.type === "wire") return applyWire(m, a.seq, a.e);
  if (a.type === "echo") return applyEcho(m, a.text, a.note);
  if (a.type === "notice") return applyNotice(m, a.seq, a.n);
  return applyState(m, a.s);
}

/** Full-screen states (09 B-5, B-6) and the stream link (B-4). */
type Phase = "starting" | "live" | "reconnecting" | "expired" | "too_many" | "replaced" | "failed";

/** OAuth notice: a link only for an allowlisted host, always shown with that host (09 B-1). */
function AuthText({text}: {text: string}) {
  const link = authLink(text);
  if (!link) return <>{text}</>;
  return (
    <>
      {link.before}
      <a href={link.url} target="_blank" rel="noopener noreferrer">
        {link.url}
      </a>{" "}
      <span className="host">({link.host})</span>
      {link.after}
    </>
  );
}

/** Rows are immutable once pushed, so only new rows render when an event arrives. */
export const TimelineRow = memo(function TimelineRow({row}: {row: Row}) {
  const className = `row tone-${row.tone}${row.kind === "gap" ? " gap" : ""}`;
  return row.kind === "markdown" ? (
    <div className={className}>
      <Markdown text={row.text} />
    </div>
  ) : (
    <div className={className}>{row.text}</div>
  );
});

const renderRow = (row: Row) => <TimelineRow row={row} />;

/** Follows the bottom unless the user scrolled up; then counts what arrived (09 §3.3).
 * Only rows near the viewport are in the DOM (3.3, N-008 — see virtual.tsx). */
function Timeline({rows, dock}: {rows: Row[]; dock: ReactNode}) {
  const list = useRef<ListHandle | null>(null);
  const following = useRef(true);
  const seen = useRef(rows.length);
  const [unseen, setUnseen] = useState(0);
  useLayoutEffect(() => {
    if (following.current) {
      seen.current = rows.length;
    } else {
      setUnseen(rows.length - seen.current);
    }
  }, [rows.length]);
  const onFollowChange = useCallback(
    (f: boolean) => {
      following.current = f;
      if (f) {
        seen.current = rows.length;
        setUnseen(0);
      }
    },
    [rows.length],
  );
  const toBottom = () => list.current?.toBottom();
  return (
    <div className="timeline-wrap">
      <VirtualList
        items={rows}
        render={renderRow}
        handle={list}
        onFollowChange={onFollowChange}
        boxProps={{
          className: "timeline",
          tabIndex: 0,
          "aria-label": "타임라인",
          onKeyDown: (ev) => ev.key === "End" && toBottom(),
        }}
      />
      {unseen > 0 && (
        <button className="new-chip" onClick={toBottom}>
          ↓ 새 이벤트 {unseen}건
        </button>
      )}
      {/* Cards float over the timeline's bottom: the composer below never moves (10 M1). */}
      {dock && <div className="dock">{dock}</div>}
    </div>
  );
}

function StatusBar({model, phase}: {model: Model; phase: Phase}) {
  const st = status(model);
  const s = model.state;
  const label = st === "exited" ? `${STATUS_LABEL.exited} ${s?.gateway.exit_code ?? "?"}` : STATUS_LABEL[st];
  const agents = s?.agents ?? [];
  const shown = agents.slice(0, 5).map((a) => `@${a}`).join(" ") + (agents.length > 5 ? ` +${agents.length - 5}` : "");
  const gw = s?.gateway.status === "exited" ? "gw ✕" : s?.gateway.status === "running" ? "gw ●" : "gw ◌";
  return (
    <header className="statusbar">
      <span className={`chip st-${st}${phase === "reconnecting" ? " stale" : ""}`} aria-live="polite">
        {phase === "reconnecting" ? "⟳ 재연결 중" : label}
      </span>
      <span className="gate">{gateSummary(model) ?? ""}</span>
      <span className="roster" title={agents.join(", ")}>
        {shown}
      </span>
      <span className="gw">{gw}</span>
      <span className="tab">{s?.is_controller ? "✎ 조작 탭" : "👁 읽기 전용"}</span>
    </header>
  );
}

function ClaimDialog({onCancel, onConfirm}: {onCancel: () => void; onConfirm: () => void}) {
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => cancel.current?.focus(), []);
  return (
    <div className="backdrop" role="dialog" aria-modal="true" aria-labelledby="claim-title" onKeyDown={(ev) => ev.key === "Escape" && onCancel()}>
      <div className="dialog">
        <h2 id="claim-title">조작 탭 가져오기</h2>
        <p>다른 탭이 조작 중입니다. 이 탭으로 가져오면 그 탭은 읽기 전용이 됩니다. 그 탭에서 보내던 명령은 그대로 처리됩니다.</p>
        <div className="actions">
          <button ref={cancel} onClick={onCancel}>
            취소 (Esc)
          </button>
          <button className="primary" onClick={onConfirm}>
            가져오기
          </button>
        </div>
      </div>
    </div>
  );
}

type Toast = {id: number; level: Notice["level"]; text: string};

const MAX_TOASTS = 5;

export function App() {
  const [model, dispatch] = useReducer(reduce, undefined, initialModel);
  const [phase, setPhase] = useState<Phase>("starting");
  const [tab, setTab] = useState("");
  const [info, setInfo] = useState<Session | null>(null);
  const [auth, setAuth] = useState<string[]>([]);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [dialog, setDialog] = useState(false);
  const [quitOpen, setQuitOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [starting, setStarting] = useState(false);
  const [warning, setWarning] = useState<string | null>(null);
  /** Approvals whose command got a 504: the card says "result unknown" (08 §6.2). */
  const [unknownIds, setUnknownIds] = useState<ReadonlySet<string>>(new Set());
  /** ``rev`` null: shown whatever revision the card has now (stale_revision). */
  const [cardError, setCardError] = useState<{id: string; rev: number | null; text: string} | null>(null);
  /** Bumped to drop the card's confirm step from outside the card. */
  const [cardReset, setCardReset] = useState(0);
  const composer = useRef<ComposerHandle>(null);
  const card = useRef<HTMLDivElement>(null);
  const questionPanel = useRef<HTMLDivElement>(null);
  const toastId = useRef(0);
  // Highest notice seq already shown as a toast / auth line. Until the first
  // bff.state everything is replay; a gap replay re-sends old seqs (08 §4.3).
  const toasted = useRef(Number.POSITIVE_INFINITY);

  const toast = useCallback((level: Toast["level"], text: string) => {
    const id = ++toastId.current;
    setToasts((all) => [...all.slice(-(MAX_TOASTS - 1)), {id, level, text}]);
    setTimeout(() => setToasts((all) => all.filter((t) => t.id !== id)), 5000);
  }, []);

  useEffect(() => {
    let close: (() => void) | undefined;
    let cancelled = false;
    void (async () => {
      // Shared across StrictMode's double effect: one token take, one exchange.
      const begun = await startOnce();
      if (cancelled) return;
      if (begun.kind !== "ok") return setPhase(begun.kind);
      const id = begun.tab;
      setTab(id);
      setInfo(begun.info);
      close = openStream(id, {
        wire: (seq, e) => dispatch({type: "wire", seq, e}),
        notice: (seq, n) => {
          dispatch({type: "notice", seq, n});
          // Replayed notices stay in the timeline but do not pop up again.
          if (!freshNotice(toasted, seq)) return;
          if (n.kind === "auth") setAuth((lines) => [...lines.slice(-3), n.text]);
          else if (n.kind !== "dev_mode") toast(n.level, n.text);
        },
        state: (s) => {
          if (toasted.current === Number.POSITIVE_INFINITY) toasted.current = s.seq_last;
          dispatch({type: "state", s});
        },
        link: (state) => {
          if (state === "open") return setPhase("live");
          if (state === "reconnecting") return setPhase("reconnecting");
          if (state === "replaced") return setPhase("replaced");
          // The browser gave up (non-200): ask API-03 why (08 API-04).
          void session(id)
            .then((s) => setPhase(s === null ? "expired" : s.streams_available === 0 ? "too_many" : "failed"))
            .catch(() => setPhase("failed"));
        },
      });
    })();
    return () => {
      cancelled = true;
      close?.();
    };
  }, [toast]);

  const takeControl = async (force: boolean) => {
    setDialog(false);
    const got = await claim(tab, force).catch(() => undefined);
    if (got === null) toast("warn", "연결을 복구한 뒤 다시 시도하세요");
    else if (got === false) setDialog(true);
    else if (got) toast("info", "이 탭에서 조작합니다");
  };

  const s = model.state;
  const st = status(model);
  const can: Can =
    phase !== "live"
      ? {act: false, reason: phase === "reconnecting" ? "연결 복구 후 사용할 수 있습니다" : "연결 중…"}
      : !s
        ? {act: false, reason: "연결 중…"}
        : s.gateway.status === "exited"
          ? {act: false, reason: `세션 프로세스가 끝났습니다 (exit ${s.gateway.exit_code ?? "?"})`}
          : s.ended
            ? {act: false, reason: "세션이 끝났습니다"}
            : !s.is_controller
              ? {act: false, reason: "👁 읽기 전용 탭입니다"}
              : {act: true, reason: null};
  const approvals = (s?.pending_approvals ?? []) as unknown as Approval[];
  const questions = (s?.pending_questions ?? []) as unknown as Question[];
  const approval = approvals[0] ?? null;
  const question = questions[0] ?? null;

  /** One command through API-05, with the shared feedback rules (compose.outcome). */
  const run = async (body: Record<string, unknown>, echo: string | null = null): Promise<{o: Outcome; r: CmdResult}> => {
    setBusy(true);
    try {
      const type = String(body.type);
      const r = await sendCommand(tab, body);
      const o = outcome(type, r);
      if (o.toast) toast(o.toast.level, o.toast.text);
      if (r.kind === "http" && r.code === "timeout" && type === "approval.resolve") {
        setUnknownIds((ids) => new Set([...ids, String(body.approval_id)]));
      }
      if (r.kind === "ok" && r.failed && type === "approval.resolve") {
        setCardError({id: String(body.approval_id), rev: Number(body.revision), text: r.failure ?? "rejected"});
      }
      if (r.kind === "http" && r.code === "stale_revision" && type === "approval.resolve") {
        // What the user confirmed is no longer what is pending: start over on the new content.
        setCardReset((n) => n + 1);
        setCardError({id: String(body.approval_id), rev: null, text: STALE_TEXT});
      }
      // Echo only what is known to have landed: not a 504, not "already handled".
      if (r.kind === "ok" && !r.failed && echo) {
        dispatch({type: "echo", text: echo, note: o.asTurn ? "idle 입력: 멘션·스레드 미전달" : null});
      }
      return {o, r};
    } finally {
      setBusy(false);
    }
  };
  const send = async (body: Record<string, unknown>, echo: string | null = null): Promise<boolean> => (await run(body, echo)).o.clear;

  const submit = async (text: string): Promise<boolean> => {
    const p = plan(text, {
      agents: s?.agents ?? [],
      question: question ? {question_id: question.question_id, thread_id: question.thread_id, agent_id: question.agent_id} : null,
      gateThread: s?.human_approval_pending?.thread_id ?? null,
    });
    if (p.kind === "empty") return false;
    if (p.kind === "quit") {
      setQuitOpen(true);
      return true;
    }
    if (p.kind === "blocked") {
      setWarning(p.warning);
      return false;
    }
    setWarning(p.warning);
    return send(p.body, p.echo);
  };

  /** *revision* is the one the card showed: the BFF refuses it if the request changed since. */
  const decide = (d: "granted" | "denied", revision: number) => {
    if (!approval || busy) return; // no second resolve while the first is in the air
    setCardError(null);
    void send(
      {type: "approval.resolve", approval_id: approval.approval_id, decision: d, revision},
      `> approval ${d} [${approval.approval_id}]`,
    );
  };
  const interrupt = () => void send({type: "session.interrupt"});
  const quit = () => {
    setQuitOpen(false);
    void send({type: "session.quit"});
  };
  const startSession = async () => {
    setStarting(true);
    const {r} = await run({type: "session.start"});
    if (!startHold(r)) setStarting(false);
    // A 504 may still have started it: stay locked until it shows, or give up after a while.
    else if (r.kind === "http") setTimeout(() => setStarting(false), START_UNKNOWN_MS);
  };
  // "Starting…" until the agents actually ran (F-011).
  if (starting && model.started) setStarting(false);

  // Keyboard: Esc and Alt+Shift+A anywhere on the page (09 §5).
  const keyState = useRef({dialogOpen: false, canInterrupt: false});
  keyState.current = {
    dialogOpen: dialog || quitOpen,
    canInterrupt: can.act && (st === "running" || st === "ask_user" || st === "approval"),
  };
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (isCardShortcut(e)) {
        e.preventDefault();
        (card.current ?? questionPanel.current)?.focus();
        return;
      }
      if (e.key !== "Escape") return;
      const action = escAction({
        dialogOpen: keyState.current.dialogOpen,
        confirming: document.querySelector('.card.approval[data-confirming="true"]') !== null,
        canInterrupt: keyState.current.canInterrupt,
        composing: e.isComposing,
        inComposer: document.activeElement?.id === "composer",
      });
      if (action === "close-dialog") {
        setDialog(false);
        setQuitOpen(false);
      } else if (action === "cancel-confirm") {
        // Reached here only with focus outside the card (the card stops its own Esc).
        setCardReset((n) => n + 1);
      } else if (action === "interrupt") {
        interrupt();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
  // Forget per-card notes once the card is gone.
  useEffect(() => {
    const live = new Set(approvals.map((a) => a.approval_id));
    if ([...unknownIds].some((id) => !live.has(id))) setUnknownIds(new Set([...unknownIds].filter((id) => live.has(id))));
    if (cardError && !live.has(cardError.id)) setCardError(null);
  });

  if (phase === "expired") {
    return (
      <div className="fullscreen">
        세션 링크가 만료됐습니다. 터미널에서 <code>agent-augury --web</code> 을 다시 실행하세요.
      </div>
    );
  }
  if (phase === "too_many") {
    return (
      <div className="fullscreen">
        열린 탭이 너무 많습니다(최대 4). 다른 탭을 닫고 <button onClick={() => location.reload()}>다시 연결</button>
      </div>
    );
  }
  if (phase === "replaced") {
    return <div className="fullscreen">이 탭은 다른 창에서 다시 열렸습니다. 이 창은 닫아도 됩니다.</div>;
  }
  if (phase === "failed") {
    return (
      <div className="fullscreen">
        연결하지 못했습니다. <button onClick={() => location.reload()}>다시 연결</button>
      </div>
    );
  }

  const hap = s?.human_approval_pending ?? null;
  const dock = approval ? (
    <ApprovalCard
      ref={card}
      a={approval}
      index={1}
      total={approvals.length}
      questions={questions.length}
      can={can}
      unknown={unknownIds.has(approval.approval_id)}
      error={cardError?.id === approval.approval_id && (cardError.rev === null || cardError.rev === approval.revision) ? cardError.text : null}
      reset={cardReset}
      onDecide={decide}
      onClaim={() => void takeControl(false)}
    />
  ) : question ? (
    <QuestionPanel
      ref={questionPanel}
      q={question}
      index={1}
      total={questions.length}
      can={can}
      onAnswer={(content) => {
        if (busy) return;
        const option = question.options[Number(content) - 1] ?? "";
        void send(
          {type: "human.answer", content, question_id: question.question_id, thread_id: question.thread_id},
          `> [${content}] ${option} → ${question.agent_id ? `@${question.agent_id}` : "?"}`,
        );
      }}
      onSkip={() => {
        if (!busy) void send({type: "human.skip", question_id: question.question_id}, "> /skip");
      }}
      onClaim={() => void takeControl(false)}
    />
  ) : hap ? (
    <GateNotice pending={hap} summary={gateSummary(model)} idle={st === "idle"} can={can} onPrefix={(p) => composer.current?.fill(p)} />
  ) : null;
  const composerLabel = question
    ? `답변 → ${question.agent_id ? `@${question.agent_id}` : "?"} (${question.question_id})`
    : st === "idle"
      ? "메시지 (새 턴 시작)"
      : "메시지";
  const composerHint = approval
    ? "승인은 위 카드 버튼으로 합니다"
    : (s?.agents.length ?? 0) === 0 && can.act
      ? "에이전트 목록이 아직 없습니다 — 멘션 검사를 건너뜁니다"
      : null;
  // While a dialog is open the page behind it takes no input (09 §3.8, review 3.4 Nit).
  const blocked = dialog || quitOpen;
  return (
    <div className="app">
      <div className="main" {...(blocked ? {inert: ""} : {})}>
        <StatusBar model={model} phase={phase} />
        <div className="banners">
          {info?.dev_mode && (
            <div className="banner warn">⚠ 개발 모드: --web 없이 기동되어 비밀값 스크럽이 보장되지 않습니다</div>
          )}
          {auth.length > 0 && (
            <div className="banner info">
              🔑 인증 필요:{" "}
              {auth.map((line, i) => (
                <div key={i}>
                  <AuthText text={line} />
                </div>
              ))}
              <button className="link" onClick={() => setAuth([])}>
                닫기
              </button>
            </div>
          )}
          {phase === "reconnecting" && <div className="banner warn">⟳ 연결이 끊겼습니다. 재연결 중…</div>}
          {s && !s.is_controller && (
            <div className="banner info">
              {s.has_controller ? "👁 다른 탭이 조작 중입니다. 이 탭은 보기만 합니다" : "조작 중인 탭이 없습니다"}{" "}
              <button onClick={() => void takeControl(false)}>이 탭에서 조작</button>
            </div>
          )}
        </div>
        <div className="sr-only" aria-live="assertive">
          {announcement(approval, question, hap)}
        </div>
        <Timeline rows={model.rows} dock={dock} />
        <Composer ref={composer} can={can} label={composerLabel} hint={composerHint} warning={warning} busy={busy} onSubmit={submit} />
        <ControlBar
          showStart={Boolean(info?.flags.no_auto_start) && !model.started}
          starting={starting}
          canStart={can.act && !busy}
          canInterrupt={keyState.current.canInterrupt && !busy}
          canQuit={can.act}
          seq={model.lastSeq}
          live={phase === "live"}
          onStart={() => void startSession()}
          onInterrupt={interrupt}
          onQuit={() => setQuitOpen(true)}
        />
      </div>
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.level}`}>
            {t.text}
          </div>
        ))}
      </div>
      {dialog && <ClaimDialog onCancel={() => setDialog(false)} onConfirm={() => void takeControl(true)} />}
      {quitOpen && <QuitDialog onCancel={() => setQuitOpen(false)} onConfirm={quit} />}
    </div>
  );
}
