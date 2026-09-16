/** Augury Wire JSONL helpers + display formatting (pt TUI parity). */

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
  agents?: string[];
  author?: string;
  thread_id?: string;
  name?: string;
  participants?: string[];
  bootstrap?: boolean;
  reused?: boolean;
  delivered_to?: string[];
  tool?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  threads?: number;
  messages?: number;
  protocol_violation?: boolean;
  violation_message?: string;
  phase?: string;
  message?: string;
  note?: string;
  reason?: string;
  [key: string]: unknown;
};

let nextId = 1;

const TOOL_ICONS: Record<string, string> = {
  read_file: "📖",
  write_file: "📝",
  list_directory: "📁",
  send_message: "💬",
  create_thread: "🧵",
  read_resource: "📊",
  run_command: "⚙️",
  fetch_url: "🌐",
  web_search: "🔎",
  edit_file: "✏️",
  append_file: "➕",
};

const SENSITIVE: Array<[RegExp, string]> = [
  [/(Authorization:\s+Bearer\s+)\S+/gi, "$1***"],
  [/(Bearer\s+)\S+/gi, "$1***"],
  [/(api[_-]?key["\s:=]+)\S+/gi, "$1***"],
  [/(token["\s:=]+)\S+/gi, "$1***"],
];

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

export function maskSensitive(text: string): string {
  let out = text;
  for (const [pattern, replacement] of SENSITIVE) {
    out = out.replace(pattern, replacement);
  }
  return out;
}

function basename(path: string): string {
  const norm = path.replace(/\\/g, "/");
  const parts = norm.split("/");
  return parts[parts.length - 1] || path;
}

function short(text: string, max = 60): string {
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

/**
 * One-line gate summary for the status bar: `P2_SPLIT · plan 2/3 · pending @a3`.
 * Returns null when the event carries no gate to show.
 */
export function formatGate(event: WireMessage): string | null {
  if (event.type !== "session.gate") return null;
  const phase = String(event.phase ?? "?");
  const name = String(event.thread_name ?? "");
  const approvals = Array.isArray(event.approvals) ? event.approvals.map(String) : [];
  const pending = Array.isArray(event.pending) ? event.pending.map(String) : [];
  const total = approvals.length + pending.length;
  const parts = [phase];
  if (name) parts.push(`${name} ${approvals.length}/${total}`);
  if (event.open) {
    parts.push("gate open");
  } else if (event.human_pending) {
    parts.push("awaiting human APPROVE:");
  } else if (event.require_proposal && !event.has_proposal) {
    parts.push("awaiting PROPOSE:");
  } else if (pending.length > 0) {
    parts.push(`pending ${pending.map((a) => `@${a}`).join(" ")}`);
  }
  return parts.join(" · ");
}

/**
 * Format a Wire event for the Ink log — mirrors ``tui/renderer.py`` layout.
 * Returns null when the event should be skipped (duplicate tool noise, etc.).
 */
export function formatEvent(event: WireMessage): string | null {
  const t = event.type ?? "?";

  if (t === "log" && typeof event.text === "string") {
    return event.text;
  }
  if (t === "session.started") {
    // Roster is applied in App state; skip noisy log line.
    return null;
  }
  if (t === "session.ended") {
    return `[session] ended (${String(event.reason ?? "done")})`;
  }
  // Debounced checkpoint noise — too noisy for the live log.
  if (t === "session.checkpoint") {
    return null;
  }
  if (t === "error") {
    return `[error] ${String(event.message ?? event.text ?? "")}`;
  }

  // Gate votes live in the status bar (single source of truth), not the log.
  if (t === "session.gate") {
    return null;
  }
  // human.question / approval.request shown in panels; skip duplicate log lines.
  if (t === "human.question") {
    return null;
  }
  if (t === "approval.request") {
    return null;
  }
  if (t === "approval.resolved" || t === "approval.granted" || t === "approval.expired") {
    const decision = String(event.decision ?? t.split(".")[1] ?? "");
    const aid = String(event.approval_id ?? "");
    const tool = String(event.tool ?? "");
    const agent = String(event.agent_id ?? "");
    const reason = event.reason ? ` (${String(event.reason)})` : "";
    return `🔐 approval ${decision} [${aid}] ${agent} ${tool}${reason}`.trim();
  }
  if (t === "tool.denied") {
    return `🚫 tool denied: ${String(event.tool ?? "?")} (${String(event.reason ?? "")})`;
  }

  if (t === "agent.step") {
    const agent = String(event.agent_id ?? "");
    const result = (event.result ?? {}) as Record<string, unknown>;
    const text = typeof result.text === "string" ? result.text : "";
    if (!text) {
      return null;
    }
    // pt TUI: "💭 {agent_id}:" then body (no hard truncate)
    return `💭 ${agent}:\n${maskSensitive(text)}`;
  }

  if (t === "thread.created") {
    const tid = String(event.thread_id ?? "");
    const name = String(event.name ?? "");
    const participants = Array.isArray(event.participants)
      ? event.participants.map(String).join(", ")
      : "";
    const verb = event.bootstrap ? "opened" : "create_thread";
    const reused = event.reused ? " (reused)" : "";
    return `🧵 [${tid}] ${verb} ${name}${reused} (${participants})`;
  }

  if (t === "message") {
    const content = String(event.content ?? "");
    if (content.startsWith("[ask-user]")) {
      return null;
    }
    const author = String(event.author ?? event.agent_id ?? "");
    const tid = String(event.thread_id ?? "");
    const delivered = Array.isArray(event.delivered_to)
      ? event.delivered_to.map(String)
      : [];
    const targets = delivered.length > 0 ? delivered.join(", ") : "broadcast";
    return `💬 [${author} -> ${targets}][${tid}]\n${maskSensitive(content)}`;
  }

  if (t === "read_resource") {
    const agent = String(event.agent_id ?? "");
    const threads = event.threads ?? 0;
    const messages = event.messages ?? 0;
    return `📊 ${agent}: read_resource (threads=${threads}, messages=${messages})`;
  }

  if (t === "tool") {
    const tool = String(event.tool ?? "");
    const agent = String(event.agent_id ?? "");
    const args = (event.args ?? {}) as Record<string, unknown>;

    // Same skip list as renderer.py — covered by thread.created / message / read_resource
    if (
      !event.protocol_violation &&
      (tool === "send_message" ||
        tool === "create_thread" ||
        tool === "read_resource")
    ) {
      return null;
    }

    if (tool === "ask_user") {
      // Panel handles this via human.question
      return null;
    }

    if (event.protocol_violation) {
      const msg = String(event.violation_message ?? "");
      const phase = String(event.phase ?? "?");
      return `⚠️ [${agent}] PROTOCOL VIOLATION (phase=${phase}): ${msg}`;
    }

    const icon = TOOL_ICONS[tool] ?? "🔧";
    if (tool === "run_command") {
      const cmd = String(args.command ?? "");
      return `${icon} ${agent}: ${tool} \`${short(cmd)}\``;
    }
    if (tool === "fetch_url") {
      const url = String(args.url ?? "");
      return `${icon} ${agent}: ${tool} ${short(url)}`;
    }
    if (
      tool === "read_file" ||
      tool === "write_file" ||
      tool === "edit_file" ||
      tool === "append_file" ||
      tool === "list_directory"
    ) {
      const path = String(args.path ?? "");
      if (path) {
        return `${icon} ${agent}: ${tool} ${basename(path)}`;
      }
      return `${icon} ${agent}: ${tool}`;
    }
    const path = String(args.path ?? "");
    if (path) {
      return `${icon} ${agent}: ${tool} ${basename(path)}`;
    }
    return `${icon} ${agent}: ${tool}`;
  }

  // Unknown wire types: compact fallback (avoid dumping full JSON)
  return `[${t}]`;
}
