/** HTTP security skeleton for the BFF (WBS 2.4, 08 §2 · API-01 · API-02).
 *
 * Every request runs the same ordered checks (08 §2.1) before any handler, so a
 * request refused here never reaches the Gateway child's stdin (N-002). Later
 * APIs (session, events, cmd, claim) plug in through ``routes``.
 */

import {randomBytes, timingSafeEqual} from "node:crypto";
import {readFile, realpath, stat} from "node:fs/promises";
import {createServer, type IncomingMessage, type Server, type ServerResponse} from "node:http";
import type {Duplex} from "node:stream";
import {basename, extname, join, resolve, sep} from "node:path";
import {performance} from "node:perf_hooks";

/** Body caps (08 §2.1 step 9, [가정]). */
export const MAX_BODY_BYTES = 64 * 1024;
export const MAX_EXCHANGE_BODY_BYTES = 1024;
/** Unexchanged token lifetime and failed-exchange budget (08 §2.4, [가정]). */
export const TOKEN_TTL_MS = 5 * 60 * 1000;
export const MAX_EXCHANGE_FAILURES = 10;

export const CSP =
  "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; " +
  "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

const MIME: Record<string, string> = {
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".woff2": "font/woff2",
  ".png": "image/png",
};

/** Build output names carry a content hash (``app-B3x_9kQz.js``). */
const HASHED_ASSET = /-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$/;

/** 32 random bytes, base64url (≥128 bit — N-002). */
export function newSecret(): string {
  return randomBytes(32).toString("base64url");
}

function sameSecret(a: string, b: string): boolean {
  const x = Buffer.from(a);
  const y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}

/** One-time token → session cookie (API-02). */
export class AuthState {
  private token: string | null;
  private readonly issuedAt: number;
  private failures = 0;
  private cookie: string | null = null;
  private readonly now: () => number;
  private readonly onRevoke: (reason: string) => void;

  /** *now* is monotonic by default so a wall-clock jump cannot stretch the TTL. */
  constructor(token: string, now: () => number = () => performance.now(), onRevoke: (reason: string) => void = () => {}) {
    this.token = token;
    this.now = now;
    this.onRevoke = onRevoke;
    this.issuedAt = now();
  }

  private revoke(reason: string): void {
    if (this.token !== null) {
      this.token = null;
      this.onRevoke(reason);
    }
  }

  /** Revoke an unexchanged token once its TTL has passed. */
  sweep(): void {
    if (this.token !== null && this.now() - this.issuedAt > TOKEN_TTL_MS) {
      this.revoke("expired");
    }
  }

  /** The new cookie value, or null when the token is wrong, used, expired or revoked. */
  exchange(candidate: string): string | null {
    this.sweep();
    if (this.token === null) {
      return null;
    }
    if (!sameSecret(candidate, this.token)) {
      this.failures += 1;
      // The 10th failure revokes ([가정], 08 §2.4).
      if (this.failures >= MAX_EXCHANGE_FAILURES) {
        this.revoke("too many failed exchanges");
      }
      return null;
    }
    this.token = null;
    this.cookie = newSecret();
    return this.cookie;
  }

  /** True when *values* (every cookie of our name) contains the live session. */
  accepts(values: string[]): boolean {
    const cookie = this.cookie;
    return cookie !== null && values.some((v) => sameSecret(v, cookie));
  }
}

export type ErrorCode =
  | "bad_request"
  | "unauthorized"
  | "invalid_token"
  | "forbidden_host"
  | "forbidden_origin"
  | "not_found"
  | "method_not_allowed"
  | "payload_too_large"
  | "unsupported_media_type"
  | "too_many_streams"
  | "internal_error";

export type Reply = {
  status: number;
  headers?: Record<string, string>;
  body?: unknown;
  /** Long-lived response (SSE): called after the headers are written, instead of a body. */
  stream?: (res: ServerResponse) => void;
};

export function errorReply(status: number, code: ErrorCode, message: string, headers?: Record<string, string>): Reply {
  return {status, headers, body: {error: {code, message}}};
}

export type RouteContext = {
  path: string;
  query: URLSearchParams;
  headers: IncomingMessage["headers"];
  /** Parsed JSON object for POST routes. */
  body: Record<string, unknown> | null;
};

export type Route = {
  method: "GET" | "POST";
  /** Needs the session cookie (08 §2.1 step 7). */
  auth: boolean;
  maxBody?: number;
  handle: (ctx: RouteContext) => Reply | Promise<Reply>;
};

type Area = "api" | "asset" | "page";

function areaOf(path: string): Area {
  if (path.startsWith("/api/")) {
    return "api";
  }
  return path.startsWith("/assets/") ? "asset" : "page";
}

/** 08 §2.1 step 3 (CP-8, review S3). Missing headers pass; old browsers send none. */
function fetchMetadataAllowed(area: Area, headers: IncomingMessage["headers"]): boolean {
  const site = headers["sec-fetch-site"];
  const mode = headers["sec-fetch-mode"];
  const dest = headers["sec-fetch-dest"];
  if (area === "api") {
    return (
      (site === undefined || site === "same-origin") &&
      (dest === undefined || dest === "empty") &&
      (mode === undefined || mode === "cors" || mode === "same-origin")
    );
  }
  if (area === "asset") {
    return site === undefined || site === "same-origin";
  }
  // The terminal-opened first page is a top-level navigation with site=none.
  return site === undefined || site === "same-origin" || (site === "none" && mode === "navigate");
}

function cookieValues(header: string | undefined, name: string): string[] {
  if (!header) {
    return [];
  }
  return header
    .split(";")
    .map((part) => part.trim())
    .filter((part) => part.startsWith(`${name}=`))
    .map((part) => part.slice(name.length + 1));
}

function isJson(contentType: string | undefined): boolean {
  return (contentType ?? "").split(";")[0].trim().toLowerCase() === "application/json";
}

export type CheckResult = {route: Route; path: string; query: URLSearchParams} | {reply: Reply};

export type SecurityContext = {
  port: number;
  routes: Map<string, Route>;
  auth: AuthState;
};

/** Steps 1–8 of 08 §2.1, in order; the first failure wins. Body steps run in the server. */
export function checkRequest(
  req: Pick<IncomingMessage, "method" | "url" | "httpVersion" | "headers">,
  ctx: SecurityContext,
): CheckResult {
  const origin = `http://127.0.0.1:${ctx.port}`;
  // 1. Host — exact, and HTTP/1.0 (no mandatory Host) is refused outright.
  //    Node keeps only the first of duplicate Host headers; browsers never send two.
  if (req.httpVersion === "1.0" || req.headers.host !== `127.0.0.1:${ctx.port}`) {
    return {reply: errorReply(403, "forbidden_host", "host not allowed")};
  }
  // 2. CORS preflight is never answered.
  if (req.method === "OPTIONS") {
    return {reply: errorReply(403, "forbidden_origin", "cross-origin requests are not allowed")};
  }
  // 1a. Origin-form targets only: an absolute ("http://x/") or network-path
  //     ("//x/") target would let routing see another host than step 1 checked.
  const target = req.url ?? "";
  if (!target.startsWith("/") || target.startsWith("//")) {
    return {reply: errorReply(400, "bad_request", "bad request target")};
  }
  let url: URL;
  try {
    url = new URL(req.url ?? "/", origin);
  } catch {
    return {reply: errorReply(400, "bad_request", "bad request target")};
  }
  const path = url.pathname;
  // 3. Fetch metadata.
  if (!fetchMetadataAllowed(areaOf(path), req.headers)) {
    return {reply: errorReply(403, "forbidden_origin", "request context not allowed")};
  }
  // 4. Route.
  const key = path.startsWith("/assets/") ? "/assets/*" : path;
  const routes = [...ctx.routes].filter(([pattern]) => pattern.split(" ")[1] === key);
  if (routes.length === 0) {
    return {reply: errorReply(404, "not_found", "not found")};
  }
  // 5. Method.
  const route = routes.find(([pattern]) => pattern.split(" ")[0] === req.method)?.[1];
  if (!route) {
    const allow = routes.map(([pattern]) => pattern.split(" ")[0]).join(", ");
    return {reply: errorReply(405, "method_not_allowed", "method not allowed", {Allow: allow})};
  }
  // 6. Origin — mandatory on POST, compared when present on GET.
  const sentOrigin = req.headers.origin;
  if ((req.method === "POST" || sentOrigin !== undefined) && sentOrigin !== origin) {
    return {reply: errorReply(403, "forbidden_origin", "origin not allowed")};
  }
  // 7. Session cookie.
  if (route.auth && !ctx.auth.accepts(cookieValues(req.headers.cookie, `augury_web_${ctx.port}`))) {
    return {reply: errorReply(401, "unauthorized", "not signed in — re-run agent-augury --web")};
  }
  if (req.method === "POST") {
    // 8. Media type.
    if (!isJson(req.headers["content-type"])) {
      return {reply: errorReply(415, "unsupported_media_type", "expected application/json")};
    }
    // 9 (first half). Declared size.
    const declared = Number(req.headers["content-length"] ?? 0);
    if (declared > (route.maxBody ?? MAX_BODY_BYTES)) {
      // Do not read a body we already refused; close the connection instead.
      return {reply: errorReply(413, "payload_too_large", "request body too large", {Connection: "close"})};
    }
  }
  return {route, path, query: url.searchParams};
}

/** 9 (second half) and 10: read at most *limit* bytes and parse a JSON object. */
async function readJsonObject(
  req: IncomingMessage,
  limit: number,
): Promise<{value: Record<string, unknown>} | {reply: Reply}> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > limit) {
      return {reply: errorReply(413, "payload_too_large", "request body too large", {Connection: "close"})};
    }
    chunks.push(chunk as Buffer);
  }
  try {
    const value: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return {value: value as Record<string, unknown>};
    }
  } catch {
    // fall through
  }
  return {reply: errorReply(400, "bad_request", "body must be a JSON object")};
}

/** Common headers (08 §2.3) on every reply; only hashed assets override no-store. */
function send(res: ServerResponse, reply: Reply): void {
  const headers: Record<string, string> = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    ...reply.headers,
  };
  if (reply.stream) {
    res.writeHead(reply.status, headers);
    reply.stream(res);
    return;
  }
  let body: Buffer | undefined;
  if (Buffer.isBuffer(reply.body)) {
    body = reply.body;
  } else if (reply.body !== undefined) {
    body = Buffer.from(JSON.stringify(reply.body));
    headers["Content-Type"] = "application/json; charset=utf-8";
  }
  if (body) {
    headers["Content-Length"] = String(body.length);
  }
  res.writeHead(reply.status, headers);
  res.end(body);
}

/** API-01: ``/`` → index.html, ``/assets/*`` → built files. No dot-files, maps or listings. */
export function staticRoutes(root: string): Array<[string, Route]> {
  const assets = resolve(root, "assets");
  // realpath, not just resolve: a symlink inside the build must not lead outside it.
  const serve = async (file: string, headers: Record<string, string>): Promise<Reply> => {
    try {
      const [real, base] = await Promise.all([realpath(file), realpath(root)]);
      if (real.startsWith(base + sep) && (await stat(real)).isFile()) {
        return {status: 200, headers, body: await readFile(file)};
      }
    } catch {
      // not found
    }
    return errorReply(404, "not_found", "not found");
  };
  return [
    [
      "GET /",
      {method: "GET", auth: false, handle: () => serve(join(root, "index.html"), {"Content-Type": "text/html; charset=utf-8"})},
    ],
    [
      "GET /assets/*",
      {
        method: "GET",
        auth: false,
        handle: ({path}) => {
          let rel: string;
          try {
            rel = decodeURIComponent(path.slice("/assets/".length));
          } catch {
            return errorReply(404, "not_found", "not found");
          }
          const file = resolve(assets, rel);
          const type = MIME[extname(file)];
          if (!file.startsWith(assets + sep) || !type || rel.split(/[\\/]/).some((p) => p.startsWith("."))) {
            return errorReply(404, "not_found", "not found");
          }
          // Only content-hashed names may be cached forever; others revalidate.
          const cache = HASHED_ASSET.test(basename(file)) ? "public, max-age=31536000, immutable" : "no-cache";
          return serve(file, {"Content-Type": type, "Cache-Control": cache});
        },
      },
    ],
  ];
}

/** API-02 route. Cookie name carries the port so two BFFs on one host do not collide. */
export function exchangeRoute(auth: AuthState, port: () => number): [string, Route] {
  return [
    "POST /api/auth/exchange",
    {
      method: "POST",
      auth: false,
      maxBody: MAX_EXCHANGE_BODY_BYTES,
      handle: ({body}) => {
        const token = body?.token;
        if (typeof token !== "string" || token.length === 0 || token.length > 512) {
          return errorReply(400, "bad_request", "token must be a non-empty string");
        }
        const cookie = auth.exchange(token);
        if (cookie === null) {
          return errorReply(401, "invalid_token", "this link is no longer valid");
        }
        return {
          status: 204,
          headers: {"Set-Cookie": `augury_web_${port()}=${cookie}; HttpOnly; SameSite=Strict; Path=/`},
        };
      },
    },
  ];
}

export type BffServerOptions = {
  token: string;
  /** Built SPA directory (``index.html`` + ``assets/``). */
  staticRoot: string;
  /** Extra API routes, keyed ``"METHOD /path"``. */
  routes?: Array<[string, Route]>;
  now?: () => number;
  onRevoke?: (reason: string) => void;
  onError?: (err: Error) => void;
};

export type BffServer = {server: Server; port: number; url: string; auth: AuthState; close: () => Promise<void>};

/** Bind 127.0.0.1 on an OS-chosen port — never configurable (N-001, 08 §2.1a). */
export async function startServer(options: BffServerOptions): Promise<BffServer> {
  const onError = options.onError ?? (() => {});
  const auth = new AuthState(
    options.token,
    options.now,
    options.onRevoke ?? ((reason) => process.stderr.write(`augury web: sign-in link revoked (${reason}) — re-run agent-augury --web\n`)),
  );
  let port = 0;
  // Revoke at the TTL even if nobody tries to exchange, so the CLI hint shows up.
  const sweep = setTimeout(() => auth.sweep(), TOKEN_TTL_MS + 1000).unref();
  const routes = new Map<string, Route>([
    ...staticRoutes(options.staticRoot),
    exchangeRoute(auth, () => port),
    ...(options.routes ?? []),
  ]);
  // Node would answer a missing Host with its own 400; step 1 owns that case (403).
  const server = createServer({requireHostHeader: false}, async (req, res) => {
    try {
      const checked = checkRequest(req, {port, routes, auth});
      if ("reply" in checked) {
        // Never drain a refused request's body: with one (declared or chunked) we
        // answer Connection: close and Node destroys the socket after the reply.
        const hasBody = Number(req.headers["content-length"] ?? 0) > 0 || req.headers["transfer-encoding"] !== undefined;
        send(res, hasBody ? {...checked.reply, headers: {...checked.reply.headers, Connection: "close"}} : checked.reply);
        return;
      }
      let body: Record<string, unknown> | null = null;
      if (checked.route.method === "POST") {
        const read = await readJsonObject(req, checked.route.maxBody ?? MAX_BODY_BYTES);
        if ("reply" in read) {
          send(res, read.reply);
          return;
        }
        body = read.value;
      }
      send(res, await checked.route.handle({path: checked.path, query: checked.query, headers: req.headers, body}));
    } catch (err) {
      if (req.destroyed) {
        return; // the client went away mid-body; nothing to answer
      }
      onError(err instanceof Error ? err : new Error(String(err)));
      if (!res.headersSent) {
        send(res, errorReply(500, "internal_error", "internal error"));
      } else {
        res.destroy();
      }
    }
  });
  // No WebSocket endpoints yet. The Vite dev proxy (UI phase) must reuse the
  // same Host/Origin checks here before upgrading (review S13).
  server.on("upgrade", (req: IncomingMessage, socket: Duplex) => {
    // Node drops the socket's default error listener on upgrade: without this a
    // client reset (EPIPE) is an uncaught exception that kills the BFF.
    socket.on("error", () => {});
    const hostOk = req.httpVersion !== "1.0" && req.headers.host === `127.0.0.1:${port}`;
    const originOk = req.headers.origin === `http://127.0.0.1:${port}`;
    socket.end(hostOk && originOk ? "HTTP/1.1 404 Not Found\r\n\r\n" : "HTTP/1.1 403 Forbidden\r\n\r\n");
  });
  server.on("clientError", (_err, socket) => {
    socket.on("error", () => {});
    if (socket.writable) {
      socket.end("HTTP/1.1 400 Bad Request\r\n\r\n");
    } else {
      socket.destroy();
    }
  });
  await new Promise<void>((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolveListen());
  });
  const address = server.address();
  if (address === null || typeof address === "string") {
    throw new Error("BFF server has no TCP address");
  }
  port = address.port;
  return {
    server,
    port,
    url: `http://127.0.0.1:${port}/#token=${options.token}`,
    auth,
    close: () =>
      new Promise((done) => {
        clearTimeout(sweep);
        server.close(() => done());
        // SSE streams never end on their own. This also cuts in-flight replies —
        // acceptable at shutdown.
        server.closeAllConnections();
      }),
  };
}
