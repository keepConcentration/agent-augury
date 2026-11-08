/** BFF integration with a real Gateway child (WBS 5.2).
 *
 * Full stack: startBff → ``session_stdio`` (real Core session) → stub model
 * (test/stub-model.ts) → tool approval required (test/fixtures/approval.yaml,
 * no bypass). Browser tabs are plain HTTP/SSE clients here; the UI's own
 * rendering is covered by the ui/*.test.tsx files.
 */

import assert from "node:assert/strict";
import {spawnSync} from "node:child_process";
import {existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from "node:fs";
import {request as httpRequest, type IncomingMessage} from "node:http";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {test} from "node:test";
import {DISGUISED_CONTENT, DISGUISED_PATH, startStubModel, type StubModel} from "../../test/stub-model.js";
import {startBff, type Bff} from "./bff.js";
import {resolvePython} from "./gateway.js";
import {repoRoot} from "./paths.js";

const TOKEN = "i".repeat(43);
const A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const python = resolvePython(process.env);
// The resolver may fall back to a bare ``python3``: ask the interpreter itself
// whether it can run the Gateway rather than looking for a file.
const usable = spawnSync(python, ["-c", "import agent_augury"], {stdio: "ignore"}).status === 0;
// Locally an unusable Python skips; in CI (env CI) it fails, so a green run means these ran (5.6).
if (process.env.CI && !usable) {
  throw new Error(`integration tests need a Python with agent_augury in CI (tried ${python}; set AUGURY_PYTHON)`);
}
const skip = !usable && `no Python with agent_augury (${python})`;
const skipSignals = skip || (process.platform === "win32" && "SIGSTOP is POSIX-only");
const FIXTURE = join(repoRoot, "fronts", "web", "test", "fixtures", "approval.yaml");

type Frame = {event?: string; id?: string; data?: string};

function http(port: number, method: string, path: string, opts: {cookie?: string; tab?: string; body?: unknown} = {}) {
  const origin = `http://127.0.0.1:${port}`;
  return new Promise<{status: number; body: any; cookie: string | null}>((resolve, reject) => {
    const req = httpRequest(
      {
        host: "127.0.0.1",
        port,
        method,
        path,
        headers: {
          ...(opts.body !== undefined ? {"Content-Type": "application/json", Origin: origin} : {}),
          ...(opts.cookie ? {Cookie: opts.cookie} : {}),
          ...(opts.tab ? {"X-Augury-Tab": opts.tab} : {}),
        },
      },
      (res) => {
        let data = "";
        res.setEncoding("utf8");
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          const set = res.headers["set-cookie"];
          let body: any = null;
          try {
            body = data ? JSON.parse(data) : null;
          } catch {
            return reject(new Error(`${method} ${path}: ${res.statusCode} with a non-JSON body: ${data.slice(0, 200)}`));
          }
          resolve({status: res.statusCode ?? 0, body, cookie: set ? String(set).split(";")[0] : null});
        });
      },
    );
    req.on("error", reject);
    req.end(opts.body === undefined ? undefined : JSON.stringify(opts.body));
  });
}

class Tab {
  frames: Frame[] = [];
  res!: IncomingMessage;
  private buf = "";
  constructor(
    readonly id: string,
    private readonly port: number,
    private readonly cookie: string,
  ) {}
  open(): Promise<this> {
    return new Promise((resolve, reject) => {
      const req = httpRequest({host: "127.0.0.1", port: this.port, path: `/api/events?tab=${this.id}`, headers: {Cookie: this.cookie}}, (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          return reject(new Error(`SSE for tab ${this.id}: HTTP ${res.statusCode}`));
        }
        this.res = res;
        res.setEncoding("utf8");
        res.on("data", (chunk: string) => {
          this.buf += chunk;
          let i: number;
          while ((i = this.buf.indexOf("\n\n")) !== -1) {
            const f: Record<string, string> = {};
            for (const line of this.buf.slice(0, i).split("\n")) {
              const [k, ...v] = line.split(": ");
              f[k] = v.join(": ");
            }
            this.buf = this.buf.slice(i + 2);
            this.frames.push(f);
          }
        });
        res.on("error", () => {});
        resolve(this);
      });
      req.on("error", reject);
      req.end();
    });
  }
  state(): Record<string, any> {
    return JSON.parse(this.frames.filter((f) => f.event === "bff.state").at(-1)!.data!);
  }
  hasState(): boolean {
    return this.frames.some((f) => f.event === "bff.state");
  }
  wire(): Array<{seq: number; e: Record<string, any>}> {
    return this.frames.filter((f) => f.event === "wire").map((f) => ({seq: Number(f.id), e: JSON.parse(f.data!)}));
  }
  notices(): Array<Record<string, any>> {
    return this.frames.filter((f) => f.event === "bff.notice").map((f) => JSON.parse(f.data!));
  }
  cmd(body: Record<string, unknown>) {
    return http(this.port, "POST", "/api/cmd", {cookie: this.cookie, tab: this.id, body});
  }
}

async function until(cond: () => boolean, what: string, ms = 20_000): Promise<void> {
  const end = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > end) {
      throw new Error(`timed out: ${what}`);
    }
    await new Promise((r) => setTimeout(r, 20));
  }
}

function alive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return (err as NodeJS.ErrnoException).code !== "ESRCH"; // EPERM: exists, not ours
  }
}

type Rig = {bff: Bff; stub: StubModel; a: Tab; b: Tab; root: string; secrets: string; gatewayPid: number; done: () => Promise<void>};

/** Resolve body for a card: the revision is the one the tab was shown (08 §4.4 M3). */
function resolve(card: {approval_id: string; revision: number}, decision: "granted" | "denied") {
  return {type: "approval.resolve", approval_id: card.approval_id, revision: card.revision, decision};
}

/** BFF + real session child on the approval fixture; tab A controls, tab B watches. */
async function rig(cmdTimeoutMs?: number): Promise<Rig> {
  // Undo in reverse order of creation, each step on its own: a failed setup
  // must not leave a Gateway child, the stub or temp dirs behind (N-007).
  const undo: Array<() => unknown> = [];
  const done = async () => {
    for (const step of undo.reverse()) {
      try {
        await step();
      } catch {
        // keep cleaning
      }
    }
    undo.length = 0;
  };
  try {
    const stub = await startStubModel();
    undo.push(() => stub.close());
    const temp = (prefix: string) => {
      const dir = mkdtempSync(join(tmpdir(), prefix));
      undo.push(() => rmSync(dir, {recursive: true, force: true}));
      return dir;
    };
    const root = temp("augury-it-root-");
    const home = temp("augury-it-home-");
    const secrets = join(temp("augury-it-secrets-"), "s.json");
    // The model key reaches the child only through the secrets file, as with the CLI.
    writeFileSync(secrets, JSON.stringify({AUGURY_STUB_MODEL_API_KEY: "stub-key"}), {mode: 0o600});
    const bff = await startBff({
      token: TOKEN,
      devMode: false,
      python,
      cmdTimeoutMs,
      log: () => {},
      env: {
        PATH: process.env.PATH,
        HOME: home,
        AUGURY_GATEWAY_MODE: "session",
        AUGURY_CONFIG: FIXTURE,
        AUGURY_FILE_ROOT: root,
        AUGURY_STUB_MODEL_URL: stub.url,
        AUGURY_GATEWAY_SECRETS_FILE: secrets,
        AGENT_AUGURY_NEW_SESSION: "1",
      },
    });
    undo.push(() => bff.shutdown());
    // Checked here: process.kill(0, …) would signal this runner's own process group.
    const gatewayPid = bff.gateway.pid;
    if (!gatewayPid) throw new Error("Gateway child has no pid");
    const ex = await http(bff.port, "POST", "/api/auth/exchange", {body: {token: TOKEN}});
    assert.equal(ex.status, 204);
    const a = await new Tab(A, bff.port, ex.cookie!).open();
    undo.push(() => a.res.destroy());
    await until(() => a.hasState(), "tab A state");
    const b = await new Tab(B, bff.port, ex.cookie!).open();
    undo.push(() => b.res.destroy());
    await until(() => b.hasState(), "tab B state");
    return {bff, stub, a, b, root, secrets, gatewayPid, done};
  } catch (err) {
    await done();
    throw err;
  }
}

test("approval end to end: real child asks, read-only tab is refused, controller grants one and denies one", {skip}, async () => {
  const r = await rig();
  try {
    const {a, b} = r;
    assert.equal(a.state().is_controller, true);
    assert.equal(b.state().is_controller, false);
    await until(() => b.state().pending_approvals.length === 2, "both approval requests");

    // The child consumed the secrets file; the key never travelled in env.
    assert.equal(existsSync(r.secrets), false, "secrets file read and removed by the child");

    const [first, second] = b.state().pending_approvals;
    assert.deepEqual([first.agent_id, first.tool, first.args_preview.path, first.revision], ["bob", "write_file", "granted.txt", 1]);
    // Disguise attempts reach the browser untouched — the card makes them visible (10 M2).
    assert.equal(second.args_preview.path, DISGUISED_PATH);
    assert.equal(second.args_preview.content, DISGUISED_CONTENT);
    assert.ok(DISGUISED_PATH.includes("\u200b") && DISGUISED_CONTENT.includes("\u202e"), "fixture still disguised");
    assert.equal(second.args_preview["cwd\ncommand"], "/tmp");

    // DW-7: only the controller tab may decide.
    const refused = await b.cmd(resolve(first, "granted"));
    assert.deepEqual([refused.status, refused.body.error.code], [409, "not_controller"]);
    assert.equal(existsSync(join(r.root, "granted.txt")), false, "a refused click writes nothing");

    const granted = await a.cmd(resolve(first, "granted"));
    assert.deepEqual([granted.status, granted.body.failed], [200, false]);
    await until(() => existsSync(join(r.root, "granted.txt")), "granted file");
    assert.equal(readFileSync(join(r.root, "granted.txt"), "utf8"), "approved by a human\n");

    // The card goes when approval.resolved arrives; wait for it rather than
    // relying on the child writing the result after the event on one pipe.
    await until(() => !a.state().pending_approvals.some((p: any) => p.approval_id === first.approval_id), "first card gone");
    const again = await a.cmd(resolve(first, "granted"));
    assert.deepEqual([again.status, again.body.error.code], [409, "not_pending"]);

    const denied = await a.cmd(resolve(second, "denied"));
    assert.deepEqual([denied.status, denied.body.failed], [200, false]);
    await until(() => a.state().pending_approvals.length === 0 && b.state().pending_approvals.length === 0, "cards gone in both tabs");
    assert.equal(existsSync(join(r.root, DISGUISED_PATH)), false, "denied write never happens");

    const resolved = a.wire().filter((w) => w.e.type === "approval.resolved").map((w) => [w.e.approval_id, w.e.decision]);
    assert.deepEqual(resolved, [
      [first.approval_id, "granted"],
      [second.approval_id, "denied"],
    ]);

    // One seq space, strictly increasing, the same frames for every tab (N-006).
    const seqA = a.wire().map((w) => w.seq);
    assert.ok(seqA.every((s, i) => i === 0 || s > seqA[i - 1]), "seq strictly increasing");
    const seqB = b.wire().map((w) => w.seq);
    assert.deepEqual(seqB, seqA.slice(seqA.length - seqB.length), "tab B saw the same frames");
    assert.ok(r.stub.calls >= 3, "the agent really ran through the model");
  } finally {
    await r.done();
  }
  assert.ok(r.gatewayPid > 0 && !alive(r.gatewayPid), "Gateway child gone after shutdown (N-007)");
});

test("504 then late_result: a slow result is not lost, the approval still lands", {skip: skipSignals}, async () => {
  const r = await rig(300);
  try {
    const {a} = r;
    await until(() => a.state().pending_approvals.length === 2, "approval requests");
    // By path, not position: the order of the two requests is not the point here.
    const first = a.state().pending_approvals.find((p: any) => p.args_preview.path === "granted.txt");
    assert.ok(first, "the granted.txt request is pending");
    // A stopped child cannot answer in time: the BFF must say "result unknown" (08 §6.2).
    process.kill(r.gatewayPid, "SIGSTOP");
    let slow;
    try {
      slow = await a.cmd(resolve(first, "granted"));
    } finally {
      process.kill(r.gatewayPid, "SIGCONT");
    }
    assert.deepEqual([slow.status, slow.body.error.code], [504, "timeout"]);
    const cmdId = slow.body.error.cmd_id;
    assert.match(cmdId, /^web-\d+$/);

    await until(() => a.notices().some((n) => n.kind === "late_result" && n.cmd_id === cmdId), "late_result notice");
    const late = a.notices().find((n) => n.kind === "late_result" && n.cmd_id === cmdId)!;
    assert.deepEqual([late.failed, late.level], [false, "info"]);
    await until(() => existsSync(join(r.root, "granted.txt")), "granted file after late result");
    await until(() => a.state().pending_approvals.length === 1, "first card cleared");
    const seq = a.wire().map((w) => w.seq);
    assert.ok(seq.every((s, i) => i === 0 || s > seq[i - 1]), "seq strictly increasing across the stall");
  } finally {
    await r.done();
  }
  assert.ok(r.gatewayPid > 0 && !alive(r.gatewayPid), "a once-stopped Gateway child is gone after shutdown");
});
