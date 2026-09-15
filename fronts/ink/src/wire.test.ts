import assert from "node:assert/strict";
import {describe, it} from "node:test";
import {formatEvent} from "./wire.js";

describe("formatEvent approval", () => {
  it("skips approval.request (panel owns UI)", () => {
    assert.equal(
      formatEvent({
        dir: "event",
        type: "approval.request",
        approval_id: "a1",
        tool: "run_command",
      }),
      null,
    );
  });

  it("formats approval.resolved", () => {
    const line = formatEvent({
      dir: "event",
      type: "approval.resolved",
      approval_id: "a1",
      decision: "granted",
      agent_id: "coder",
      tool: "run_command",
    });
    assert.ok(line?.includes("granted"));
    assert.ok(line?.includes("a1"));
    assert.ok(line?.includes("run_command"));
  });

  it("formats tool.denied", () => {
    const line = formatEvent({
      dir: "event",
      type: "tool.denied",
      tool: "write_file",
      reason: "no_approval_channel",
    });
    assert.ok(line?.includes("write_file"));
    assert.ok(line?.includes("no_approval_channel"));
  });

  it("skips session.checkpoint noise", () => {
    assert.equal(
      formatEvent({
        dir: "event",
        type: "session.checkpoint",
        session_id: "abc",
      }),
      null,
    );
  });
});
