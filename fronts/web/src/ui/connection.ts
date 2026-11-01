/** Browser side of the BFF contract (08 API-02/03/04/06 · §4.3 · §7.3). */

import type {WireMessage} from "../../../ink/src/wire.js";
import type {BffState, Notice} from "./model.js";

const TAB_KEY = "augury.tab_id";
/** How long other tabs get to say "that id is mine" (08 §7.3, [가정]). */
const DUPLICATE_WAIT_MS = 300;

/**
 * This tab's id: kept in sessionStorage so a reload keeps controller status.
 * "Duplicate tab" copies sessionStorage too, so ask the other tabs first and
 * take a fresh id if one of them already uses ours.
 */
export async function tabId(): Promise<string> {
  let id = sessionStorage.getItem(TAB_KEY) ?? crypto.randomUUID();
  if (typeof BroadcastChannel !== "undefined") {
    const channel = new BroadcastChannel("augury-tab");
    const taken = await new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => resolve(false), DUPLICATE_WAIT_MS);
      channel.onmessage = (ev: MessageEvent) => {
        if (ev.data?.type === "mine" && ev.data.id === id) {
          clearTimeout(timer);
          resolve(true);
        }
      };
      channel.postMessage({type: "who", id});
    });
    if (taken) {
      id = crypto.randomUUID();
    }
    // From now on, answer for our own id.
    channel.onmessage = (ev: MessageEvent) => {
      if (ev.data?.type === "who" && ev.data.id === id) {
        channel.postMessage({type: "mine", id});
      }
    };
    // Node (tests) keeps a process alive for an open channel; browsers have no unref.
    (channel as unknown as {unref?: () => void}).unref?.();
  }
  sessionStorage.setItem(TAB_KEY, id);
  return id;
}

/** API-02 step 1–2: take the token out of the fragment and the address bar, before anything else. */
export function takeFragmentToken(): string | null {
  const match = /(?:^#|&)token=([A-Za-z0-9_-]+)/.exec(location.hash);
  if (location.hash) {
    history.replaceState(null, "", location.pathname);
  }
  return match ? match[1] : null;
}

/** API-02 step 3. A 401 is fine: another tab may have exchanged it already (the cookie still works). */
export async function exchangeToken(token: string | null): Promise<void> {
  if (token === null) {
    return;
  }
  await fetch("/api/auth/exchange", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({token}),
  });
}

export type Session = {
  gateway: BffState["gateway"];
  seq_last: number;
  flags: {demo: boolean; no_auto_start: boolean; plan_first: boolean; session: string | null};
  dev_mode: boolean;
  tab: {controller: boolean};
  streams_available: number;
};

/** API-03. Null on 401 (link used up / BFF restarted → B-6). */
export async function session(tab: string): Promise<Session | null> {
  const res = await fetch(`/api/session?tab=${tab}`);
  if (res.status === 401) {
    return null;
  }
  if (!res.ok) {
    throw new Error(`session: HTTP ${res.status}`);
  }
  return (await res.json()) as Session;
}

/** API-06: true/false, or null when this tab has no live stream (409). */
export async function claim(tab: string, force: boolean): Promise<boolean | null> {
  const res = await fetch("/api/control/claim", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({tab_id: tab, force}),
  });
  if (res.status === 409) {
    return null;
  }
  if (!res.ok) {
    throw new Error(`claim: HTTP ${res.status}`);
  }
  return ((await res.json()) as {controller: boolean}).controller;
}

export type Start = {kind: "ok"; tab: string; info: Session} | {kind: "expired"} | {kind: "failed"};

/**
 * Page start-up: token out of the URL first, then (in parallel) the tab id and
 * the exchange, then API-03. Any failure lands on a screen, never a blank page.
 */
export async function start(token: string | null): Promise<Start> {
  try {
    const [tab] = await Promise.all([tabId(), exchangeToken(token)]);
    const info = await session(tab);
    return info === null ? {kind: "expired"} : {kind: "ok", tab, info};
  } catch {
    return {kind: "failed"};
  }
}

export type CmdResult =
  | {kind: "ok"; result: Record<string, unknown>; failed: boolean; failure: string | null}
  | {kind: "http"; status: number; code: string; cmdId: string | null}
  | {kind: "network"};

/** API-05. The tab id rides X-Augury-Tab; the BFF owns dir, id and source. */
export async function sendCommand(tab: string, body: Record<string, unknown>): Promise<CmdResult> {
  let res: Response;
  try {
    res = await fetch("/api/cmd", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Augury-Tab": tab},
      body: JSON.stringify(body),
    });
  } catch {
    return {kind: "network"};
  }
  const data = (await res.json().catch(() => ({}))) as Record<string, any>;
  if (res.status === 200) {
    return {kind: "ok", result: data.result ?? {}, failed: data.failed === true, failure: data.failure ?? null};
  }
  return {kind: "http", status: res.status, code: String(data.error?.code ?? res.status), cmdId: data.error?.cmd_id ?? null};
}

export type StreamHandlers = {
  wire: (seq: number, e: WireMessage) => void;
  notice: (seq: number, n: Notice) => void;
  state: (s: BffState) => void;
  /** "open" | "reconnecting" | "replaced" | "closed" (the caller asks API-03 why). */
  link: (state: "open" | "reconnecting" | "replaced" | "closed") => void;
};

/** API-04. EventSource resends Last-Event-ID itself on reconnect (08 §4.3). */
export function openStream(tab: string, on: StreamHandlers): () => void {
  const source = new EventSource(`/api/events?tab=${tab}`);
  const parse = (ev: MessageEvent) => {
    try {
      return JSON.parse(ev.data as string);
    } catch {
      return null;
    }
  };
  source.onopen = () => on.link("open");
  // A frame without a usable seq would break de-duplication: drop it.
  const seqOf = (ev: Event) => {
    const seq = Number((ev as MessageEvent).lastEventId);
    return Number.isInteger(seq) && seq > 0 ? seq : null;
  };
  source.addEventListener("wire", (ev) => {
    const e = parse(ev as MessageEvent);
    const seq = seqOf(ev);
    if (e && seq !== null) on.wire(seq, e);
  });
  source.addEventListener("bff.notice", (ev) => {
    const n = parse(ev as MessageEvent);
    const seq = seqOf(ev);
    if (n && seq !== null) on.notice(seq, n);
  });
  source.addEventListener("bff.state", (ev) => {
    const s = parse(ev as MessageEvent);
    if (s) on.state(s);
  });
  // Same tab opened again elsewhere: do not fight it for the stream (08 §4.1).
  source.addEventListener("bff.replaced", () => {
    source.close();
    on.link("replaced");
  });
  source.onerror = () => on.link(source.readyState === EventSource.CLOSED ? "closed" : "reconnecting");
  return () => source.close();
}
