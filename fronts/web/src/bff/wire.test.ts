import assert from "node:assert/strict";
import {test} from "node:test";
import {
  COMMAND_TYPES,
  EVENT_TYPES,
  commandSchemaError,
  lineSplitter,
  maskSensitive,
  parseChildLine,
} from "./wire.js";

test("type lists come from the Wire schemas (22 events, 7 commands)", () => {
  assert.equal(EVENT_TYPES.size, 22);
  assert.equal(COMMAND_TYPES.size, 7);
  assert.ok(EVENT_TYPES.has("approval.request"));
  assert.ok(COMMAND_TYPES.has("approval.resolve"));
});

test("valid event is classified with no schema error", () => {
  const got = parseChildLine('{"dir":"event","type":"session.started","agents":["a"]}');
  assert.equal(got.kind, "event");
  assert.equal(got.kind === "event" && got.schemaError, null);
});

test("event with a wrongly typed field keeps the message but reports the error", () => {
  const got = parseChildLine('{"dir":"event","type":"session.started","agents":"a"}');
  assert.equal(got.kind, "event");
  assert.ok(got.kind === "event" && got.schemaError);
  assert.equal(got.kind === "event" && got.message.type, "session.started");
});

test("unknown event type is a schema error, not dropped", () => {
  const got = parseChildLine('{"dir":"event","type":"made.up"}');
  assert.equal(got.kind, "event");
  assert.match(got.kind === "event" ? got.schemaError ?? "" : "", /unknown event type/);
});

test("schema errors name the real problem and stay short", () => {
  const got = parseChildLine('{"dir":"event","type":"session.started","agents":"a"}');
  const error = got.kind === "event" ? got.schemaError ?? "" : "";
  assert.match(error, /agents must be array/);
  assert.doesNotMatch(error, /equal to constant/);
  const long = parseChildLine(JSON.stringify({dir: "event", type: "x".repeat(5000)}));
  assert.ok(long.kind === "event" && (long.schemaError ?? "").length < 120);
});

test("result lines are passed through for correlation", () => {
  const got = parseChildLine('{"dir":"result","id":"web-1","ok":true,"queued":true}');
  assert.equal(got.kind, "result");
});

test("decode errors never quote the line", () => {
  const got = parseChildLine("Authorization: Bearer sk-live-123 not json");
  assert.equal(got.kind, "decode_error");
  assert.ok(got.kind === "decode_error" && !got.error.includes("sk-live-123"));
});

test("a cmd coming back from the child is rejected", () => {
  assert.equal(parseChildLine('{"dir":"cmd","type":"session.quit","id":"x"}').kind, "decode_error");
});

test("BFF masking adds cookie and password/secret rules on top of Ink's", () => {
  const out = maskSensitive("Cookie: augury_web_1=abc; x=y | password=hunter2 | secret: s3 | Bearer tok123");
  assert.doesNotMatch(out, /abc|hunter2|s3 |tok123/);
  assert.match(out, /Cookie: \*\*\*/);
});

test("command schema: answer needs content, unknown types are named", () => {
  assert.equal(
    commandSchemaError({dir: "cmd", type: "human.answer", id: "web-1", content: "ok", question_id: "q1", mentions: ["a"]}),
    null,
  );
  assert.ok(commandSchemaError({dir: "cmd", type: "human.answer", id: "web-1", question_id: "q1"}));
  assert.match(commandSchemaError({dir: "cmd", type: "nope", id: "x"}) ?? "", /unknown command type/);
});

test("lineSplitter joins chunks, keeps split multibyte chars intact, skips blanks", () => {
  const lines: string[] = [];
  const feed = lineSplitter((l) => lines.push(l), () => assert.fail("no overflow")).write;
  const bytes = Buffer.from('{"a":"한글"}\r\n\n{"b":1}\n', "utf8");
  const cut = bytes.indexOf(Buffer.from("글")) + 1; // inside the 3-byte char
  feed(bytes.subarray(0, cut));
  feed(bytes.subarray(cut));
  assert.deepEqual(lines, ['{"a":"한글"}', '{"b":1}']);
});

test("lineSplitter drops an oversized line once and keeps going", () => {
  const lines: string[] = [];
  const overflows: number[] = [];
  const feed = lineSplitter((l) => lines.push(l), (n) => overflows.push(n), 8).write;
  feed(Buffer.from("0123"));
  feed(Buffer.from("456789abc\nok\n"));
  assert.deepEqual(lines, ["ok"]);
  assert.deepEqual(overflows, [13]);
});

test("lineSplitter end() flushes a last line without newline, or reports its overflow", () => {
  const lines: string[] = [];
  const overflows: number[] = [];
  const s = lineSplitter((l) => lines.push(l), (n) => overflows.push(n), 8);
  s.write(Buffer.from("a\ntail"));
  s.end();
  s.write(Buffer.from("0123456789"));
  s.end();
  s.end();
  assert.deepEqual(lines, ["a", "tail"]);
  assert.deepEqual(overflows, [10]);
});
