import assert from "node:assert/strict";
import {afterEach, test} from "node:test";
import {start, startOnce, tabId, takeFragmentToken} from "./connection.js";

// Minimal browser globals for code that runs in the page.
const g = globalThis as Record<string, unknown>;
const saved = {fetch: g.fetch, location: g.location, history: g.history, sessionStorage: g.sessionStorage};

function storage(): Storage {
  const data = new Map<string, string>();
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, String(v)),
    removeItem: (k: string) => void data.delete(k),
    clear: () => data.clear(),
    key: () => null,
    get length() {
      return data.size;
    },
  } as Storage;
}

function page(hash: string) {
  const replaced: string[] = [];
  g.location = {hash, pathname: "/"};
  g.history = {replaceState: (_s: unknown, _t: string, url: string) => replaced.push(url)};
  g.sessionStorage = storage();
  return replaced;
}

afterEach(() => Object.assign(g, saved));

test("the fragment token leaves the address bar before anything else", () => {
  const replaced = page("#token=abc_DEF-123");
  assert.equal(takeFragmentToken(), "abc_DEF-123");
  assert.deepEqual(replaced, ["/"]);
  page("");
  assert.equal(takeFragmentToken(), null);
});

test("start: BFF unreachable → failed screen, not a blank page", async () => {
  page("#token=t");
  g.fetch = async () => {
    throw new TypeError("fetch failed");
  };
  assert.deepEqual(await start("t"), {kind: "failed"});
});

test("start: 401 from the session API → expired; a used token (401 on exchange) is fine", async () => {
  page("");
  const calls: string[] = [];
  g.fetch = async (url: string) => {
    calls.push(String(url));
    return new Response(null, {status: 401});
  };
  assert.deepEqual(await start("used-token"), {kind: "expired"});
  assert.equal(calls[0], "/api/auth/exchange");
  assert.match(calls[1], /^\/api\/session\?tab=[0-9a-f-]{36}$/);
});

test("start: ok carries the tab id and session info", async () => {
  page("");
  const info = {gateway: {status: "running"}, seq_last: 0, flags: {}, dev_mode: false, tab: {controller: true}, streams_available: 3};
  g.fetch = async () => new Response(JSON.stringify(info), {status: 200});
  const got = await start(null);
  assert.equal(got.kind, "ok");
  assert.ok(got.kind === "ok" && got.tab.length === 36);
});

test("tab id survives a reload (nobody else claims it)", async () => {
  page("");
  const kept = "0f0f0f0f-1111-4222-8333-444444444444";
  sessionStorage.setItem("augury.tab_id", kept);
  assert.equal(await tabId(), kept);
});

test("a duplicated tab (same sessionStorage copy, original still open) gets a new id (08 §7.3)", async () => {
  page("");
  const original = "0f0f0f0f-1111-4222-8333-555555555555";
  // The original tab, in another page: it answers for its id.
  const other = new BroadcastChannel("augury-tab");
  other.onmessage = (ev: MessageEvent) => {
    if (ev.data?.type === "who" && ev.data.id === original) other.postMessage({type: "mine", id: original});
  };
  try {
    sessionStorage.setItem("augury.tab_id", original);
    const duplicate = await tabId();
    assert.notEqual(duplicate, original);
    assert.equal(sessionStorage.getItem("augury.tab_id"), duplicate);
  } finally {
    other.close();
  }
});

test("asking twice in the same page (StrictMode) keeps the id: our own answerer is closed, not counted", async () => {
  page("");
  const first = await tabId();
  assert.equal(await tabId(), first);
  assert.equal(await tabId(), first);
});

test("startOnce: one token take and one exchange, shared by StrictMode's second effect", async () => {
  const replaced = page("#token=tok1");
  const info = {gateway: {status: "running"}, seq_last: 0, flags: {}, dev_mode: false, tab: {controller: true}, streams_available: 3};
  const calls: Array<{url: string; body?: string}> = [];
  g.fetch = async (url: string, init?: RequestInit) => {
    calls.push({url: String(url), body: init?.body as string | undefined});
    return new Response(JSON.stringify(info), {status: 200});
  };
  const a = startOnce();
  const b = startOnce(); // the second mount: the fragment is already gone
  assert.equal(a, b);
  assert.equal((await a).kind, "ok");
  assert.deepEqual(replaced, ["/"]);
  const exchanges = calls.filter((c) => c.url === "/api/auth/exchange");
  assert.deepEqual(exchanges.map((c) => c.body), [JSON.stringify({token: "tok1"})]);
  assert.equal(calls.filter((c) => c.url.startsWith("/api/session")).length, 1);
  assert.equal(calls[calls.length - 1].url.startsWith("/api/session"), true); // after the exchange
});
