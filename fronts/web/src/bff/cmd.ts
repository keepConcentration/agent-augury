/** Command POST, result correlation and failure judgement (WBS 2.6, 08 API-05 · §6).
 *
 * The BFF is the only writer on the child's stdin, so ``web-<n>`` ids never
 * collide (N-006). Who may send (controller) and what is pending belong to the
 * state module (WBS 2.7); this module only asks it through ``CommandState``.
 */

import {errorReply, type Reply, type Route} from "./http.js";
import type {EventHub} from "./sse.js";
import {commandSchemaError, type WireMessage} from "./wire.js";

/** 08 §6.4 values (all [가정]). */
export const CMD_DEFAULTS = {
  timeoutMs: 10_000,
  unknownMs: 60_000,
  maxUnknown: 256,
};

/** Pending key: a question or an approval the command answers. */
export type CmdKey = `q:${string}` | `a:${string}`;

/** What the state module (2.7) provides. Every call is synchronous.
 *
 * ``setResolving`` and ``release`` may be called for keys that are no longer
 * pending (an ``approval.*`` event got there first): those calls are no-ops and
 * do not broadcast. Clearing all pending items on child exit and
 * ``session.ended`` (CP-2) is the state module's job, not this one's.
 */
export interface CommandState {
  isController(tab: string): boolean;
  /** Step 4/5: is *key* pending, and not already being answered? */
  check(key: CmdKey): "not_pending" | "in_flight" | null;
  /** Mark *key* as answered-but-unconfirmed (true) or answerable again (false); broadcasts. */
  setResolving(key: CmdKey, resolving: boolean): void;
  /** Drop *key* from pending; broadcasts. Must be idempotent. */
  release(key: CmdKey): void;
}

/** Where commands go: the Gateway child. */
export interface CommandSink {
  alive(): boolean;
  send(message: WireMessage): boolean;
}

/** F-017: did Core refuse this command? (08 §6.3) */
export function judge(result: Record<string, unknown>): {failed: boolean; failure: string | null} {
  const failed = result.ok === false || result.queued === false || result.skipped === false || result.error != null;
  const failure =
    typeof result.error === "string"
      ? result.error
      : typeof result.reason === "string"
        ? result.reason
        : failed
          ? "rejected"
          : null;
  return {failed, failure};
}

// Fields a client may set per type (08 API-05 step 2). Everything else is dropped.
const ALLOWED: Record<string, readonly string[]> = {
  "session.start": [],
  "session.interrupt": [],
  "session.quit": [],
  "human.send": ["content", "mentions", "thread_id"],
  "human.answer": ["content", "question_id", "thread_id", "mentions"],
  "human.skip": ["question_id"],
  "approval.resolve": ["approval_id", "decision"],
};

const SOURCE = {surface: "web", mode: "interact"};
const TAB_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
/** Id the gateway uses for its own shutdown quit (``GatewayChild.stop``). */
const SHUTDOWN_ID = "web-shutdown";
/** Id Core puts on a result for a line it could not decode or handle (``session_stdio.py``). */
const UNDECODED_ID = "?";

// Commands that answer a pending item, and the field that names it.
const KEY_FIELD: Record<string, {prefix: "q" | "a"; field: string}> = {
  "human.answer": {prefix: "q", field: "question_id"},
  "human.skip": {prefix: "q", field: "question_id"},
  "approval.resolve": {prefix: "a", field: "approval_id"},
};

/** The pending key a command answers; null for keyless commands or a missing/empty id. */
function keyOf(cmd: WireMessage): CmdKey | null {
  const spec = cmd.type !== undefined && Object.hasOwn(KEY_FIELD, cmd.type) ? KEY_FIELD[cmd.type] : undefined;
  const value = spec ? cmd[spec.field] : undefined;
  return spec && typeof value === "string" && value !== "" ? `${spec.prefix}:${value}` : null;
}

// Core's own wording (bridge.py:198, :237); 08 §6.3 is the reference if it changes.
const NO_PENDING = "no pending question";
const FAILURE_TEXT_MAX = 200;

type Waiter = {key: CmdKey | null; settle: (reply: Reply) => void; timer: NodeJS.Timeout};
type Unknown = {key: CmdKey | null; timer: NodeJS.Timeout};

export type CommandBusOptions = Partial<typeof CMD_DEFAULTS> & {
  sink: CommandSink;
  state: CommandState;
  hub: EventHub;
  /** BFF log line (stderr by default). Never gets message content. */
  log?: (line: string) => void;
};

export class CommandBus {
  private readonly opts: typeof CMD_DEFAULTS;
  private readonly sink: CommandSink;
  private readonly state: CommandState;
  private readonly hub: EventHub;
  private readonly log: (line: string) => void;
  private next = 1;
  private inflight = new Map<string, Waiter>();
  private unknown = new Map<string, Unknown>();
  /** Expired UNKNOWN ids, so a very late result is still recognised (bounded by maxUnknown). */
  private expired = new Map<string, CmdKey | null>();
  /** The command that last answered each key. Only it may settle or reopen the key, so a
   *  late result for an older command never touches a newer one's card. */
  private owner = new Map<CmdKey, string>();

  constructor(options: CommandBusOptions) {
    this.opts = {...CMD_DEFAULTS, ...options};
    this.sink = options.sink;
    this.state = options.state;
    this.hub = options.hub;
    this.log = options.log ?? ((line) => process.stderr.write(`${line}\n`));
  }

  /** After a Core verdict on a keyed command: release, or make it answerable again.
   *
   * Returns false (and changes nothing) when a newer command has taken the key since.
   */
  private settleKey(key: CmdKey | null, id: string, failed: boolean, failure: string | null): boolean {
    if (!key) {
      return true;
    }
    if (this.owner.get(key) !== id) {
      return false;
    }
    this.owner.delete(key);
    // "no pending question" means it is already gone (F-006/F-007): drop the card.
    if (!failed || failure === NO_PENDING) {
      this.state.release(key);
    } else {
      this.state.setResolving(key, false);
    }
    return true;
  }

  private success(cmd: WireMessage, result: WireMessage): Reply {
    const {failed, failure} = judge(result);
    this.settleKey(keyOf(cmd), String(cmd.id), failed, failure);
    if (!failed && cmd.type === "human.send" && result.as_turn === true) {
      // DW-5/DW-6: Core drops source/mentions on an idle send; keep the provenance here.
      this.log(`augury web: idle human.send started a new turn (id=${cmd.id}, surface=web, mentions=${JSON.stringify(cmd.mentions ?? [])})`);
    }
    return {status: 200, body: {id: cmd.id, result, failed, failure}};
  }

  /** A ``dir=result`` line from the child (via ``forwardChildLine``). */
  onResult(result: WireMessage): void {
    const id = String(result.id);
    let waiter = this.inflight.get(id);
    // Core could not read a line. With one command waiting it can only be that one (we are
    // the only writer and every earlier one has answered or timed out).
    if (!waiter && id === UNDECODED_ID && this.inflight.size === 1) {
      waiter = this.inflight.values().next().value!;
    }
    if (waiter) {
      // The waiter's own command is needed for the verdict; it settles itself.
      waiter.settle({status: 200, body: result});
      return;
    }
    const late = this.unknown.get(id);
    const expired = this.expired.has(id);
    if (late || expired) {
      let key: CmdKey | null;
      if (late) {
        clearTimeout(late.timer);
        this.unknown.delete(id);
        key = late.key;
      } else {
        key = this.expired.get(id) ?? null;
        this.expired.delete(id);
      }
      const {failed, failure} = judge(result);
      // After expiry the key is answerable again; a late success still means it was applied.
      // If a newer command has answered the key since, its card is not ours to change.
      const superseded = !this.settleKey(key, id, failed, failure);
      this.hub.publishNotice({
        kind: "late_result",
        level: failed || expired || superseded ? "warn" : "info",
        text: `${id} result arrived late: ${failed ? "failed" : "ok"}${superseded ? " (a newer command has answered it since)" : ""}`,
        cmd_id: id,
        failed,
        failure: failure === null ? null : failure.slice(0, FAILURE_TEXT_MAX),
        superseded,
      });
      return;
    }
    if (id === SHUTDOWN_ID) {
      return;
    }
    const hint =
      id === UNDECODED_ID
        ? ` — Core could not read a command line; ${this.inflight.size} waiting command(s) cannot be matched and will time out`
        : "";
    this.hub.publishNotice({kind: "orphan_result", level: "error", text: `result with unknown id ${id.slice(0, 64)}${hint}`, cmd_id: id.slice(0, 64)});
  }

  /** The child is gone: every waiting command fails now, not after the timeout (SRS API-05 6). */
  onChildExit(): void {
    const keys: Array<[string, CmdKey | null]> = [];
    for (const [id, waiter] of this.inflight) {
      clearTimeout(waiter.timer);
      keys.push([id, waiter.key]);
      // Already on the child's stdin: unlike a 503, it may have been applied.
      waiter.settle(this.lost(id));
    }
    this.inflight.clear();
    for (const [id, entry] of this.unknown) {
      clearTimeout(entry.timer);
      keys.push([id, entry.key]);
    }
    this.unknown.clear();
    this.expired.clear();
    // Nothing can confirm these any more; the state module clears pending on exit (CP-2).
    for (const [id, key] of keys) {
      if (key && this.owner.get(key) === id) {
        this.state.setResolving(key, false);
      }
    }
    this.owner.clear();
  }

  /** Not sent: the child was not running (or refused the write). Safe to retry. */
  private unavailable(id: string): Reply {
    return {status: 503, body: {error: {code: "gateway_unavailable", message: "the agent session is not running", cmd_id: id}}};
  }

  /** Sent, then the child exited before answering: the outcome is unknown. */
  private lost(id: string): Reply {
    return {
      status: 502,
      body: {error: {code: "gateway_exited", message: "the agent session ended before answering — it may already have been applied", cmd_id: id}},
    };
  }

  private tombstone(id: string, key: CmdKey | null): void {
    this.expired.set(id, key);
    if (this.expired.size > this.opts.maxUnknown) {
      const [oldId, oldKey] = this.expired.entries().next().value!;
      this.expired.delete(oldId);
      if (oldKey && this.owner.get(oldKey) === oldId) {
        this.owner.delete(oldKey);
      }
    }
  }

  /** UNKNOWN → EXPIRED after a while: the key becomes answerable again (08 §6.2).
   *
   * The cap only evicts keyless entries: keyed ones are bounded by what is
   * pending, and evicting one early would reopen a double answer.
   */
  private rememberUnknown(id: string, key: CmdKey | null): void {
    if (this.unknown.size >= this.opts.maxUnknown) {
      for (const [oldId, entry] of this.unknown) {
        if (!entry.key) {
          clearTimeout(entry.timer);
          this.unknown.delete(oldId);
          this.tombstone(oldId, null);
          break;
        }
      }
    }
    const timer = setTimeout(() => {
      this.unknown.delete(id);
      this.tombstone(id, key);
      // Ownership stays with this id, so a late success can still release the key.
      if (key && this.owner.get(key) === id) {
        this.state.setResolving(key, false);
      }
    }, this.opts.unknownMs).unref();
    this.unknown.set(id, {key, timer});
  }

  /** Steps 0–8 of 08 API-05. */
  async handle(tab: unknown, body: Record<string, unknown>): Promise<Reply> {
    // 0. Controller (CP-5): checked first so a read-only tab always gets the same answer.
    if (typeof tab !== "string" || !TAB_RE.test(tab) || !this.state.isController(tab)) {
      return errorReply(409, "not_controller", "this tab is read-only");
    }
    // 1. Known type.
    const type = body.type;
    // hasOwn, not ``in``: "toString" and friends are not commands.
    if (typeof type !== "string" || !Object.hasOwn(ALLOWED, type)) {
      return errorReply(400, "unknown_command", "unknown command type");
    }
    // 2. Allowlisted fields, then what the BFF owns.
    const cmd: WireMessage = {dir: "cmd", type};
    for (const field of ALLOWED[type]) {
      if (field in body) {
        cmd[field] = body[field];
      }
    }
    const key = keyOf(cmd);
    // 3. Schema; answers and skips must name their question (08 API-05 3).
    //    The id is taken here (the schema wants one); a refused command just skips a number.
    const id = `web-${this.next++}`;
    const candidate = {...cmd, id, ...(type.startsWith("session.") ? {} : {source: SOURCE})};
    if (commandSchemaError(candidate) || (Object.hasOwn(KEY_FIELD, type) && !key)) {
      return errorReply(400, "schema_invalid", "command does not match the Wire schema");
    }
    // 4–5. Pending (state module).
    if (key) {
      const problem = this.state.check(key);
      if (problem) {
        return errorReply(409, problem, problem === "in_flight" ? "already being answered" : "nothing pending with that id");
      }
    }
    // 6. Child alive.
    if (!this.sink.alive()) {
      return this.unavailable(id);
    }
    // 7. Send. From the pending check to the waiter registration there is no await.
    if (!this.sink.send(candidate)) {
      return this.unavailable(id);
    }
    if (key) {
      this.owner.set(key, id);
      this.state.setResolving(key, true);
    }
    const raw = await new Promise<Reply>((settle) => {
      const timer = setTimeout(() => {
        this.inflight.delete(id);
        this.rememberUnknown(id, key);
        settle({
          status: 504,
          body: {error: {code: "timeout", message: "no result in time — it may already have been applied", cmd_id: id}},
        });
      }, this.opts.timeoutMs);
      this.inflight.set(id, {
        key,
        timer,
        settle: (reply) => {
          clearTimeout(timer);
          this.inflight.delete(id);
          settle(reply);
        },
      });
    });
    // 8. Verdict.
    if (raw.status !== 200) {
      return raw;
    }
    return this.success(candidate, raw.body as WireMessage);
  }

  /** API-05 ``POST /api/cmd`` (``X-Augury-Tab`` header required). */
  route(): [string, Route] {
    return [
      "POST /api/cmd",
      {
        method: "POST",
        auth: true,
        handle: ({headers, body}) => this.handle(headers["x-augury-tab"], body ?? {}),
      },
    ];
  }
}
