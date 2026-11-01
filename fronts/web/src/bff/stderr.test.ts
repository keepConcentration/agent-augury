import assert from "node:assert/strict";
import {test} from "node:test";
import {classifyStderr, handleStderrLine} from "./stderr.js";
import type {Notice} from "./sse.js";

test("OAuth device-code lines become auth notices; known noise is log-only; the rest warns", () => {
  assert.deepEqual(classifyStderr("To authenticate, enter code: ABCD-EFGH"), {kind: "auth", level: "info", text: "To authenticate, enter code: ABCD-EFGH"});
  assert.deepEqual(classifyStderr("  Verification URL: https://portal.nousresearch.com/device"), {
    kind: "auth",
    level: "info",
    text: "Verification URL: https://portal.nousresearch.com/device",
  });
  assert.equal(classifyStderr("WARNING tools.shell.enabled=true with empty 'allowed' list — ALL commands ..."), null);
  assert.deepEqual(classifyStderr("Traceback (most recent call last):"), {kind: "stderr", level: "warn", text: "Traceback (most recent call last):"});
  // Only a line that starts with the prompt is an auth notice — not one that merely quotes it.
  assert.equal(classifyStderr("agent said: To authenticate, enter code: X")?.kind, "stderr");
});

test("every line is logged masked; blank lines are ignored", () => {
  const logs: string[] = [];
  const notices: Notice[] = [];
  const sinks = {notice: (n: Notice) => notices.push(n), log: (l: string) => logs.push(l)};
  handleStderrLine("Authorization: Bearer sk-live-9 failed", sinks);
  handleStderrLine("tools.shell.enabled=true with empty 'allowed'", sinks);
  handleStderrLine("   ", sinks);
  assert.equal(logs.length, 2);
  assert.doesNotMatch(logs[0], /sk-live-9/);
  assert.match(logs[0], /^\[gateway\] /);
  assert.deepEqual(notices.map((n) => n.kind), ["stderr"]);
});
