import assert from "node:assert/strict";
import {request as httpRequest, type IncomingMessage} from "node:http";
import {mkdtempSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {test} from "node:test";
import {startServer, type BffServer} from "./http.js";
import {EventHub, forwardChildLine, type HubOptions} from "./sse.js";
import {parseChildLine} from "./wire.js";

const TOKEN = "s".repeat(43);
const TAB = ["1", "2", "3", "4", "5"].map((d) => `${d.repeat(8)}-aaaa-4aaa-8aaa-${"a".repeat(12)}`);
const root = mkdtempSync(join(tmpdir(), "augury-web-sse-"));

type Frame = {id?: string; event?: string; data?: string; comment?: string; retry?: string};

/** Minimal SSE reader: collects frames as they arrive. */
class Stream {
  frames: Frame[] = [];
  ended = false;
  status = 0;
  res!: IncomingMessage;
  private buf = "";

  static open(port: number, cookie: string, tab: string, lastEventId?: string): Promise<Stream> {
    const s = new Stream();
    return new Promise((resolve, reject) => {
      const req = httpRequest(
        {
          host: "127.0.0.1",
          port,
          path: `/api/events?tab=${tab}`,
          headers: {Cookie: cookie, Accept: "text/event-stream", ...(lastEventId ? {"Last-Event-ID": lastEventId} : {})},
        },
        (res) => {
          s.res = res;
          s.status = res.statusCode ?? 0;
          res.setEncoding("utf8");
          res.on("data", (chunk: string) => s.feed(chunk));
          res.on("close", () => (s.ended = true));
          resolve(s);
        },
      );
      req.on("error", reject);
      req.end();
    });
  }

  private feed(chunk: string): void {
    this.buf += chunk;
    let i: number;
    while ((i = this.buf.indexOf("\n\n")) !== -1) {
      const block = this.buf.slice(0, i);
      this.buf = this.buf.slice(i + 2);
      const f: Frame = {};
      for (const line of block.split("\n")) {
        if (line.startsWith(":")) {
          f.comment = line.slice(1).trim();
        } else {
          const [k, ...v] = line.split(": ");
          (f as Record<string, string>)[k] = v.join(": ");
        }
      }
      this.frames.push(f);
    }
  }

  events(name: string): Frame[] {
    return this.frames.filter((f) => f.event === name);
  }

  close(): void {
    this.res.destroy();
  }
}

async function until(cond: () => boolean, ms = 3000): Promise<void> {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) {
      throw new Error("timed out");
    }
    await new Promise((r) => setTimeout(r, 10));
  }
}

async function setup(options: HubOptions = {}): Promise<{bff: BffServer; hub: EventHub; cookie: string}> {
  const hub = new EventHub(options);
  const bff = await startServer({token: TOKEN, staticRoot: root, routes: [hub.route()], onRevoke: () => {}});
  const cookie = await new Promise<string>((resolve, reject) => {
    const req = httpRequest(
      {
        host: "127.0.0.1",
        port: bff.port,
        method: "POST",
        path: "/api/auth/exchange",
        headers: {"Content-Type": "application/json", Origin: `http://127.0.0.1:${bff.port}`},
      },
      (res) => {
        res.resume();
        resolve(String(res.headers["set-cookie"]).split(";")[0]);
      },
    );
    req.on("error", reject);
    req.end(JSON.stringify({token: TOKEN}));
  });
  return {bff, hub, cookie};
}

async function teardown(bff: BffServer, hub: EventHub): Promise<void> {
  hub.close();
  await bff.close();
}

const ev = (type: string, extra: Record<string, unknown> = {}) => ({dir: "event" as const, type, ...extra});

test("API-04: tab required, cookie required", async () => {
  const {bff, hub, cookie} = await setup();
  assert.equal((await Stream.open(bff.port, cookie, "nope")).status, 400);
  assert.equal((await Stream.open(bff.port, cookie, TAB[0].toUpperCase())).status, 400);
  assert.equal((await Stream.open(bff.port, "augury_web_1=x", TAB[0])).status, 401);
  await teardown(bff, hub);
});

test("stream opens with retry, then bff.state (no id), then live frames with ids", async () => {
  const {bff, hub, cookie} = await setup({snapshot: () => ({gateway: {status: "running"}})});
  const s = await Stream.open(bff.port, cookie, TAB[0]);
  assert.equal(s.status, 200);
  assert.equal(s.res.headers["content-type"], "text/event-stream; charset=utf-8");
  assert.equal(s.res.headers["cache-control"], "no-store");
  await until(() => s.frames.length >= 2);
  assert.deepEqual(s.frames[0], {retry: "2000"});
  assert.equal(s.frames[1].event, "bff.state");
  assert.equal(s.frames[1].id, undefined);
  assert.deepEqual(JSON.parse(s.frames[1].data!), {gateway: {status: "running"}, seq_last: 0, replay_gap: null});

  hub.publishWire(ev("message", {content: "line1\nline2"}));
  await until(() => s.events("wire").length === 1);
  const w = s.events("wire")[0];
  assert.equal(w.id, "1");
  assert.deepEqual(JSON.parse(w.data!), ev("message", {content: "line1\nline2"}));
  s.close();
  await teardown(bff, hub);
});

test("replay: after Last-Event-ID, everything without it, everything for a foreign seq", async () => {
  const {bff, hub, cookie} = await setup();
  for (let i = 0; i < 3; i++) {
    hub.publishWire(ev("log", {text: String(i)}));
  }
  const ids = async (last?: string) => {
    const s = await Stream.open(bff.port, cookie, TAB[0], last);
    await until(() => s.frames.some((f) => f.event === "bff.state"));
    await new Promise((r) => setTimeout(r, 30));
    s.close();
    await until(() => s.ended);
    return s.events("wire").map((f) => f.id);
  };
  assert.deepEqual(await ids("1"), ["2", "3"]);
  assert.deepEqual(await ids(), ["1", "2", "3"]);
  assert.deepEqual(await ids("99"), ["1", "2", "3"]);
  assert.deepEqual(await ids("junk"), ["1", "2", "3"]);
  assert.deepEqual(await ids("3"), []);
  await teardown(bff, hub);
});

test("ring buffer: frame cap and byte cap trim the front; the gap is reported once", async () => {
  const {bff, hub, cookie} = await setup({maxFrames: 3});
  for (let i = 0; i < 5; i++) {
    hub.publishWire(ev("log", {text: String(i)}));
  }
  const s = await Stream.open(bff.port, cookie, TAB[0], "1");
  await until(() => s.events("wire").length === 3);
  const state = JSON.parse(s.events("bff.state")[0].data!);
  assert.deepEqual(state.replay_gap, {from: 2, to: 2});
  assert.deepEqual(s.events("wire").map((f) => f.id), ["3", "4", "5"]);
  hub.broadcastState();
  await until(() => s.events("bff.state").length === 2);
  assert.equal(JSON.parse(s.events("bff.state")[1].data!).replay_gap, null);
  s.close();
  await teardown(bff, hub);

  const small = await setup({maxBytes: 400});
  for (let i = 0; i < 5; i++) {
    small.hub.publishWire(ev("log", {text: "x".repeat(100)}));
  }
  const t = await Stream.open(small.bff.port, small.cookie, TAB[0]);
  await until(() => t.events("bff.state").length === 1);
  await new Promise((r) => setTimeout(r, 30));
  // Each frame is ~150 bytes, so a 400-byte cap keeps the last two.
  assert.deepEqual(JSON.parse(t.events("bff.state")[0].data!).replay_gap, {from: 1, to: 3});
  assert.deepEqual(t.events("wire").map((f) => f.id), ["4", "5"]);
  t.close();
  await teardown(small.bff, small.hub);
});

test("notices share the seq, are masked and clipped", async () => {
  const {bff, hub, cookie} = await setup({now: () => 42});
  const s = await Stream.open(bff.port, cookie, TAB[0]);
  hub.publishWire(ev("log", {text: "a"}));
  hub.publishNotice({kind: "stderr", level: "warn", text: `Authorization: Bearer sk-live-1 ${"y".repeat(3000)}`});
  await until(() => s.events("bff.notice").length === 1);
  const n = s.events("bff.notice")[0];
  assert.equal(n.id, "2");
  const data = JSON.parse(n.data!);
  assert.equal(data.ts, 42);
  assert.doesNotMatch(data.text, /sk-live-1/);
  assert.ok(data.text.length <= 2001);
  s.close();
  await teardown(bff, hub);
});

test("forwardChildLine: events always shown, schema errors add a notice, results go back", async () => {
  const {bff, hub, cookie} = await setup();
  const s = await Stream.open(bff.port, cookie, TAB[0]);
  assert.equal(forwardChildLine(hub, parseChildLine('{"dir":"event","type":"session.started","agents":"bad"}')), null);
  assert.equal(forwardChildLine(hub, parseChildLine("not json")), null);
  assert.equal(forwardChildLine(hub, {kind: "decode_error", error: "line too long (2000000 bytes)", oversize: true}), null);
  const result = forwardChildLine(hub, parseChildLine('{"dir":"result","id":"web-1","ok":true}'));
  assert.equal(result?.id, "web-1");
  await until(() => s.frames.filter((f) => f.id).length === 4);
  const kinds = s.frames.filter((f) => f.id).map((f) => [f.event, f.data && JSON.parse(f.data).kind, f.data && JSON.parse(f.data).level]);
  assert.deepEqual(kinds, [
    ["wire", undefined, undefined],
    ["bff.notice", "schema_invalid", "error"],
    ["bff.notice", "decode_error", "warn"],
    ["bff.notice", "decode_error", "error"],
  ]);
  s.close();
  await teardown(bff, hub);
});

test("same tab again: the old stream gets bff.replaced and ends; no disconnect fired", async () => {
  const events: string[] = [];
  const {bff, hub, cookie} = await setup({
    onConnect: (t) => events.push(`connect ${t[0]}`),
    onDisconnect: (t) => events.push(`disconnect ${t[0]}`),
  });
  const a = await Stream.open(bff.port, cookie, TAB[0]);
  await until(() => a.events("bff.state").length === 1);
  const b = await Stream.open(bff.port, cookie, TAB[0]);
  await until(() => a.ended && b.events("bff.state").length === 1);
  assert.equal(a.events("bff.replaced").length, 1);
  assert.deepEqual(hub.connectedTabs(), [TAB[0]]);
  b.close();
  await until(() => hub.connectedTabs().length === 0);
  assert.deepEqual(events, ["connect 1", "disconnect 1"]);
  await teardown(bff, hub);
});

test("onConnect runs before the first snapshot", async () => {
  let controller = "";
  const {bff, hub, cookie} = await setup({
    onConnect: (t) => (controller ||= t),
    snapshot: (t) => ({is_controller: controller === t}),
  });
  const s = await Stream.open(bff.port, cookie, TAB[0]);
  await until(() => s.events("bff.state").length === 1);
  assert.equal(JSON.parse(s.events("bff.state")[0].data!).is_controller, true);
  s.close();
  await teardown(bff, hub);
});

test("stream cap: the 5th tab gets 429, a replacement does not count, a freed slot reopens", async () => {
  const {bff, hub, cookie} = await setup();
  const open = [];
  for (let i = 0; i < 4; i++) {
    open.push(await Stream.open(bff.port, cookie, TAB[i]));
  }
  await until(() => hub.available() === 0);
  assert.equal((await Stream.open(bff.port, cookie, TAB[4])).status, 429);
  const again = await Stream.open(bff.port, cookie, TAB[0]);
  assert.equal(again.status, 200);
  open[1].close();
  await until(() => hub.available() === 1);
  const fifth = await Stream.open(bff.port, cookie, TAB[4]);
  assert.equal(fifth.status, 200);
  for (const s of [...open, again, fifth]) {
    s.close();
  }
  await teardown(bff, hub);
});

test("a client that stops reading is cut off instead of growing the BFF", async () => {
  const {bff, hub, cookie} = await setup({writableLimit: 64 * 1024});
  const s = await Stream.open(bff.port, cookie, TAB[0]);
  await until(() => s.events("bff.state").length === 1);
  s.res.pause();
  const chunk = "z".repeat(32 * 1024);
  for (let i = 0; i < 400 && hub.connectedTabs().length === 1; i++) {
    hub.publishWire(ev("log", {text: chunk}));
    await new Promise((r) => setImmediate(r));
  }
  await until(() => hub.connectedTabs().length === 0, 5000);
  s.res.resume();
  await until(() => s.ended, 5000);
  await teardown(bff, hub);
});

test("a replay larger than the live limit is delivered whole", async () => {
  const {bff, hub, cookie} = await setup({writableLimit: 64 * 1024});
  const chunk = "r".repeat(32 * 1024);
  for (let i = 0; i < 64; i++) {
    hub.publishWire(ev("log", {text: chunk}));
  }
  const s = await Stream.open(bff.port, cookie, TAB[0]);
  await until(() => s.events("wire").length === 64, 5000);
  assert.equal(s.ended, false);
  // After the replay drained, the normal limit applies again.
  hub.publishWire(ev("log", {text: "live"}));
  await until(() => s.events("wire").length === 65);
  s.close();
  await teardown(bff, hub);
});

test("frame injection: CR, LF, U+2028 and fake SSE fields stay inside one data line", async () => {
  const {bff, hub, cookie} = await setup();
  const s = await Stream.open(bff.port, cookie, TAB[0]);
  await until(() => s.events("bff.state").length === 1);
  const content = "a\nb\r\nc\u2028d\n\nid: 999\nevent: bff.state\ndata: {}";
  hub.publishWire(ev("message", {content}));
  hub.publishNotice({kind: "stderr", level: "warn", text: content});
  await until(() => s.events("bff.notice").length === 1);
  assert.equal(s.events("bff.state").length, 1);
  assert.equal(s.events("wire").length, 1);
  assert.equal(JSON.parse(s.events("wire")[0].data!).content, content);
  assert.deepEqual(s.frames.filter((f) => f.id).map((f) => f.id), ["1", "2"]);
  s.close();
  await teardown(bff, hub);
});

test("notice: every string field is masked; clipping never splits a surrogate pair", async () => {
  const {bff, hub, cookie} = await setup();
  const s = await Stream.open(bff.port, cookie, TAB[0]);
  hub.publishNotice({kind: "late_result", level: "info", text: "ok", detail: "Authorization: Bearer sk-detail"});
  hub.publishNotice({kind: "stderr", level: "warn", text: `${"a".repeat(1999)}😀tail`});
  await until(() => s.events("bff.notice").length === 2);
  const [a, b] = s.events("bff.notice").map((f) => JSON.parse(f.data!));
  assert.doesNotMatch(a.detail, /sk-detail/);
  assert.equal(b.text, `${"a".repeat(1999)}…`);
  s.close();
  await teardown(bff, hub);
});

test("after hub.close() new streams are ended at once", async () => {
  const {bff, hub, cookie} = await setup();
  hub.close();
  const s = await Stream.open(bff.port, cookie, TAB[0]);
  await until(() => s.ended);
  assert.deepEqual(hub.connectedTabs(), []);
  await bff.close();
});

test("keep-alive pings", async () => {
  const {bff, hub, cookie} = await setup({pingMs: 20});
  const s = await Stream.open(bff.port, cookie, TAB[0]);
  await until(() => s.frames.some((f) => f.comment === "ping"));
  s.close();
  await teardown(bff, hub);
});

test("BFF shutdown ends open streams", async () => {
  const {bff, hub, cookie} = await setup();
  const s = await Stream.open(bff.port, cookie, TAB[0]);
  await until(() => s.events("bff.state").length === 1);
  await teardown(bff, hub);
  await until(() => s.ended);
});
