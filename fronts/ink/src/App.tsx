import React, {useEffect, useRef, useState} from "react";
import {Box, Text, Static, useApp, useInput} from "ink";
import TextInput from "ink-text-input";
import {GatewayChild} from "./gateway.js";
import Markdown from "./markdown.js";
import {parseHumanMentions} from "./mentions.js";
import {formatEvent, formatGate, makeCommand, type WireMessage} from "./wire.js";

type LogItem = {
  id: number;
  text: string;
  /** Agent prose / messages — render as Markdown (pt TUI parity). */
  markdown?: boolean;
};

type PendingQuestion = {
  question_id?: string;
  agent_id?: string;
  thread_id?: string;
  question: string;
  options: string[];
};

type PendingApproval = {
  approval_id: string;
  agent_id?: string;
  tool: string;
  argsPreview?: Record<string, unknown>;
  ttlSeconds?: number;
};

function previewApprovalArgs(args?: Record<string, unknown>): string {
  if (!args || typeof args !== "object") {
    return "";
  }
  if (typeof args.command === "string") {
    return String(args.command);
  }
  if (typeof args.path === "string") {
    return String(args.path);
  }
  try {
    const raw = JSON.stringify(args);
    return raw.length > 120 ? `${raw.slice(0, 120)}...` : raw;
  } catch {
    return "";
  }
}

function parseApprovalDecision(line: string): "granted" | "denied" | null {
  const t = line.trim().toLowerCase();
  if (t === "1" || t === "y" || t === "yes" || t === "approve" || t === "/approve") {
    return "granted";
  }
  if (t === "2" || t === "n" || t === "no" || t === "deny" || t === "/deny") {
    return "denied";
  }
  return null;
}

export default function App() {
  const {exit} = useApp();
  const [logs, setLogs] = useState<LogItem[]>([]);
  const [value, setValue] = useState("");
  const [status, setStatus] = useState("starting...");
  const [running, setRunning] = useState(true);
  const [pending, setPending] = useState<PendingQuestion | null>(null);
  const [pendingApproval, setPendingApproval] = useState<PendingApproval | null>(
    null,
  );
  const [agents, setAgents] = useState<string[]>([]);
  const [gate, setGate] = useState<string | null>(null);
  const gwRef = useRef<GatewayChild | null>(null);
  const lastCtrlC = useRef(0);

  const pushLog = (text: string, markdown = false) => {
    setLogs((prev) => [...prev, {id: prev.length + 1, text, markdown}]);
  };

  useEffect(() => {
    const child = new GatewayChild({
      onMessage: (msg: WireMessage) => {
        if (msg.dir === "event") {
          if (msg.type === "session.started") {
            const roster = Array.isArray(msg.agents)
              ? msg.agents.map(String).filter(Boolean)
              : [];
            if (roster.length > 0) {
              setAgents(roster);
            }
            setStatus(
              roster.length > 0
                ? `ready · ${roster.map((a) => `@${a}`).join(" ")}`
                : "ready",
            );
          }
          if (msg.type === "session.gate") {
            setGate(formatGate(msg));
          }
          if (msg.type === "approval.request") {
            const approvalId = String(msg.approval_id ?? "");
            if (approvalId) {
              setPendingApproval({
                approval_id: approvalId,
                agent_id: msg.agent_id ? String(msg.agent_id) : undefined,
                tool: String(msg.tool ?? "tool"),
                argsPreview:
                  msg.args_preview && typeof msg.args_preview === "object"
                    ? (msg.args_preview as Record<string, unknown>)
                    : undefined,
                ttlSeconds:
                  typeof msg.ttl_seconds === "number" ? msg.ttl_seconds : undefined,
              });
              setStatus("approval");
            }
          }
          if (
            msg.type === "approval.resolved" ||
            msg.type === "approval.granted" ||
            msg.type === "approval.expired" ||
            msg.type === "tool.denied"
          ) {
            const aid = String(msg.approval_id ?? "");
            setPendingApproval((prev) =>
              prev && (!aid || prev.approval_id === aid) ? null : prev,
            );
            setStatus((s) => (s === "approval" ? "running" : s));
          }
          if (msg.type === "human.question") {
            setPending({
              question_id: msg.question_id,
              agent_id: msg.agent_id,
              thread_id: msg.thread_id,
              question: String(msg.question ?? ""),
              options: Array.isArray(msg.options) ? msg.options.map(String) : [],
            });
            setStatus((s) => (s === "approval" ? s : "ask_user"));
          }
          if (msg.type === "log" && String(msg.text ?? "").includes("interrupted")) {
            setRunning(false);
            setStatus("interrupted");
          }
          const line = formatEvent(msg);
          if (line) {
            const asMd =
              msg.type === "agent.step" ||
              msg.type === "message" ||
              msg.type === "human.question";
            pushLog(line, asMd);
          }
          if (msg.type === "session.ended") {
            setStatus("ended");
            setTimeout(() => exit(), 50);
          }
        } else if (msg.dir === "result") {
          if (!msg.ok) {
            pushLog(`! ${msg.error ?? "command failed"}`);
          }
        }
      },
      onExit: (code) => {
        setStatus(`gateway exit ${code ?? "?"}`);
        exit();
      },
      onError: (err) => {
        pushLog(err.message);
      },
    });
    gwRef.current = child;
    setStatus("connected");
    return () => child.kill();
  }, [exit]);

  useInput((input, key) => {
    if (!(key.ctrl && input === "c")) {
      return;
    }
    const gw = gwRef.current;
    if (!gw) {
      return;
    }
    const now = Date.now();
    const double = lastCtrlC.current > 0 && now - lastCtrlC.current < 1000;
    lastCtrlC.current = now;

    if (running && !double) {
      gw.send(makeCommand("session.interrupt"));
      pushLog(
        "Agents stopped. Send a message to resume, or press Ctrl+C again to exit.",
      );
      setRunning(false);
      setStatus("interrupted");
      return;
    }
    if (double) {
      pushLog("Ctrl+C twice - exiting...");
      gw.send(makeCommand("session.quit"));
      return;
    }
    pushLog("Press Ctrl+C again to exit, or type /quit");
  });

  const onSubmit = (line: string) => {
    const trimmed = line.trim();
    setValue("");
    const gw = gwRef.current;
    if (!trimmed || !gw) {
      return;
    }
    if (trimmed === "/quit" || trimmed === "/exit") {
      gw.send(makeCommand("session.quit"));
      return;
    }
    if (trimmed === "/skip") {
      gw.send(
        makeCommand("human.skip", {
          question_id: pending?.question_id,
        }),
      );
      setPending(null);
      setStatus(running ? "running" : "idle");
      return;
    }

    // Tool approval takes priority over ask_user answers.
    if (pendingApproval) {
      const decision = parseApprovalDecision(trimmed);
      if (!decision) {
        pushLog("! type 1/approve or 2/deny");
        return;
      }
      pushLog(`> approval ${decision} [${pendingApproval.approval_id}]`);
      gw.send(
        makeCommand("approval.resolve", {
          approval_id: pendingApproval.approval_id,
          decision,
        }),
      );
      setPendingApproval(null);
      setStatus(running ? "running" : "idle");
      return;
    }

    const {mentions, unknown} = parseHumanMentions(trimmed, agents);
    if (unknown.length > 0) {
      const hint =
        agents.length > 0 ? ` (known: ${agents.join(", ")})` : "";
      pushLog(`! unknown @mention: ${unknown.map((u) => `@${u}`).join(", ")}${hint}`);
    }
    // User typed @… but none matched the roster — don't accidentally broadcast.
    if (unknown.length > 0 && mentions.length === 0) {
      return;
    }

    const to =
      mentions.length > 0 ? ` → ${mentions.map((m) => `@${m}`).join(" ")}` : "";
    pushLog(`> ${trimmed}${to}`);

    if (pending) {
      const fields: Record<string, unknown> = {
        content: trimmed,
        thread_id: pending.thread_id,
        question_id: pending.question_id,
      };
      if (mentions.length > 0) {
        fields.mentions = mentions;
      }
      gw.send(makeCommand("human.answer", fields));
      setPending(null);
      setStatus(running ? "running" : "idle");
      return;
    }

    const fields: Record<string, unknown> = {content: trimmed};
    if (mentions.length > 0) {
      fields.mentions = mentions;
    }
    gw.send(makeCommand("human.send", fields));
    if (!running) {
      setRunning(true);
      setStatus("running");
    }
  };

  return (
    <Box flexDirection="column">
      <Static items={logs}>
        {(item) => (
          <Box key={item.id} flexDirection="column" marginBottom={1}>
            {item.markdown ? <Markdown>{item.text}</Markdown> : <Text>{item.text}</Text>}
          </Box>
        )}
      </Static>

      {pendingApproval ? (
        <Box flexDirection="column" marginTop={1} borderStyle="round" paddingX={1}>
          <Text color="magenta">
            approval [{pendingApproval.agent_id ?? "?"}] {pendingApproval.tool}
          </Text>
          {previewApprovalArgs(pendingApproval.argsPreview) ? (
            <Text>{previewApprovalArgs(pendingApproval.argsPreview)}</Text>
          ) : null}
          {pendingApproval.ttlSeconds != null ? (
            <Text dimColor>{`ttl ${pendingApproval.ttlSeconds}s`}</Text>
          ) : null}
          <Text>{`  [1] approve`}</Text>
          <Text>{`  [2] deny`}</Text>
          <Text dimColor>type 1/approve or 2/deny</Text>
        </Box>
      ) : null}

      {!pendingApproval && pending ? (
        <Box flexDirection="column" marginTop={1} borderStyle="round" paddingX={1}>
          <Text color="yellow">
            ask_user [{pending.agent_id ?? "?"}]{" "}
          </Text>
          <Markdown>{pending.question}</Markdown>
          {pending.options.map((opt, i) => (
            <Box key={`${i}-${opt}`}>
              <Text>{`  [${i + 1}] `}</Text>
              <Markdown>{opt}</Markdown>
            </Box>
          ))}
          <Text dimColor>/skip to dismiss</Text>
        </Box>
      ) : null}

      {gate ? (
        <Box marginTop={1}>
          <Text color="green">{gate}</Text>
        </Box>
      ) : null}

      <Box marginTop={gate ? 0 : 1}>
        <Text dimColor>[{status}] </Text>
        <Text color="cyan">ink&gt; </Text>
        <TextInput value={value} onChange={setValue} onSubmit={onSubmit} />
      </Box>
    </Box>
  );
}
