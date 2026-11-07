import assert from "node:assert/strict";
import {test} from "node:test";
import {FOLD_CHARS, foldedKeys, isTruncated} from "./approval.js";

const long = "x".repeat(FOLD_CHARS + 1);

test("foldedKeys: long values stay folded until opened", () => {
  assert.deepEqual(foldedKeys({a: "short", b: long, c: {v: long}}, new Set()), ["b", "c"]);
  assert.deepEqual(foldedKeys({a: "short", b: long}, new Set(["b"])), []);
  assert.deepEqual(foldedKeys({a: "x".repeat(FOLD_CHARS)}, new Set()), [], "exactly the limit is not folded");
});

test("isTruncated: the BFF clip marker or a clipped name means part can never be seen", () => {
  const a = (extra = {}) => ({agent_id: "bob", tool: "run_command", args_preview: {command: "ls"}, ...extra});
  assert.equal(isTruncated(a()), false);
  assert.equal(isTruncated(a({args_preview: {content: `${"y".repeat(4000)}… (+1000 chars)`}})), true);
  assert.equal(isTruncated(a({args_preview: {["k".repeat(65)]: "v"}})), true);
  assert.equal(isTruncated(a({tool: "t".repeat(65)})), true);
  assert.equal(isTruncated(a({agent_id: "a".repeat(65)})), true);
  assert.equal(isTruncated(a({args_preview: {content: long}})), false, "folded is not truncated");
});
