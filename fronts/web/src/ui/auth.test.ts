import assert from "node:assert/strict";
import {test} from "node:test";
import {authLink} from "./auth.js";

test("auth link only for the allowlisted OAuth host, judged by the real hostname", () => {
  assert.deepEqual(authLink("Verification URL: https://portal.nousresearch.com/device x"), {
    before: "Verification URL: ",
    url: "https://portal.nousresearch.com/device",
    host: "portal.nousresearch.com",
    after: " x",
  });
  for (const text of [
    "Verification URL: https://portal.nousresearch.com@evil.example/device", // userinfo trick
    "Verification URL: https://portal.nousresearch.com.evil.example/device",
    "Verification URL: https://evil.example/portal.nousresearch.com",
    "Verification URL: http://portal.nousresearch.com/device", // not https
    "Verification URL: javascript:alert(1)",
    "To authenticate, enter code: ABCD-EFGH",
  ]) {
    assert.equal(authLink(text), null, text);
  }
});
