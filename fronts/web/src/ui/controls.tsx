/** Composer, question panel, approval card, gate notice and control bar (WBS 3.4–3.8, 09 §3.4–3.8). */

import {forwardRef, useEffect, useImperativeHandle, useRef, useState, type KeyboardEvent} from "react";
import {APPEAR_LOCK_MS, CONFIRM_LOCK_MS, FOLD_CHARS, TTL_WARN_S, formatLeft, ttlLeft} from "./approval.js";
import {Markdown} from "./markdown.js";
import {clip, valueText, visible} from "./sanitize.js";

/** Who may act right now, and why not (09 §4 matrix). */
export type Can = {act: boolean; reason: string | null};

// ---- 3.4 Composer ------------------------------------------------------------------

export type ComposerHandle = {focus: () => void; fill: (prefix: string) => void};

export const Composer = forwardRef<
  ComposerHandle,
  {
    can: Can;
    label: string;
    hint: string | null;
    warning: string | null;
    busy: boolean;
    onSubmit: (text: string) => Promise<boolean>;
  }
>(function Composer({can, label, hint, warning, busy, onSubmit}, ref) {
  const [text, setText] = useState("");
  const area = useRef<HTMLTextAreaElement>(null);
  useImperativeHandle(ref, () => ({
    focus: () => area.current?.focus(),
    fill: (prefix) => {
      setText((t) => (t.startsWith(prefix) ? t : `${prefix} ${t.replace(/^(APPROVE|REJECT):\s*/, "")}`));
      area.current?.focus();
    },
  }));
  const submit = async () => {
    if (!can.act || busy || !text.trim()) return;
    if (await onSubmit(text)) setText("");
  };
  return (
    <div className="composer">
      <label className="composer-label" htmlFor="composer">
        {label}
      </label>
      <div className="composer-row">
        <textarea
          id="composer"
          ref={area}
          rows={2}
          value={text}
          disabled={!can.act}
          placeholder={can.reason ?? "메시지 (Enter 전송 · Shift+Enter 줄바꿈)"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // Enter sends; Shift+Enter is a newline; never act inside an IME composition.
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void submit();
            }
          }}
        />
        <button className="primary" disabled={!can.act || busy || !text.trim()} onClick={() => void submit()}>
          {busy ? "전송 중…" : "전송"}
        </button>
      </div>
      {warning && <div className="composer-warn">⚠ {warning}</div>}
      {hint && <div className="composer-hint">ⓘ {hint}</div>}
    </div>
  );
});

// ---- 3.5 Question panel ----------------------------------------------------------------

export type Question = {question_id: string; agent_id: string | null; thread_id: string | null; question: string; options: string[]; resolving: boolean};

export const QuestionPanel = forwardRef<
  HTMLDivElement,
  {q: Question; index: number; total: number; can: Can; onAnswer: (content: string) => void; onSkip: () => void; onClaim: () => void}
>(function QuestionPanel({q, index, total, can, onAnswer, onSkip, onClaim}, ref) {
  const disabled = !can.act || q.resolving;
  const onKey = (e: KeyboardEvent) => {
    if (disabled || e.target instanceof HTMLTextAreaElement) return;
    const n = Number(e.key);
    if (Number.isInteger(n) && n >= 1 && n <= q.options.length) onAnswer(String(n));
    else if (e.key === "s") onSkip();
  };
  return (
    <section className="card question" ref={ref} tabIndex={0} onKeyDown={onKey} aria-label="질문">
      <header>
        ? 질문 · {visible(clip(q.agent_id ?? "?", 64))}
        {q.thread_id ? ` · 스레드 ${visible(clip(q.thread_id, 64))}` : ""}
        <span className="count">
          {index}/{total}
        </span>
      </header>
      <Markdown text={q.question} />
      {q.options.length > 0 && (
        <ol className="options">
          {q.options.map((opt, i) => (
            <li key={i}>
              {/* Core maps the number to the option text (bridge.py _resolve_option_content). */}
              <button disabled={disabled} onClick={() => onAnswer(String(i + 1))}>
                [{i + 1}] {visible(clip(opt, 200))}
              </button>
            </li>
          ))}
        </ol>
      )}
      <footer>
        {can.act ? (
          <>
            {q.resolving ? <span className="muted">전송 중… 결과를 기다립니다</span> : <span className="muted">번호를 누르거나 아래 입력창에 답하세요.</span>}
            <button disabled={disabled} onClick={onSkip}>
              건너뛰기 (s)
            </button>
          </>
        ) : (
          <span className="muted">
            {can.reason}{" "}
            {can.reason?.startsWith("👁") && <button onClick={onClaim}>이 탭에서 조작</button>}
          </span>
        )}
      </footer>
    </section>
  );
});

// ---- 3.6 Approval card ----------------------------------------------------------------------

export type Approval = {
  approval_id: string;
  agent_id: string;
  tool: string;
  args_preview: Record<string, unknown>;
  revision: number;
  ttl_seconds: number | null;
  received_at: number;
  resolving: boolean;
};

function ArgValue({text}: {text: string}) {
  const [open, setOpen] = useState(false);
  const long = text.length > FOLD_CHARS;
  return (
    <>
      {visible(long && !open ? clip(text, FOLD_CHARS) : text)}
      {long && (
        <button className="link" onClick={() => setOpen(!open)}>
          {open ? "접기" : "전체 보기"}
        </button>
      )}
    </>
  );
}

export const ApprovalCard = forwardRef<
  HTMLDivElement,
  {a: Approval; index: number; total: number; questions: number; can: Can; unknown: boolean; error: string | null; onDecide: (d: "granted" | "denied") => void; onClaim: () => void}
>(function ApprovalCard({a, index, total, questions, can, unknown, error, onDecide, onClaim}, ref) {
  const [now, setNow] = useState(() => Date.now());
  const [shownAt, setShownAt] = useState(() => Date.now());
  const [confirmAt, setConfirmAt] = useState<number | null>(null);
  const self = useRef<HTMLDivElement>(null);
  useImperativeHandle(ref, () => self.current as HTMLDivElement);
  // New card or changed content: reset the confirmation and lock again (10 M1, M3),
  // and remember which arguments changed so they can be highlighted.
  // (React's "store the previous value in state" pattern.)
  const key = `${a.approval_id}#${a.revision}`;
  const [shownKey, setShownKey] = useState(key);
  const [shownArgs, setShownArgs] = useState(a.args_preview);
  const [changed, setChanged] = useState<ReadonlySet<string>>(new Set());
  if (shownKey !== key) {
    const sameCard = shownKey.split("#")[0] === a.approval_id;
    setChanged(
      sameCard
        ? new Set(Object.keys(a.args_preview).filter((k) => valueText(a.args_preview[k]) !== valueText(shownArgs[k])))
        : new Set(),
    );
    setShownKey(key);
    setShownArgs(a.args_preview);
    setShownAt(Date.now());
    setConfirmAt(null);
  }
  // Becoming able to act (reconnect, "이 탭에서 조작") locks again too: the click that
  // gained control must not land on the card (review 3.4 S).
  const [couldAct, setCouldAct] = useState(can.act);
  if (couldAct !== can.act) {
    setCouldAct(can.act);
    if (can.act) {
      setShownAt(Date.now());
      setConfirmAt(null);
    }
  }
  // Coming back to the tab: same reason.
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") {
        setShownAt(Date.now());
        setConfirmAt(null);
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, []);
  // The confirm step owns the keyboard, so Esc cancels even after a mouse click.
  useEffect(() => {
    if (confirmAt !== null) self.current?.focus();
  }, [confirmAt]);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 200);
    return () => clearInterval(t);
  }, []);
  // Re-render exactly when a lock ends, so a button unlocks on time, not up to 200 ms late.
  useEffect(() => {
    const ends = [shownAt + APPEAR_LOCK_MS, confirmAt === null ? 0 : confirmAt + CONFIRM_LOCK_MS];
    const timers = ends.filter((t) => t > Date.now()).map((t) => setTimeout(() => setNow(Date.now()), t - Date.now() + 1));
    return () => timers.forEach(clearTimeout);
  }, [shownAt, confirmAt]);

  // Rendering uses the 200 ms clock (may lag, so it only ever looks *more* locked);
  // the click guards check the real time, so a lock never ends early.
  const locked = now - shownAt < APPEAR_LOCK_MS;
  const confirmLocked = confirmAt !== null && now - confirmAt < CONFIRM_LOCK_MS;
  const disabled = !can.act || a.resolving || locked;
  const left = ttlLeft(a, now);
  const open = () => can.act && !a.resolving && Date.now() - shownAt >= APPEAR_LOCK_MS;
  const startConfirm = () => open() && setConfirmAt(Date.now());
  const approve = () => open() && confirmAt !== null && Date.now() - confirmAt >= CONFIRM_LOCK_MS && onDecide("granted");
  const deny = () => open() && onDecide("denied");
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape" && confirmAt !== null) {
      e.stopPropagation();
      setConfirmAt(null);
      return;
    }
    // a / d / Enter only when the card itself has focus: Enter on its "취소" button
    // must cancel, never approve (review 3.4 Must).
    if (e.target !== e.currentTarget) return;
    if (e.key === "a") startConfirm();
    else if (e.key === "d") deny();
    else if (e.key === "Enter" && confirmAt !== null) {
      e.preventDefault();
      approve(); // Enter alone never approves: only after "a" (N-012)
    }
  };

  return (
    <section className="card approval" ref={self} tabIndex={0} onKeyDown={onKey} aria-label="도구 승인 요청" data-confirming={confirmAt !== null}>
      <header>
        ⚑ 도구 승인 요청
        <span className="count">
          {index}/{total}
          {questions > 0 ? ` · 질문 ${questions}건 대기` : ""}
        </span>
        {a.revision > 1 && <span className="changed">⚠ 내용이 변경됨 (rev {a.revision})</span>}
        {left !== null && (
          <span className={`ttl${left <= TTL_WARN_S ? " urgent" : ""}`}>{left > 0 ? `남은 시간 ${formatLeft(left)}` : "만료 대기 중"}</span>
        )}
      </header>
      <dl className="meta">
        <dt>에이전트</dt>
        <dd>{visible(clip(a.agent_id, 64))}</dd>
        <dt>도구</dt>
        <dd>{visible(clip(a.tool, 64))}</dd>
        <dt>approval_id</dt>
        <dd>{visible(clip(a.approval_id, 64))}</dd>
      </dl>
      {/* Key/value cells, never "key: value" lines — a newline in a key cannot forge a row (10 M2). */}
      <table className="args">
        <tbody>
          {Object.entries(a.args_preview).map(([k, v]) => (
            <tr key={k} className={changed.has(k) ? "changed-cell" : undefined}>
              <th>{visible(clip(k, 64))}</th>
              <td>
                <ArgValue text={valueText(v)} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {error && <div className="card-error">{error}</div>}
      {!can.act ? (
        <footer className="muted">
          {can.reason?.startsWith("👁") ? "👁 조작 탭에서만 승인할 수 있습니다 " : can.reason}
          {can.reason?.startsWith("👁") && <button onClick={onClaim}>이 탭에서 조작</button>}
        </footer>
      ) : a.resolving ? (
        <footer className="muted">{unknown ? "결과 확인 중 — 이미 처리됐을 수 있습니다" : "전송 중…"}</footer>
      ) : confirmAt === null ? (
        <footer className="decide">
          <button disabled={disabled} onClick={deny}>
            ✕ Deny (d)
          </button>
          {locked && <span className="lock" aria-hidden="true" />}
          <button disabled={disabled} onClick={startConfirm}>
            ✓ Approve… (a)
          </button>
        </footer>
      ) : (
        <>
          <footer className="decide">
            <button onClick={() => setConfirmAt(null)}>취소 (Esc)</button>
            <span className="muted">Approve 를 확정할까요?</span>
          </footer>
          {/* A separate row, away from where "Approve…" was: a double click cannot land here. */}
          <div className="confirm-row">
            <button className="primary" disabled={disabled || confirmLocked} onClick={approve}>
              확정 (Enter)
            </button>
          </div>
        </>
      )}
    </section>
  );
});

// ---- 3.7 Gate notice ------------------------------------------------------------------------

export function GateNotice({
  pending,
  summary,
  idle,
  can,
  onPrefix,
}: {
  pending: {phase: string | null; thread_id: string | null; text: string | null};
  summary: string | null;
  idle: boolean;
  can: Can;
  onPrefix: (prefix: "APPROVE:" | "REJECT:") => void;
}) {
  return (
    <section className="card gate" aria-label="사람 승인 단계">
      <header>
        ⚖ 사람 승인이 필요한 단계 {pending.phase ? `· ${visible(clip(pending.phase, 64))}` : ""}
        {pending.thread_id ? ` · 스레드 ${visible(clip(pending.thread_id, 64))}` : ""}
      </header>
      {pending.text && <p className="gate-text">{pending.text}</p>}
      {summary && <p className="gate-summary">상태: {summary}</p>}
      {idle && <p className="composer-warn">⚠ 지금은 idle 상태라 스레드 지정이 전달되지 않습니다 (DW-6)</p>}
      <footer className="decide">
        <button disabled={!can.act} onClick={() => onPrefix("APPROVE:")}>
          APPROVE: …
        </button>
        <button disabled={!can.act} onClick={() => onPrefix("REJECT:")}>
          REJECT: …
        </button>
      </footer>
    </section>
  );
}

// ---- 3.8 Control bar and Quit dialog --------------------------------------------------------

export function ControlBar({
  showStart,
  starting,
  canStart,
  canInterrupt,
  canQuit,
  seq,
  live,
  onStart,
  onInterrupt,
  onQuit,
}: {
  showStart: boolean;
  starting: boolean;
  canStart: boolean;
  canInterrupt: boolean;
  canQuit: boolean;
  seq: number;
  live: boolean;
  onStart: () => void;
  onInterrupt: () => void;
  onQuit: () => void;
}) {
  return (
    <div className="controlbar">
      {showStart && (
        <button disabled={!canStart || starting} onClick={onStart}>
          {starting ? "시작 중…" : "▶ Start"}
        </button>
      )}
      <button disabled={!canInterrupt} onClick={onInterrupt}>
        ■ Interrupt (Esc)
      </button>
      <button disabled={!canQuit} onClick={onQuit}>
        ⏻ Quit…
      </button>
      <span className="muted seq">
        seq {seq} · {live ? "●SSE" : "○SSE"}
      </span>
    </div>
  );
}

export function QuitDialog({onCancel, onConfirm}: {onCancel: () => void; onConfirm: () => void}) {
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => cancel.current?.focus(), []);
  return (
    <div className="backdrop" role="dialog" aria-modal="true" aria-labelledby="quit-title">
      <div className="dialog">
        <h2 id="quit-title">세션 종료</h2>
        <p>세션을 끝낼까요? 에이전트가 멈추고 이 페이지는 더 이상 명령을 보낼 수 없습니다. (체크포인트는 남습니다)</p>
        <div className="actions">
          <button ref={cancel} onClick={onCancel}>
            취소 (Esc)
          </button>
          <button className="primary" onClick={onConfirm}>
            종료
          </button>
        </div>
      </div>
    </div>
  );
}
