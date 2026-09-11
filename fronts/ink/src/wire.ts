/** Augury Wire JSONL helpers (M2/M3). */

export type WireDir = "event" | "cmd" | "result";

export type WireMessage = {
  dir: WireDir;
  type?: string;
  id?: string;
  ok?: boolean;
  error?: string;
  text?: string;
  content?: string;
  question?: string;
  question_id?: string;
  options?: string[];
  agent_id?: string;
  thread_id?: string;
  [key: string]: unknown;
};

let nextId = 1;

export function encodeLine(message: WireMessage): string {
  return JSON.stringify(message);
}

export function decodeLine(line: string): WireMessage {
  const raw = line.trim();
  if (!raw) {
    throw new Error("empty wire line");
  }
  const data = JSON.parse(raw) as WireMessage;
  if (!data || typeof data !== "object" || !data.dir) {
    throw new Error("wire line must be a JSON object with dir");
  }
  return data;
}

export function makeCommand(
  type: string,
  fields: Record<string, unknown> = {},
): WireMessage {
  const id = String(nextId++);
  return {dir: "cmd", type, id, ...fields};
}

export function formatEvent(event: WireMessage): string {
  const t = event.type ?? "?";
  if (t === "log" && typeof event.text === "string") {
    return event.text;
  }
  if (t === "human.question") {
    const opts = Array.isArray(event.options) ? event.options : [];
    const lines = [
      `? [${event.agent_id ?? "?"}] ${event.question ?? ""}`,
      ...opts.map((o, i) => `   [${i + 1}] ${o}`),
    ];
    return lines.join("\n");
  }
  if (t === "session.started") {
    return `[session] started${event.note ? ` - ${String(event.note)}` : ""}`;
  }
  if (t === "session.ended") {
    return `[session] ended (${String(event.reason ?? "done")})`;
  }
  if (t === "error") {
    return `[error] ${String(event.message ?? event.text ?? "")}`;
  }
  return `[${t}] ${encodeLine(event)}`;
}
