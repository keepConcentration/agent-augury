import assert from "node:assert/strict";
import {describe, it} from "node:test";
import {formatEvent, formatGate,
  previewApprovalArgs,
} from "./wire.js";

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

  it("formats bootstrap thread.created as opened", () => {
    const line = formatEvent({
      dir: "event",
      type: "thread.created",
      thread_id: "thread-1",
      name: "plan",
      participants: ["a1", "a2"],
      bootstrap: true,
    });
    assert.ok(line?.includes("opened"));
    assert.ok(!line?.includes("create_thread"));
  });

  it("skips session.started log (status bar owns roster)", () => {
    assert.equal(
      formatEvent({
        dir: "event",
        type: "session.started",
        agents: ["a1", "a2"],
      }),
      null,
    );
  });
});

describe("formatGate", () => {
  it("skips session.gate from the log (status bar owns it)", () => {
    assert.equal(
      formatEvent({dir: "event", type: "session.gate", phase: "P2_SPLIT"}),
      null,
    );
  });

  it("summarises votes and who is pending", () => {
    assert.equal(
      formatGate({
        dir: "event",
        type: "session.gate",
        phase: "P2_SPLIT",
        thread_name: "plan",
        approvals: ["a1", "a2"],
        pending: ["a3"],
        open: false,
        has_proposal: true,
        require_proposal: true,
      }),
      "P2_SPLIT · plan 2/3 · pending @a3",
    );
  });

  it("reports an open gate", () => {
    assert.equal(
      formatGate({
        dir: "event",
        type: "session.gate",
        phase: "P3_EXECUTE",
        thread_name: "execution",
        approvals: ["a1", "a2"],
        pending: [],
        open: true,
      }),
      "P3_EXECUTE · execution 2/2 · gate open",
    );
  });

  it("explains a missing proposal instead of blaming voters", () => {
    assert.equal(
      formatGate({
        dir: "event",
        type: "session.gate",
        phase: "P2_SPLIT",
        thread_name: "plan",
        approvals: ["a1", "a2"],
        pending: [],
        open: false,
        require_proposal: true,
        has_proposal: false,
      }),
      "P2_SPLIT · plan 2/2 · awaiting PROPOSE:",
    );
  });

  it("flags the human approval stage", () => {
    assert.equal(
      formatGate({
        dir: "event",
        type: "session.gate",
        phase: "P5_SUBMIT",
        thread_name: "submission",
        approvals: ["a1"],
        pending: [],
        open: false,
        human_pending: true,
      }),
      "P5_SUBMIT · submission 1/1 · awaiting human APPROVE:",
    );
  });

  it("ignores non-gate events", () => {
    assert.equal(formatGate({dir: "event", type: "session.phase"}), null);
  });
});

describe("previewApprovalArgs", () => {
  it("previewApprovalArgs lists every arg the digest binds", () => {
    const out = previewApprovalArgs({
      path: "/etc/passwd",
      content: "root::0:0::/:/bin/sh",
    });
    assert.match(out, /path: \/etc\/passwd/);
    assert.match(out, /content: root::0:0/);
  });

  it("previewApprovalArgs neutralises control chars", () => {
    // Loopjacking, painting half: ESC repaints the card, a newline forges a row.
    const out = previewApprovalArgs({
      command: "ls\u001b[2K\ncommand: rm -rf /",
    });
    assert.ok(!out.includes("\u001b"));
    assert.equal(out.split("\n").length, 1);
  });

  it("previewApprovalArgs clips long values with a marker", () => {
    const out = previewApprovalArgs({content: "A".repeat(500)});
    assert.match(out, /\(\+200 chars\)/);
  });
});
