import assert from "node:assert/strict";
import {test} from "node:test";
import {escAction, isPanelKey} from "./keys.js";

const plain = {ctrlKey: false, metaKey: false, altKey: false, repeat: false};

test("question shortcuts: never with Ctrl/Cmd/Alt or a held key (Cmd+S must not skip)", () => {
  assert.equal(isPanelKey(plain), true);
  assert.equal(isPanelKey({...plain, metaKey: true}), false);
  assert.equal(isPanelKey({...plain, ctrlKey: true}), false);
  assert.equal(isPanelKey({...plain, altKey: true}), false);
  assert.equal(isPanelKey({...plain, repeat: true}), false);
});

test("Esc: a confirm step is cancelled wherever focus is, before interrupting", () => {
  const base = {dialogOpen: false, confirming: true, canInterrupt: true, composing: false, inComposer: false};
  assert.equal(escAction(base), "cancel-confirm");
  assert.equal(escAction({...base, inComposer: true}), "cancel-confirm");
  assert.equal(escAction({...base, confirming: false}), "interrupt");
  assert.equal(escAction({...base, dialogOpen: true}), "close-dialog");
  assert.equal(escAction({...base, composing: true}), null);
});
