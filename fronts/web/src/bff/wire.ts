/** Wire JSONL framing, decoding and schema checks for the BFF (WBS 2.3).
 *
 * Decoding reuses the Ink helpers so both Surfaces read the Wire the same way
 * (AS-5). The schemas under ``schemas/wire`` are the single source of truth for
 * event and command names (N-005); nothing here keeps its own list.
 */

import {readFileSync} from "node:fs";
import {join} from "node:path";
import {Ajv2020} from "ajv/dist/2020.js";
import {decodeLine, encodeLine, type WireMessage} from "../../../ink/src/wire.js";
import {repoRoot} from "./paths.js";

export {encodeLine, type WireMessage};

/** Longest stdout line we accept from the child (08 §4.1, [가정]). */
export const MAX_LINE_BYTES = 1024 * 1024;

type Branch = {properties: {type: {const: string}}};
type Schema = {oneOf: Branch[]};

function loadSchema(name: string): Schema {
  const path = join(repoRoot, "schemas", "wire", `${name}.schema.json`);
  return JSON.parse(readFileSync(path, "utf8")) as Schema;
}

const ajv = new Ajv2020();

// One validator per type: the schema's oneOf would report every branch's
// "type must be equal to constant" and bury the one error that matters.
function validators(schema: Schema) {
  return new Map(schema.oneOf.map((branch) => [branch.properties.type.const, ajv.compile(branch)]));
}

const eventValidators = validators(loadSchema("events"));
const commandValidators = validators(loadSchema("commands"));

export const EVENT_TYPES: ReadonlySet<string> = new Set(eventValidators.keys());
export const COMMAND_TYPES: ReadonlySet<string> = new Set(commandValidators.keys());

/** Child-supplied text in an error goes to the browser: keep it short. */
function clip(value: unknown, max = 64): string {
  const text = String(value);
  return text.length > max ? `${text.slice(0, max)}… (+${text.length - max} chars)` : text;
}

function schemaError(
  kind: string,
  table: Map<string, ReturnType<typeof ajv.compile>>,
  message: WireMessage,
): string | null {
  const check = typeof message.type === "string" ? table.get(message.type) : undefined;
  if (!check) {
    return `unknown ${kind} type: ${clip(message.type)}`;
  }
  return check(message) ? null : clip(ajv.errorsText(check.errors), 500);
}

/** Why *message* is not a valid event, or null when it is. */
export function eventSchemaError(message: WireMessage): string | null {
  return schemaError("event", eventValidators, message);
}

/** Why *message* is not a valid command, or null when it is. */
export function commandSchemaError(message: WireMessage): string | null {
  return schemaError("command", commandValidators, message);
}

/** One stdout line from the Gateway child, classified. */
export type ChildLine =
  | {kind: "event"; message: WireMessage; schemaError: string | null}
  | {kind: "result"; message: WireMessage}
  | {kind: "decode_error"; error: string};

/** Classify one child stdout line.
 *
 * Error texts never quote the line: it may carry a secret the masking rules
 * do not know about, and decode errors end up in front of the browser.
 */
export function parseChildLine(line: string): ChildLine {
  let message: WireMessage;
  try {
    message = decodeLine(line);
  } catch {
    return {kind: "decode_error", error: `not a Wire JSON object (${line.length} chars)`};
  }
  if (message.dir === "result") {
    return {kind: "result", message};
  }
  if (message.dir === "event") {
    return {kind: "event", message, schemaError: eventSchemaError(message)};
  }
  return {kind: "decode_error", error: `unexpected dir from gateway: ${clip(message.dir)}`};
}

export type LineSplitter = {
  write: (chunk: Buffer) => void;
  /** Flush a last line that had no trailing newline. */
  end: () => void;
};

/** Split a byte stream into lines without ever buffering one longer than *maxBytes*.
 *
 * ``readline`` keeps a whole line in memory before handing it over, so a
 * runaway child could grow the BFF without bound. An oversized line is dropped
 * up to its newline and reported once through *onOverflow*.
 */
export function lineSplitter(
  onLine: (line: string) => void,
  onOverflow: (bytes: number) => void,
  maxBytes = MAX_LINE_BYTES,
): LineSplitter {
  let parts: Buffer[] = [];
  let size = 0;
  let dropping = false;
  const emit = () => {
    if (dropping) {
      onOverflow(size);
    } else {
      const line = Buffer.concat(parts).toString("utf8").replace(/\r$/, "");
      if (line.trim()) {
        onLine(line);
      }
    }
    parts = [];
    size = 0;
    dropping = false;
  };
  const write = (chunk: Buffer) => {
    let start = 0;
    while (start < chunk.length) {
      const nl = chunk.indexOf(0x0a, start);
      const end = nl === -1 ? chunk.length : nl;
      const piece = chunk.subarray(start, end);
      size += piece.length;
      if (!dropping && size > maxBytes) {
        dropping = true;
        parts = [];
      }
      if (!dropping) {
        parts.push(piece);
      }
      if (nl === -1) {
        return;
      }
      emit();
      start = nl + 1;
    }
  };
  const end = () => {
    if (size > 0) {
      emit();
    }
  };
  return {write, end};
}
