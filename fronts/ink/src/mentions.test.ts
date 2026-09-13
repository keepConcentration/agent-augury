import assert from "node:assert/strict";
import {describe, it} from "node:test";
import {parseHumanMentions} from "./mentions.js";

describe("parseHumanMentions", () => {
  it("returns empty when no @tokens", () => {
    assert.deepEqual(parseHumanMentions("hello all"), {
      mentions: [],
      unknown: [],
    });
  });

  it("extracts multiple agent ids", () => {
    assert.deepEqual(
      parseHumanMentions("@agent-1 please sync with @agent-2"),
      {mentions: ["agent-1", "agent-2"], unknown: []},
    );
  });

  it("dedupes repeated mentions", () => {
    assert.deepEqual(parseHumanMentions("@a1 ping @a1"), {
      mentions: ["a1"],
      unknown: [],
    });
  });

  it("ignores email-like @ mid-token", () => {
    assert.deepEqual(parseHumanMentions("mail me at user@example.com"), {
      mentions: [],
      unknown: [],
    });
  });

  it("filters to known roster and reports unknown", () => {
    assert.deepEqual(
      parseHumanMentions("@agent-1 @ghost do it", ["agent-1", "agent-2"]),
      {mentions: ["agent-1"], unknown: ["ghost"]},
    );
  });

  it("accepts @ after brackets", () => {
    assert.deepEqual(parseHumanMentions("(@a1) go", ["a1"]), {
      mentions: ["a1"],
      unknown: [],
    });
  });
});
