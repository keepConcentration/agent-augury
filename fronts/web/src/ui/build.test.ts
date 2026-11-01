import assert from "node:assert/strict";
import {mkdtempSync, readFileSync, readdirSync} from "node:fs";
import {request} from "node:http";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {test} from "node:test";
import {build} from "vite";
import {CSP, startServer} from "../bff/http.js";
import {repoRoot} from "../bff/paths.js";

const webRoot = join(repoRoot, "fronts", "web");

function get(port: number, path: string, headers: Record<string, string> = {}) {
  return new Promise<{status: number; type: string; csp: string; body: string}>((resolve, reject) => {
    const req = request({host: "127.0.0.1", port, path, headers}, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (c) => (body += c));
      res.on("end", () =>
        resolve({status: res.statusCode ?? 0, type: String(res.headers["content-type"]), csp: String(res.headers["content-security-policy"]), body}),
      );
    });
    req.on("error", reject);
    req.end();
  });
}

test("the Vite build fits the BFF: no inline script, hashed assets served under the CSP (API-01)", async () => {
  const out = mkdtempSync(join(tmpdir(), "augury-web-dist-"));
  await build({root: webRoot, configFile: join(webRoot, "vite.config.ts"), logLevel: "silent", build: {outDir: out, emptyOutDir: true}});
  const index = readFileSync(join(out, "index.html"), "utf8");
  // CSP script-src 'self': every <script> must load from a file.
  assert.doesNotMatch(index, /<script(?![^>]*\bsrc=)[^>]*>/);
  assert.doesNotMatch(index, /\son[a-z]+=/i, "no inline event handlers");
  const assets = readdirSync(join(out, "assets"));
  assert.ok(assets.some((f) => f.endsWith(".js")) && assets.some((f) => f.endsWith(".css")));
  assert.ok(!assets.some((f) => f.endsWith(".map")), "no source maps shipped");

  const bff = await startServer({token: "b".repeat(43), staticRoot: out, onRevoke: () => {}});
  try {
    const page = await get(bff.port, "/", {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate"});
    assert.equal(page.status, 200);
    assert.equal(page.csp, CSP);
    for (const src of [...page.body.matchAll(/(?:src|href)="(\/assets\/[^"]+)"/g)].map((m) => m[1])) {
      const asset = await get(bff.port, src, {"Sec-Fetch-Site": "same-origin"});
      assert.equal(asset.status, 200, src);
      assert.match(asset.type, src.endsWith(".js") ? /javascript/ : /text\/css/);
    }
  } finally {
    await bff.close();
  }
});
