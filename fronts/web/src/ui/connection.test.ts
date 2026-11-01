import assert from "node:assert/strict";
import {afterEach, test} from "node:test";
import {start, tabId, takeFragmentToken} from "./connection.js";

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
  const original = await tabId(); // this page now answers for its id
  const copy = storage();
  copy.setItem("augury.tab_id", original);
  g.sessionStorage = copy;
  const duplicate = await tabId();
  assert.notEqual(duplicate, original);
  assert.equal(copy.getItem("augury.tab_id"), duplicate);
});
