/** BFF-owned session state (WBS 2.7, 08 §4.4 · §7 · API-03 · API-06).
 *
 * Wire has no "answered" event, so the BFF owns the pending questions and
 * approvals and tells every tab through ``bff.state`` (F-026). It also owns the
 * controller tab (DW-7) — a UX guard against two tabs answering, not a
 * security boundary.
 */

import type {CmdKey, CommandBus, CommandState} from "./cmd.js";
import {errorReply, type Route} from "./http.js";
import {forwardChildLine, type EventHub, type Notice} from "./sse.js";
import type {ChildLine, WireMessage} from "./wire.js";

/** Controller grace after its stream drops, and the args clip (08 §6.4 / §4.4, [가정]). */
export const STATE_DEFAULTS = {
  controllerGraceMs: 5000,
  argValueMax: 4000,
};

const TAB_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export type ApprovalItem = {
  approval_id: string;
  agent_id: string;
  tool: string;
  args_preview: Record<string, unknown>;
  revision: number;
  ttl_seconds: number | null;
  received_at: number;
  resolving: boolean;
};

export type QuestionItem = {
  question_id: string;
  agent_id: string | null;
  thread_id: string | null;
  question: string;
  options: string[];
  resolving: boolean;
};

type Gateway = {status: "starting" | "running" | "exited"; exit_code: number | null; signal: string | null};
type Controller =
  | {mode: "INIT"}
  | {mode: "NONE"}
  | {mode: "HELD"; tab: string}
  | {mode: "ORPHANED"; tab: string; timer: NodeJS.Timeout};

export type Flags = {demo: boolean; no_auto_start: boolean; plan_first: boolean; session: string | null};

export type BffStateOptions = Partial<typeof STATE_DEFAULTS> & {
  /** Send every tab a fresh ``bff.state`` (``EventHub.broadcastState``). */
  broadcast: () => void;
  notice: (notice: Notice) => void;
  /** Does *tab* have a live SSE stream right now? */
  isConnected: (tab: string) => boolean;
  now?: () => number;
};

/** Long values are clipped so every snapshot stays small (08 §4.4, review S6). */
function clipArgs(args: unknown, max: number): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (!args || typeof args !== "object" || Array.isArray(args)) {
    return out;
  }
  for (const [key, value] of Object.entries(args)) {
    const text = typeof value === "string" ? value : JSON.stringify(value) ?? String(value);
    out[key] = text.length > max ? `${text.slice(0, max)}… (+${text.length - max} chars)` : value;
  }
  return out;
}

const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

/** Key-order-independent JSON, so a re-sent request with shuffled keys is not "changed". */
function stableJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(stableJson).join(",")}]`;
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${stableJson(v)}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

export class BffState implements CommandState {
  private readonly opts: typeof STATE_DEFAULTS;
  private readonly broadcast: () => void;
  private readonly notice: (n: Notice) => void;
  private readonly isConnected: (tab: string) => boolean;
  private readonly now: () => number;

  gateway: Gateway = {status: "starting", exit_code: null, signal: null};
  private agents: string[] = [];
  private phase: string | null = null;
  private gate: Record<string, unknown> | null = null;
  private humanApproval: {phase: string | null; thread_id: string | null; text: string | null} | null = null;
  private approvals = new Map<string, ApprovalItem>();
  /** Fingerprint of each approval's *unclipped* tool+args — never sent to tabs. */
  private approvalPrints = new Map<string, string>();
  private questions = new Map<string, QuestionItem>();
  private ended: {reason: string | null; steps: number | null} | null = null;
  private controller: Controller = {mode: "INIT"};

  constructor(options: BffStateOptions) {
    this.opts = {...STATE_DEFAULTS, ...options};
    this.broadcast = options.broadcast;
    this.notice = options.notice;
    this.isConnected = options.isConnected;
    this.now = options.now ?? Date.now;
  }

  // ---- CommandState (cmd.ts) ------------------------------------------------

  isController(tab: string): boolean {
    const c = this.controller;
    return (c.mode === "HELD" || c.mode === "ORPHANED") && c.tab === tab;
  }

  private item(key: CmdKey): ApprovalItem | QuestionItem | undefined {
    const id = key.slice(2);
    return key.startsWith("a:") ? this.approvals.get(id) : this.questions.get(id);
  }

  check(key: CmdKey, revision?: number): "not_pending" | "stale_revision" | "in_flight" | null {
    const item = this.item(key);
    if (!item) {
      return "not_pending";
    }
    // An answer to an older revision must not approve the re-issued args.
    if (revision !== undefined && "revision" in item && item.revision !== revision) {
      return "stale_revision";
    }
    return item.resolving ? "in_flight" : null;
  }

  setResolving(key: CmdKey, resolving: boolean): void {
    const item = this.item(key);
    if (item && item.resolving !== resolving) {
      item.resolving = resolving;
      this.broadcast();
    }
  }

  release(key: CmdKey): void {
    const id = key.slice(2);
    const approval = key.startsWith("a:");
    if (approval) {
      this.approvalPrints.delete(id);
    }
    if ((approval ? this.approvals : this.questions).delete(id)) {
      this.broadcast();
    }
  }

  // ---- Snapshot ---------------------------------------------------------------

  /** ``bff.state`` body for one tab; the hub adds ``seq_last`` and ``replay_gap``. */
  snapshot(tab: string): Record<string, unknown> {
    const c = this.controller;
    return {
      gateway: {...this.gateway},
      agents: [...this.agents],
      phase: this.phase,
      gate: this.gate,
      human_approval_pending: this.humanApproval,
      pending_approvals: [...this.approvals.values()].map((a) => ({...a})),
      pending_questions: [...this.questions.values()].map((q) => ({...q})),
      ended: this.ended,
      is_controller: this.isController(tab),
      has_controller: c.mode === "HELD" || c.mode === "ORPHANED",
    };
  }

  // ---- Gateway lifecycle ------------------------------------------------------

  /** First stdout line seen (API-03 ``starting`` → ``running``). True when it changed; caller broadcasts. */
  gatewayRunning(): boolean {
    if (this.gateway.status !== "starting") {
      return false;
    }
    this.gateway = {...this.gateway, status: "running"};
    return true;
  }

  /** Child gone: nothing can be answered any more (CP-2). Controller stays (08 §7.2). */
  gatewayExited(code: number | null, signal: string | null): void {
    this.gateway = {status: "exited", exit_code: code, signal};
    this.approvals.clear();
    this.approvalPrints.clear();
    this.questions.clear();
    this.humanApproval = null;
    this.broadcast();
  }

  // ---- Events (08 §4.4, SRS §3.4.2) -------------------------------------------

  private digest(): string {
    return JSON.stringify([
      this.agents, this.phase, this.gate, this.humanApproval, this.ended,
      [...this.approvals.values()], [...this.questions.values()],
    ]);
  }

  /** Apply one schema-valid event. True only when the snapshot really changed (caller broadcasts). */
  applyEvent(e: WireMessage): boolean {
    const before = this.digest();
    this.apply(e);
    return this.digest() !== before;
  }

  /** Nothing is answerable once the session ended or the child is gone (CP-2). */
  private closedForAnswers(): boolean {
    return this.ended !== null || this.gateway.status === "exited";
  }

  private apply(e: WireMessage): void {
    switch (e.type) {
      case "session.started":
        this.agents = Array.isArray(e.agents) ? e.agents.map(String) : [];
        this.ended = null;
        return;
      case "session.resumed":
        this.agents = Array.isArray(e.agents) ? e.agents.map(String) : this.agents;
        this.phase = str(e.phase) ?? this.phase;
        return;
      case "session.phase":
        this.phase = str(e.phase);
        this.humanApproval = null;
        if (this.phase === "COMPLETED" || this.phase === "REJECTED") {
          this.gate = null;
        }
        return;
      case "session.gate": {
        const {dir: _dir, type: _type, ...gate} = e;
        this.gate = gate;
        if (e.human_pending === false) {
          this.humanApproval = null;
        }
        return;
      }
      case "session.human_approval_pending":
        this.humanApproval = {phase: str(e.phase), thread_id: str(e.thread_id), text: str(e.text)};
        return;
      case "session.turn_done":
        this.gate = null;
        this.humanApproval = null;
        return;
      case "session.ended":
        this.ended = {reason: str(e.reason), steps: typeof e.steps === "number" ? e.steps : null};
        this.approvals.clear();
        this.approvalPrints.clear();
        this.questions.clear();
        this.humanApproval = null;
        return;
      case "human.question": {
        if (this.closedForAnswers()) {
          return;
        }
        const id = str(e.question_id);
        if (!id) {
          this.notice({kind: "schema_invalid", level: "error", text: "question without question_id — cannot be answered from the Web"});
          return;
        }
        const old = this.questions.get(id);
        this.questions.set(id, {
          question_id: id,
          agent_id: str(e.agent_id),
          thread_id: str(e.thread_id),
          question: String(e.question),
          options: Array.isArray(e.options) ? e.options.map(String) : [],
          resolving: old?.resolving ?? false,
        });
        return;
      }
      case "approval.request":
        if (!this.closedForAnswers()) {
          this.request(e);
        }
        return;
      case "approval.resolved":
      case "approval.granted":
      case "approval.expired":
      case "tool.denied": {
        // A second release after our own command succeeded is a no-op (08 §4.4).
        const id = str(e.approval_id);
        if (id !== null) {
          this.approvals.delete(id);
          this.approvalPrints.delete(id);
        }
        return;
      }
      default:
        return;
    }
  }

  /** Same id again (resume re-issue): keep its place, bump ``revision`` only if tool/args changed.
   *
   * Compared on the full, unclipped args: a change hidden past the clip must
   * still reset the user's confirmation (08 §4.4 M3). ``resolving`` survives a
   * change — a command already sent cannot be recalled; its result releases the
   * card or makes it answerable again, and the client shows "changed" meanwhile.
   */
  private request(e: WireMessage): void {
    const id = str(e.approval_id);
    if (!id) {
      return; // Ink ignores an empty id too (App.tsx)
    }
    const print = stableJson([String(e.tool), e.args_preview ?? null]);
    const old = this.approvals.get(id);
    const changed = old !== undefined && this.approvalPrints.get(id) !== print;
    this.approvalPrints.set(id, print);
    const agentId = String(e.agent_id);
    const ttl = typeof e.ttl_seconds === "number" ? e.ttl_seconds : null;
    // An identical re-issue keeps received_at, so the snapshot (and digest) stays the same.
    const same = old !== undefined && !changed && old.agent_id === agentId && old.ttl_seconds === ttl;
    this.approvals.set(id, {
      approval_id: id,
      agent_id: agentId,
      tool: String(e.tool),
      args_preview: clipArgs(e.args_preview, this.opts.argValueMax),
      revision: old ? old.revision + (changed ? 1 : 0) : 1,
      ttl_seconds: ttl,
      received_at: same ? old.received_at : this.now(),
      resolving: old?.resolving ?? false,
    });
  }

  // ---- Controller tab (08 §7) -----------------------------------------------------

  /** Hub ``onConnect``: runs before the new stream's first snapshot. */
  tabConnected(tab: string): void {
    const c = this.controller;
    if (c.mode === "INIT") {
      // T1. No other stream can exist yet, so nobody else needs a broadcast.
      this.controller = {mode: "HELD", tab};
    } else if (c.mode === "ORPHANED" && c.tab === tab) {
      clearTimeout(c.timer); // T4: the controller came back (reload)
      this.controller = {mode: "HELD", tab};
    }
    // T1' / T2 / T6: nothing changes.
  }

  /** Hub ``onDisconnect``. */
  tabDisconnected(tab: string): void {
    const c = this.controller;
    if (c.mode !== "HELD" || c.tab !== tab) {
      return;
    }
    // T3 → (T5 after the grace period).
    const timer = setTimeout(() => {
      if (this.controller.mode === "ORPHANED" && this.controller.tab === tab) {
        this.controller = {mode: "NONE"};
        this.broadcast();
      }
    }, this.opts.controllerGraceMs).unref();
    this.controller = {mode: "ORPHANED", tab, timer};
  }

  /** API-06 transitions T7–T11. Null means 409 ``tab_not_connected``. */
  claim(tab: string, force: boolean): boolean | null {
    if (!this.isConnected(tab)) {
      return null; // T11
    }
    const c = this.controller;
    if (c.mode === "HELD") {
      if (c.tab === tab) {
        return true; // T8
      }
      if (!force) {
        return false; // T9
      }
    }
    if (c.mode === "ORPHANED") {
      clearTimeout(c.timer);
    }
    this.controller = {mode: "HELD", tab}; // T7 / T10
    this.broadcast();
    return true;
  }

  /** Stop the grace timer (BFF shutdown). */
  close(): void {
    if (this.controller.mode === "ORPHANED") {
      clearTimeout(this.controller.timer);
    }
  }

  // ---- Routes -------------------------------------------------------------------

  /** API-06 ``POST /api/control/claim``. */
  claimRoute(): [string, Route] {
    return [
      "POST /api/control/claim",
      {
        method: "POST",
        auth: true,
        handle: ({body}) => {
          const tab = body?.tab_id;
          const force = body?.force === undefined ? false : body.force;
          if (typeof tab !== "string" || !TAB_RE.test(tab) || typeof force !== "boolean") {
            return errorReply(400, "bad_request", "tab_id must be a lowercase UUID v4 and force a boolean");
          }
          const controller = this.claim(tab, force);
          if (controller === null) {
            return errorReply(409, "tab_not_connected", "open the event stream for this tab first");
          }
          return {status: 200, body: {controller}};
        },
      },
    ];
  }

  /** API-03 ``GET /api/session[?tab=]``. */
  sessionRoute(info: {flags: Flags; devMode: boolean; seqLast: () => number; streamsAvailable: () => number}): [string, Route] {
    return [
      "GET /api/session",
      {
        method: "GET",
        auth: true,
        handle: ({query}) => {
          const tab = query.get("tab");
          if (tab !== null && !TAB_RE.test(tab)) {
            return errorReply(400, "bad_request", "tab must be a lowercase UUID v4");
          }
          return {
            status: 200,
            body: {
              gateway: {...this.gateway},
              seq_last: info.seqLast(),
              flags: info.flags,
              dev_mode: info.devMode,
              tab: {controller: tab !== null && this.isController(tab)},
              streams_available: info.streamsAvailable(),
            },
          };
        },
      },
    ];
  }
}

export type BffParts = {hub: EventHub; state: BffState; bus: CommandBus};

/** One child stdout line, in order (N-006): the frame first, then one ``bff.state`` (08 §4.3). */
export function handleChildLine(line: ChildLine, parts: BffParts): void {
  const result = forwardChildLine(parts.hub, line);
  let changed = parts.state.gatewayRunning();
  if (result) {
    // release/setResolving broadcast for themselves; if this is the very first
    // line, the "running" broadcast below repeats it once — harmless.
    parts.bus.onResult(result);
  } else if (line.kind === "event" && !line.schemaError) {
    // Schema-invalid events are shown but never change state (08 §4.2).
    changed = parts.state.applyEvent(line.message) || changed;
  }
  if (changed) {
    parts.hub.broadcastState();
  }
}

/** The child exited: fail waiting commands at once, then clear what can no longer be answered. */
export function handleChildExit(code: number | null, signal: string | null, parts: BffParts): void {
  parts.bus.onChildExit();
  parts.state.gatewayExited(code, signal);
}
