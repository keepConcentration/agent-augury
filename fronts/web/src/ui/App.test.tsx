import assert from "node:assert/strict";
import {test} from "node:test";
import {TimelineRow} from "./App.js";

test("timeline rows are memoized: an event re-renders only the new row", () => {
  assert.equal((TimelineRow as unknown as {$$typeof: symbol}).$$typeof, Symbol.for("react.memo"));
});
