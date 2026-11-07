/** SSE event stream for the BFF (WBS 2.5, 08 §4 · API-04).
 *
 * ``wire`` and ``bff.notice`` frames share one seq space and one ring buffer;
 * child stdout order = seq order = send order (N-006). ``bff.state`` carries no
 * ``id:`` so it never moves the browser's Last-Event-ID, and is not buffered —
 * a (re)connecting tab gets a fresh snapshot first, then the replay.
 */

import type {ServerResponse} from "node:http";
import {errorReply, type Route} from "./http.js";
import {maskSensitive, type ChildLine, type WireMessage} from "./wire.js";

/** 08 §4.1 / §4.3 values (all [가정]). */
export const SSE_DEFAULTS = {
  maxFrames: 5000,
  maxBytes: 16 * 1024 * 1024,
  maxStreams: 4,
  pingMs: 15_000,
  /** Buffered output that has not drained for this long means a dead (half-open) peer. */
  stallMs: 60_000,
  retryMs: 2000,
  writableLimit: 4 * 1024 * 1024,
};

const NOTICE_TEXT_MAX = 2000;
const TAB_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export type Notice = {
  kind: string;
  level: "info" | "warn" | "error";
  text: string;
  [field: string]: unknown;
};

export type HubOptions = Partial<typeof SSE_DEFAULTS> & {
  /** ``bff.state`` body for one tab (08 §4.4); the hub adds ``seq_last``/``replay_gap``. */
  snapshot?: (tab: string) => Record<string, unknown>;
  /** A tab got its first stream. Runs before that stream's snapshot (08 §7 T1). */
  onConnect?: (tab: string) => void;
  /** A tab's stream ended and was not replaced. */
  onDisconnect?: (tab: string) => void;
  now?: () => number;
};

type Frame = {seq: number; text: string; bytes: number};
/** ``replayAllowance``: bytes of the initial replay still allowed in the socket buffer. */
/** ``stalledAt``: when a ping first found output still buffered; cleared on ``drain``. */
type Conn = {tab: string; res: ServerResponse; closed: boolean; replayAllowance: number; stalledAt?: number};
type Gap = {from: number; to: number};

/** Mask every string, however deep (08 §4.5). */
function maskDeep(value: unknown): unknown {
  if (typeof value === "string") {
    return maskSensitive(value);
  }
  if (Array.isArray(value)) {
    return value.map(maskDeep);
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, maskDeep(v)]));
  }
  return value;
}

function frame(event: string, data: string, id?: number): string {
  return `${id === undefined ? "" : `id: ${id}\n`}event: ${event}\ndata: ${data}\n\n`;
}

export class EventHub {
  private readonly opts: typeof SSE_DEFAULTS;
  private readonly snapshot: (tab: string) => Record<string, unknown>;
  private readonly onConnect: (tab: string) => void;
  private readonly onDisconnect: (tab: string) => void;
  private readonly now: () => number;
  private frames: Frame[] = [];
  private bytes = 0;
  private seq = 0;
  private conns = new Map<string, Conn>();
  private ping: NodeJS.Timeout;
  private shut = false;

  constructor(options: HubOptions = {}) {
    this.opts = {...SSE_DEFAULTS, ...options};
    this.snapshot = options.snapshot ?? (() => ({}));
    this.onConnect = options.onConnect ?? (() => {});
    this.onDisconnect = options.onDisconnect ?? (() => {});
    this.now = options.now ?? Date.now;
    this.ping = setInterval(() => {
      for (const conn of this.conns.values()) {
        this.checkStall(conn);
        this.write(conn, ": ping\n\n");
      }
    }, this.opts.pingMs).unref();
  }

  get seqLast(): number {
    return this.seq;
  }

  /** Free SSE slots (API-03 ``streams_available``). */
  available(): number {
    return Math.max(0, this.opts.maxStreams - this.conns.size);
  }

  connectedTabs(): string[] {
    return [...this.conns.keys()];
  }

  /** Write or drop: a client that cannot keep up is cut off and replays on reconnect (08 §4.1).
   *
   * The ring buffer (16 MiB) is larger than the live limit (4 MiB), so the
   * initial replay is allowed on top of the limit until the socket drains once;
   * otherwise a big replay would be cut at the same point on every reconnect.
   */
  private write(conn: Conn, text: string): void {
    if (conn.closed) {
      return;
    }
    conn.res.write(text);
    if (conn.res.writableLength > this.opts.writableLimit + conn.replayAllowance) {
      conn.closed = true;
      conn.res.destroy();
    }
  }

  /** A half-open peer never acks, so its buffer never drains: free its slot (maxStreams). */
  private checkStall(conn: Conn): void {
    if (conn.res.writableLength === 0) {
      conn.stalledAt = undefined;
    } else if (conn.stalledAt === undefined) {
      conn.stalledAt = this.now();
    } else if (this.now() - conn.stalledAt >= this.opts.stallMs) {
      conn.closed = true;
      conn.res.destroy();
    }
  }

  private append(event: "wire" | "bff.notice", data: string): void {
    const seq = ++this.seq;
    const text = frame(event, data, seq);
    const bytes = Buffer.byteLength(text);
    this.frames.push({seq, text, bytes});
    this.bytes += bytes;
    while (this.frames.length > this.opts.maxFrames || (this.bytes > this.opts.maxBytes && this.frames.length > 1)) {
      this.bytes -= this.frames.shift()!.bytes;
    }
    for (const conn of this.conns.values()) {
      this.write(conn, text);
    }
  }

  publishWire(message: WireMessage): void {
    this.append("wire", JSON.stringify(message));
  }

  /** ``bff.notice`` (08 §4.5): every string (nested too) masked, text clipped, stamped with ``ts``. */
  publishNotice(notice: Notice): void {
    const out = maskDeep(notice) as Record<string, unknown>;
    let text = out.text as string;
    if (text.length > NOTICE_TEXT_MAX) {
      let end = NOTICE_TEXT_MAX;
      const code = text.charCodeAt(end - 1);
      if (code >= 0xd800 && code <= 0xdbff) {
        end -= 1; // do not split a surrogate pair
      }
      text = `${text.slice(0, end)}…`;
    }
    this.append("bff.notice", JSON.stringify({...out, text, ts: this.now()}));
  }

  private stateFrame(tab: string, gap: Gap | null): string {
    return frame("bff.state", JSON.stringify({...this.snapshot(tab), seq_last: this.seq, replay_gap: gap}));
  }

  /** Send every tab a fresh ``bff.state``. Callers send it right after the frames that changed it. */
  broadcastState(): void {
    for (const conn of this.conns.values()) {
      this.write(conn, this.stateFrame(conn.tab, null));
    }
  }

  /** Register, snapshot and replay in one synchronous block (08 §4.3) — nothing can slip in between. */
  private attach(tab: string, lastEventId: string | undefined, res: ServerResponse): void {
    let n = lastEventId !== undefined && /^\d+$/.test(lastEventId) ? Number(lastEventId) : 0;
    if (n > this.seq) {
      n = 0; // another seq space (BFF restarted): replay everything
    }
    const min = this.frames.length ? this.frames[0].seq : this.seq + 1;
    // A first connect (no Last-Event-ID) missed nothing, so a trimmed buffer front is no gap.
    const gap = lastEventId !== undefined && n + 1 < min ? {from: n + 1, to: min - 1} : null;
    const replay = gap ? this.frames : this.frames.filter((f) => f.seq > n);

    if (this.shut) {
      res.end();
      return;
    }
    const old = this.conns.get(tab);
    if (old && !old.closed) {
      // Same tab again (reload, or a duplicated tab): tell the old stream not to reconnect.
      this.write(old, frame("bff.replaced", "{}"));
      old.closed = true;
      old.res.end();
    }
    const conn: Conn = {tab, res, closed: false, replayAllowance: 0};
    this.conns.set(tab, conn);
    res.socket?.setKeepAlive(true, this.opts.pingMs);
    res.on("drain", () => (conn.stalledAt = undefined));
    res.on("close", () => {
      conn.closed = true;
      if (this.conns.get(tab) === conn) {
        this.conns.delete(tab);
        this.onDisconnect(tab);
      }
    });
    if (!old) {
      this.onConnect(tab);
    }
    const state = this.stateFrame(tab, gap); // after onConnect (08 §7 T1)
    conn.replayAllowance = Buffer.byteLength(state) + replay.reduce((sum, f) => sum + f.bytes, 0);
    res.write(`retry: ${this.opts.retryMs}\n\n`);
    this.write(conn, state);
    for (const f of replay) {
      this.write(conn, f.text);
    }
    if (res.writableLength === 0) {
      conn.replayAllowance = 0;
    } else {
      res.once("drain", () => (conn.replayAllowance = 0));
    }
  }

  /** API-04 ``GET /api/events?tab=<uuid>``.
   *
   * The handler is synchronous on purpose: between the slot check and ``attach``
   * there is only a microtask (``await handle()``), never an I/O turn, so two
   * connects cannot both take the last slot.
   */
  route(): [string, Route] {
    return [
      "GET /api/events",
      {
        method: "GET",
        auth: true,
        handle: ({query, headers}) => {
          const tab = query.get("tab") ?? "";
          if (!TAB_RE.test(tab)) {
            return errorReply(400, "bad_request", "tab must be a lowercase UUID v4");
          }
          if (!this.conns.has(tab) && this.conns.size >= this.opts.maxStreams) {
            return errorReply(429, "too_many_streams", "too many open tabs");
          }
          const lastEventId = headers["last-event-id"];
          return {
            status: 200,
            headers: {"Content-Type": "text/event-stream; charset=utf-8", Connection: "keep-alive"},
            stream: (res) => this.attach(tab, typeof lastEventId === "string" ? lastEventId : undefined, res),
          };
        },
      },
    ];
  }

  /** End every stream (BFF shutdown). The Gateway child exiting does not call this (N-007). */
  close(): void {
    this.shut = true;
    clearInterval(this.ping);
    for (const conn of this.conns.values()) {
      conn.closed = true;
      conn.res.end();
    }
    this.conns.clear();
  }
}

/** Route one child stdout line to the stream; results go back to the caller (cmd module).
 *
 * A schema-invalid event is still shown (Ink shows everything — 08 §4.2, review
 * S8) but gets an error notice; state modules must check ``schemaError`` first.
 */
export function forwardChildLine(hub: EventHub, line: ChildLine): WireMessage | null {
  if (line.kind === "result") {
    return line.message;
  }
  if (line.kind === "event") {
    hub.publishWire(line.message);
    if (line.schemaError) {
      hub.publishNotice({kind: "schema_invalid", level: "error", text: line.schemaError});
    }
    return null;
  }
  hub.publishNotice({kind: "decode_error", level: line.oversize ? "error" : "warn", text: line.error});
  return null;
}
