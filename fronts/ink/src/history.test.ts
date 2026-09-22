import assert from "node:assert/strict";
import {describe, it} from "node:test";
import {emptyHistory, recall, remember, type History} from "./history.js";

function build(...lines: string[]): History {
  return lines.reduce(remember, emptyHistory);
}

describe("history", () => {
  it("has nowhere to go when empty", () => {
    assert.equal(recall(emptyHistory, -1, "draft"), null);
    assert.equal(recall(emptyHistory, 1, "draft"), null);
  });

  it("walks back from newest to oldest and stops", () => {
    let h = build("one", "two");
    let step = recall(h, -1, "")!;
    assert.equal(step.value, "two");
    h = step.history;
    step = recall(h, -1, "")!;
    assert.equal(step.value, "one");
    assert.equal(recall(step.history, -1, ""), null);
  });

  it("returns the half-typed draft when walking forward off the end", () => {
    const h = build("one");
    const back = recall(h, -1, "half typed")!;
    assert.equal(back.value, "one");
    const forward = recall(back.history, 1, "one")!;
    assert.equal(forward.value, "half typed");
    // And we are back on the live line, so forward again is a no-op.
    assert.equal(recall(forward.history, 1, "half typed"), null);
  });

  it("ignores blanks and immediate repeats", () => {
    assert.deepEqual(build("one", "one", "  ", "two").entries, ["one", "two"]);
  });

  it("submitting while browsing drops back to the live line", () => {
    const back = recall(build("one", "two"), -1, "")!;
    assert.equal(back.history.index, 1);
    const after = remember(back.history, "three");
    assert.equal(after.index, after.entries.length);
    assert.equal(after.draft, "");
  });
});
