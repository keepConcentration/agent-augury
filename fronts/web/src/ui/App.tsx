/** Web Surface app shell (WBS 3.1) and timeline (3.2) — 09 §2·§3.1~3.3·§3.9. */

import {memo, useCallback, useEffect, useLayoutEffect, useReducer, useRef, useState} from "react";
import {authLink} from "./auth.js";
import {claim, openStream, session, startOnce, type Session} from "./connection.js";
import {Markdown} from "./markdown.js";
import {
  STATUS_LABEL,
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
  | {type: "state"; s: NonNullable<Model["state"]>};

function reduce(m: Model, a: Action): Model {
  if (a.type === "wire") return applyWire(m, a.seq, a.e);
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

/** Follows the bottom unless the user scrolled up; then counts what arrived (09 §3.3). */
function Timeline({rows}: {rows: Row[]}) {
  const box = useRef<HTMLDivElement>(null);
  const atBottom = useRef(true);
  const seen = useRef(rows.length);
  const [unseen, setUnseen] = useState(0);
  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    if (atBottom.current) {
      el.scrollTop = el.scrollHeight;
      seen.current = rows.length;
      setUnseen(0);
    } else {
      setUnseen(rows.length - seen.current);
    }
  }, [rows.length]);
  const toBottom = () => {
    const el = box.current;
    if (el) el.scrollTop = el.scrollHeight;
    atBottom.current = true;
    seen.current = rows.length;
    setUnseen(0);
  };
  return (
    <div className="timeline-wrap">
      <div
        className="timeline"
        ref={box}
        tabIndex={0}
        aria-label="타임라인"
        onScroll={(ev) => {
          const el = ev.currentTarget;
          atBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          if (atBottom.current) {
            seen.current = rows.length;
            setUnseen(0);
          }
        }}
        onKeyDown={(ev) => ev.key === "End" && toBottom()}
      >
        {rows.map((row) => (
          <TimelineRow key={row.key} row={row} />
        ))}
      </div>
      {unseen > 0 && (
        <button className="new-chip" onClick={toBottom}>
          ↓ 새 이벤트 {unseen}건
        </button>
      )}
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

  const s = model.state;
  return (
    <div className="app">
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
      <Timeline rows={model.rows} />
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.level}`}>
            {t.text}
          </div>
        ))}
      </div>
      {dialog && <ClaimDialog onCancel={() => setDialog(false)} onConfirm={() => void takeControl(true)} />}
    </div>
  );
}
