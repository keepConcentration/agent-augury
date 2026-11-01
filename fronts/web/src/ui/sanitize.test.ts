import assert from "node:assert/strict";
import {test} from "node:test";
import {formatLeft, ttlLeft} from "./approval.js";
import {escAction, isCardShortcut} from "./keys.js";
import {clip, valueText, visible} from "./sanitize.js";

test("visible: controls become ·, invisible marks are shown, ordinary text untouched (10 M2)", () => {
  assert.equal(visible("rm -rf build\nok"), "rm -rf build·ok");
  assert.equal(visible("\u001b[2J"), "·[2J");
  assert.equal(visible("a\u0085b"), "a·b", "C1 too, not just C0");
  assert.equal(visible("safe ‮etouq"), "safe ⟨U+202E⟩etouq");
  assert.equal(visible("x​y﻿z w"), "x⟨U+200B⟩y⟨U+FEFF⟩z⟨U+2028⟩w");
  assert.equal(visible("한글 path/파일.txt"), "한글 path/파일.txt");
});

test("clip and valueText", () => {
  assert.equal(clip("abcdef", 3), "abc… (+3 chars)");
  assert.equal(clip("abc", 3), "abc");
  assert.equal(valueText("s"), "s");
  assert.equal(valueText({a: 1}), '{"a":1}');
  assert.equal(valueText(undefined), "undefined");
});

test("ttlLeft: none without a TTL, rounds up, never negative (F-009)", () => {
  assert.equal(ttlLeft({ttl_seconds: null, received_at: 0}, 0), null);
  assert.equal(ttlLeft({ttl_seconds: 60, received_at: 1000}, 1000), 60);
  assert.equal(ttlLeft({ttl_seconds: 60, received_at: 1000}, 1000 + 59_001), 1);
  assert.equal(ttlLeft({ttl_seconds: 60, received_at: 1000}, 1000 + 99_000), 0);
  assert.equal(formatLeft(65), "01:05");
});

test("Esc: IME first, then dialog, then approval confirm, then interrupt — never from the composer", () => {
  const base = {dialogOpen: false, confirming: false, canInterrupt: true, composing: false, inComposer: false};
  assert.equal(escAction({...base, composing: true, dialogOpen: true}), null);
  assert.equal(escAction({...base, dialogOpen: true, confirming: true}), "close-dialog");
  assert.equal(escAction({...base, confirming: true}), "cancel-confirm");
  assert.equal(escAction(base), "interrupt");
  assert.equal(escAction({...base, inComposer: true}), null);
  assert.equal(escAction({...base, canInterrupt: false}), null);
});

test("Alt+Shift+A is matched by code (macOS key is 'Å')", () => {
  const e = {altKey: true, shiftKey: true, ctrlKey: false, metaKey: false, code: "KeyA"};
  assert.equal(isCardShortcut(e), true);
  assert.equal(isCardShortcut({...e, code: "KeyJ"}), false);
  assert.equal(isCardShortcut({...e, ctrlKey: true}), false);
});
