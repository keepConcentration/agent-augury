import assert from "node:assert/strict";
import {mkdirSync, mkdtempSync, symlinkSync, writeFileSync} from "node:fs";
import {connect} from "node:net";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {after, before, test} from "node:test";
import {
  AuthState,
  CSP,
  MAX_EXCHANGE_FAILURES,
  TOKEN_TTL_MS,
  startServer,
  type BffServer,
} from "./http.js";

type Raw = {status: number; headers: Record<string, string>; body: string};

/** Send bytes exactly as given (Host, HTTP version and all) and read until close. */
function raw(port: number, request: string): Promise<Raw> {
  return new Promise((resolve, reject) => {
    // write, not end: a half-closed socket lets the server drop async replies.
    const socket = connect(port, "127.0.0.1", () => socket.write(request));
    const chunks: Buffer[] = [];
    socket.on("data", (c) => chunks.push(c));
    // A refused body is never read: the server closes with our unsent bytes still
    // queued, so the kernel may answer with RST after the reply. Keep the reply.
    socket.on("error", (err: NodeJS.ErrnoException) => {
      if (err.code !== "ECONNRESET" || chunks.length === 0) {
        reject(err);
      }
    });
    socket.on("close", () => {
      const text = Buffer.concat(chunks).toString("utf8");
      const [head, ...rest] = text.split("\r\n\r\n");
      const [statusLine, ...lines] = head.split("\r\n");
      const headers: Record<string, string> = {};
      for (const line of lines) {
        const i = line.indexOf(":");
        headers[line.slice(0, i).toLowerCase()] = line.slice(i + 1).trim();
      }
      resolve({status: Number(statusLine.split(" ")[1]), headers, body: rest.join("\r\n\r\n")});
    });
  });
}

function request(
  port: number,
  method: string,
  path: string,
  headers: Record<string, string | undefined> = {},
  body = "",
): Promise<Raw> {
  const all: Record<string, string | undefined> = {
    Host: `127.0.0.1:${port}`,
    Connection: "close",
    ...(body ? {"Content-Length": String(Buffer.byteLength(body))} : {}),
    ...headers,
  };
  const lines = Object.entries(all)
    .filter(([, v]) => v !== undefined)
    .map(([k, v]) => `${k}: ${v}`);
  return raw(port, `${method} ${path} HTTP/1.1\r\n${lines.join("\r\n")}\r\n\r\n${body}`);
}

const TOKEN = "t".repeat(43);
const root = mkdtempSync(join(tmpdir(), "augury-web-static-"));
mkdirSync(join(root, "assets"));
writeFileSync(join(root, "index.html"), "<!doctype html><title>augury</title>");
writeFileSync(join(root, "assets", "app-abc.js"), "console.log(1)");
writeFileSync(join(root, "assets", "app-B3x_9kQz.js"), "console.log(2)");
writeFileSync(join(root, "assets", "app-abc.js.map"), "{}");
writeFileSync(join(root, "assets", ".hidden.js"), "x");
writeFileSync(join(root, "secret.txt"), "do not serve");
mkdirSync(join(root, "assets", "dir.js"));
const outside = mkdtempSync(join(tmpdir(), "augury-web-outside-"));
writeFileSync(join(outside, "leak.js"), "do not serve");
symlinkSync(join(outside, "leak.js"), join(root, "assets", "link.js"));

let bff: BffServer;
let port: number;
let origin: string;
const json = {"Content-Type": "application/json"};
const post = () => ({...json, Origin: origin, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"});

before(async () => {
  bff = await startServer({
    token: TOKEN,
    staticRoot: root,
    onRevoke: () => {},
    routes: [
      ["GET /api/probe", {method: "GET", auth: true, handle: () => ({status: 200, body: {ok: true}})}],
      ["POST /api/echo", {method: "POST", auth: false, handle: ({body}) => ({status: 200, body})}],
      [
        "GET /api/boom",
        {
          method: "GET",
          auth: false,
          handle: () => {
            throw new Error("secret detail");
          },
        },
      ],
    ],
  });
  port = bff.port;
  origin = `http://127.0.0.1:${port}`;
});

after(async () => {
  await bff.close();
});

test("binds 127.0.0.1 on an OS-chosen port and prints a fragment URL", () => {
  const address = bff.server.address();
  assert.ok(address && typeof address === "object" && address.address === "127.0.0.1");
  assert.ok(port > 0);
  assert.equal(bff.url, `http://127.0.0.1:${port}/#token=${TOKEN}`);
});

test("step 1: wrong, missing or localhost Host and HTTP/1.0 are 403 forbidden_host", async () => {
  for (const host of ["evil.example", "localhost:" + port, `127.0.0.1:${port + 1}`, undefined]) {
    const r = await request(port, "GET", "/", {Host: host});
    assert.equal(r.status, 403, String(host));
    assert.match(r.body, /forbidden_host/);
  }
  const old = await raw(port, `GET / HTTP/1.0\r\nHost: 127.0.0.1:${port}\r\nConnection: close\r\n\r\n`);
  assert.equal(old.status, 403);
});

test("step 2: preflight is refused", async () => {
  const r = await request(port, "OPTIONS", "/api/auth/exchange", {Origin: origin});
  assert.equal(r.status, 403);
  assert.match(r.body, /forbidden_origin/);
  assert.equal(r.headers["access-control-allow-origin"], undefined);
});

test("step 3: fetch metadata combinations (CP-8)", async () => {
  const nav = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"};
  assert.equal((await request(port, "GET", "/", nav)).status, 200);
  assert.equal((await request(port, "GET", "/", {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "no-cors"})).status, 403);
  assert.equal((await request(port, "GET", "/", {"Sec-Fetch-Site": "cross-site"})).status, 403);
  // A model-made link clicked in the timeline: same-origin top-level navigation to the API.
  const linkNav = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"};
  assert.equal((await request(port, "GET", "/api/probe", linkNav)).status, 403);
  assert.equal((await request(port, "GET", "/assets/app-abc.js", {"Sec-Fetch-Site": "none"})).status, 403);
  assert.equal((await request(port, "GET", "/assets/app-abc.js", {"Sec-Fetch-Site": "same-origin"})).status, 200);
});

test("steps 4–5: unknown path 404, wrong method 405 with Allow", async () => {
  assert.equal((await request(port, "GET", "/api/nope")).status, 404);
  const r = await request(port, "GET", "/api/auth/exchange");
  assert.equal(r.status, 405);
  assert.equal(r.headers.allow, "POST");
});

test("step 6: POST needs the exact Origin; GET checks it when present", async () => {
  const body = JSON.stringify({token: "x"});
  assert.equal((await request(port, "POST", "/api/auth/exchange", json, body)).status, 403);
  assert.equal((await request(port, "POST", "/api/auth/exchange", {...json, Origin: "http://127.0.0.1:1"}, body)).status, 403);
  assert.equal((await request(port, "GET", "/", {Origin: "http://evil.example"})).status, 403);
});

test("steps 8–10: media type, size (declared and streamed), JSON object", async () => {
  assert.equal((await request(port, "POST", "/api/echo", {...post(), "Content-Type": "text/plain"}, "{}")).status, 415);
  assert.equal((await request(port, "POST", "/api/echo", {...post(), "Content-Type": "application/json; charset=utf-8"}, "{}")).status, 200);
  assert.equal((await request(port, "POST", "/api/echo", post(), JSON.stringify({a: "x".repeat(70_000)}))).status, 413);
  const big = JSON.stringify({a: "x".repeat(70_000)});
  const chunked = `${Buffer.byteLength(big).toString(16)}\r\n${big}\r\n0\r\n\r\n`;
  const r = await request(port, "POST", "/api/echo", {...post(), "Transfer-Encoding": "chunked", "Content-Length": undefined}, chunked);
  assert.equal(r.status, 413);
  assert.equal((await request(port, "POST", "/api/echo", post(), "[1]")).status, 400);
  assert.equal((await request(port, "POST", "/api/echo", post(), "not json")).status, 400);
  // A body that merely has a "status" field is still a body.
  const echo = await request(port, "POST", "/api/echo", post(), JSON.stringify({status: 500}));
  assert.equal(echo.status, 200);
  assert.deepEqual(JSON.parse(echo.body), {status: 500});
});

test("common headers on success and error, no CORS", async () => {
  for (const r of [await request(port, "GET", "/"), await request(port, "GET", "/api/nope")]) {
    assert.equal(r.headers["content-security-policy"], CSP);
    assert.equal(r.headers["x-content-type-options"], "nosniff");
    assert.equal(r.headers["referrer-policy"], "no-referrer");
    assert.equal(r.headers["cache-control"], "no-store");
    assert.equal(r.headers["access-control-allow-origin"], undefined);
  }
});

test("handler errors are 500 internal_error without details", async () => {
  const r = await request(port, "GET", "/api/boom");
  assert.equal(r.status, 500);
  assert.match(r.body, /internal_error/);
  assert.doesNotMatch(r.body, /secret detail/);
});

test("API-01: index and hashed assets; no traversal, maps, dot-files or directories", async () => {
  const index = await request(port, "GET", "/");
  assert.equal(index.status, 200);
  assert.match(index.headers["content-type"], /text\/html/);
  const asset = await request(port, "GET", "/assets/app-B3x_9kQz.js");
  assert.equal(asset.headers["content-type"], "text/javascript; charset=utf-8");
  assert.equal(asset.headers["cache-control"], "public, max-age=31536000, immutable");
  // No content hash in the name → never immutable.
  const plain = await request(port, "GET", "/assets/app-abc.js");
  assert.equal(plain.status, 200);
  assert.equal(plain.headers["cache-control"], "no-cache");
  for (const path of [
    "/assets/../secret.txt",
    "/assets/%2e%2e/secret.txt",
    "/assets/..%2fsecret.txt",
    "/assets/app-abc.js.map",
    "/assets/.hidden.js",
    "/assets/dir.js",
    "/assets/%E0%A4%A",
    "/assets/link.js",
    "/secret.txt",
  ]) {
    const r = await request(port, "GET", path);
    assert.equal(r.status, 404, path);
    assert.doesNotMatch(r.body, /do not serve/);
  }
});

test("API-02 + step 7: one-time exchange issues the session cookie", async () => {
  const probe = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"};
  assert.equal((await request(port, "GET", "/api/probe", probe)).status, 401);
  assert.equal((await request(port, "POST", "/api/auth/exchange", post(), JSON.stringify({token: 1}))).status, 400);
  assert.equal((await request(port, "POST", "/api/auth/exchange", post(), JSON.stringify({token: "wrong"}))).status, 401);

  const ok = await request(port, "POST", "/api/auth/exchange", post(), JSON.stringify({token: TOKEN}));
  assert.equal(ok.status, 204);
  const cookie = ok.headers["set-cookie"];
  assert.match(cookie, new RegExp(`^augury_web_${port}=[A-Za-z0-9_-]{43}; HttpOnly; SameSite=Strict; Path=/$`));
  const pair = cookie.split(";")[0];

  const again = await request(port, "POST", "/api/auth/exchange", post(), JSON.stringify({token: TOKEN}));
  assert.equal(again.status, 401);
  assert.match(again.body, /invalid_token/);

  assert.equal((await request(port, "GET", "/api/probe", {...probe, Cookie: pair})).status, 200);
  // Another local server may plant a same-name cookie; ours still counts.
  assert.equal((await request(port, "GET", "/api/probe", {...probe, Cookie: `augury_web_${port}=junk; ${pair}`})).status, 200);
  assert.equal((await request(port, "GET", "/api/probe", {...probe, Cookie: `augury_web_${port}=junk`})).status, 401);
  assert.equal((await request(port, "GET", "/api/probe", {...probe, Cookie: `augury_web_1=${pair.split("=")[1]}`})).status, 401);
});

test("WebSocket upgrades: checked, then refused (no WS endpoints yet)", async () => {
  const up = (host: string, o?: string) =>
    raw(
      port,
      `GET /ws HTTP/1.1\r\nHost: ${host}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n` +
        `${o ? `Origin: ${o}\r\n` : ""}Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n`,
    );
  assert.equal((await up("evil.example", origin)).status, 403);
  assert.equal((await up(`127.0.0.1:${port}`, "http://evil.example")).status, 403);
  assert.equal((await up(`127.0.0.1:${port}`, origin)).status, 404);
});

test("checks stop at the first failing step", async () => {
  // Bad Host and bad Origin together → the Host error (step 1) wins.
  const r = await request(port, "POST", "/api/auth/exchange", {...json, Host: "evil.example", Origin: "http://evil.example"}, "{}");
  assert.match(r.body, /forbidden_host/);
  // Bad fetch metadata on an unknown path → 403 (step 3) before 404 (step 4).
  assert.equal((await request(port, "GET", "/api/nope", {"Sec-Fetch-Site": "cross-site"})).status, 403);
  // Wrong method and wrong Origin → 405 (step 5) before 403 (step 6).
  assert.equal((await request(port, "GET", "/api/auth/exchange", {Origin: "http://evil.example"})).status, 405);
  assert.equal((await request(port, "PUT", "/api/auth/exchange")).status, 405);
});

test("step 1a: absolute-form and network-path targets are 400", async () => {
  assert.equal((await request(port, "GET", "http://evil.example/")).status, 400);
  assert.equal((await request(port, "GET", "//evil.example/")).status, 400);
  assert.equal((await request(port, "GET", `http://127.0.0.1:${port}/`)).status, 400);
});

test("exchange body cap is 1 KiB; a declared oversize closes without reading", async () => {
  const r = await request(port, "POST", "/api/auth/exchange", post(), JSON.stringify({token: "x".repeat(1100)}));
  assert.equal(r.status, 413);
  assert.equal(r.headers.connection, "close");
});

test("a refused request with a body is answered with Connection: close, never drained", async () => {
  // Content-Length body on a refused request (wrong Origin) → close, not keep-alive.
  const declared = await raw(
    port,
    `POST /api/echo HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\nContent-Type: application/json\r\n` +
      `Origin: http://evil.example\r\nContent-Length: 2\r\n\r\n{}`,
  );
  assert.equal(declared.status, 403);
  assert.equal(declared.headers.connection, "close");
  // An endless chunked body: the server must close the socket itself instead of
  // reading forever. The client stays half-open and keeps sending.
  const closed = await new Promise<{status: number; connection: string | undefined}>((resolve, reject) => {
    const socket = connect({port, host: "127.0.0.1", allowHalfOpen: true}, () => {
      socket.write(
        `POST /api/nope HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\nContent-Type: application/json\r\n` +
          `Transfer-Encoding: chunked\r\n\r\n4\r\nxxxx\r\n`,
      );
    });
    const pump = setInterval(() => socket.write("4\r\nxxxx\r\n", () => {}), 10);
    const timer = setTimeout(() => reject(new Error("server kept reading the refused body")), 3000);
    const chunks: Buffer[] = [];
    socket.on("data", (c) => chunks.push(c));
    socket.on("error", () => {});
    socket.on("close", () => {
      clearInterval(pump);
      clearTimeout(timer);
      const head = Buffer.concat(chunks).toString("utf8").split("\r\n\r\n")[0];
      const connection = /\r\nconnection: *([^\r]*)/i.exec(head)?.[1];
      resolve({status: Number(head.split(" ")[1]), connection});
    });
  });
  assert.equal(closed.status, 404);
  assert.equal(closed.connection, "close");
});

test("a reset upgrade socket does not crash the server", async () => {
  await new Promise<void>((resolve) => {
    const socket = connect(port, "127.0.0.1", () => {
      socket.write(
        `GET /ws HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\nOrigin: ${origin}\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n`,
      );
      socket.resetAndDestroy();
      resolve();
    });
    socket.on("error", () => {});
  });
  await new Promise((r) => setTimeout(r, 50));
  assert.equal((await request(port, "GET", "/")).status, 200);
});

test("over HTTP: ten failed exchanges revoke the token; the right one then fails", async () => {
  const reasons: string[] = [];
  const s = await startServer({token: TOKEN, staticRoot: root, onRevoke: (r) => reasons.push(r)});
  const o = `http://127.0.0.1:${s.port}`;
  const hdr = {...json, Origin: o, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"};
  for (let i = 0; i < MAX_EXCHANGE_FAILURES; i++) {
    assert.equal((await request(s.port, "POST", "/api/auth/exchange", hdr, JSON.stringify({token: "wrong"}))).status, 401);
  }
  const r = await request(s.port, "POST", "/api/auth/exchange", hdr, JSON.stringify({token: TOKEN}));
  assert.equal(r.status, 401);
  assert.equal(r.headers["set-cookie"], undefined);
  assert.deepEqual(reasons, ["too many failed exchanges"]);
  await s.close();
});

test("over HTTP: an expired token is refused and never yields a cookie", async () => {
  let now = 0;
  const reasons: string[] = [];
  const s = await startServer({token: TOKEN, staticRoot: root, now: () => now, onRevoke: (r) => reasons.push(r)});
  const o = `http://127.0.0.1:${s.port}`;
  now = TOKEN_TTL_MS + 1;
  const r = await request(s.port, "POST", "/api/auth/exchange", {...json, Origin: o}, JSON.stringify({token: TOKEN}));
  assert.equal(r.status, 401);
  assert.equal(r.headers["set-cookie"], undefined);
  assert.deepEqual(reasons, ["expired"]);
  await s.close();
});

test("AuthState: sweep() revokes at the TTL without an exchange", () => {
  let now = 0;
  const reasons: string[] = [];
  const auth = new AuthState("tok", () => now, (r) => reasons.push(r));
  auth.sweep();
  assert.deepEqual(reasons, []);
  now = TOKEN_TTL_MS + 1;
  auth.sweep();
  auth.sweep();
  assert.deepEqual(reasons, ["expired"]);
});

test("AuthState: a used token gives no second cookie", () => {
  const auth = new AuthState("tok");
  const first = auth.exchange("tok");
  assert.ok(first);
  assert.equal(auth.exchange("tok"), null);
  assert.ok(auth.accepts([first]));
});

test("AuthState: token expires after the TTL", () => {
  let now = 0;
  const reasons: string[] = [];
  const auth = new AuthState("tok", () => now, (r) => reasons.push(r));
  now = TOKEN_TTL_MS + 1;
  assert.equal(auth.exchange("tok"), null);
  assert.deepEqual(reasons, ["expired"]);
});

test("AuthState: too many failures revoke the token, once", () => {
  const reasons: string[] = [];
  const auth = new AuthState("tok", Date.now, (r) => reasons.push(r));
  for (let i = 0; i < MAX_EXCHANGE_FAILURES + 2; i++) {
    auth.exchange("nope");
  }
  assert.equal(auth.exchange("tok"), null);
  assert.deepEqual(reasons, ["too many failed exchanges"]);
  assert.equal(auth.accepts(["anything"]), false);
});
