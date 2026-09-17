"""Agent loop: step() with inbox drain as single consumer (§3.5.2, §3.6).

Also resolves ``$thread:N`` placeholders against this agent's own
create_thread results, so scripted/real tool sequences can reference
threads created earlier in the same run.

v0.7 (AGENT_TOOLS_EXPANSION_DESIGN.md v4.1):
- ``policy`` param threads a ``ToolPolicy`` into the ``ToolBox`` (P9/P11).
- ``_update_phase_in_prompt`` renders the dynamic tool-instruction block
  (P6) from the active tool specs — only enabled tools are described.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any

from ...backend.base import Completion, ModelBackend
from ..protocol.signals import has_signal, is_ready_message
from ..server import MessageServer
from .approval import (
    ApprovalStore,
    denied_result,
    gate_decision,
    pending_result,
    tool_approval_class,
)
from .policy import ToolPolicy
from .system_prompt import render_system_prompt, render_tool_instructions
from .tools import ToolBox

Message = dict[str, Any]

_THREAD_REF = re.compile(r"^\$thread:(\d+)$")
_THREAD_BY_NAME = re.compile(r"^\$thread_by_name:(.+)$")


@dataclass
class StepResult:
    """Outcome of one agent step."""

    text: str | None = None
    tool_calls: list[Any] = field(default_factory=list)
    drained_count: int = 0
    usage: dict[str, Any] | None = None
    # Infrastructure failure, not something the model said. Session decides
    # whether to retry; the loop never turns it into conversation text.
    error: Any | None = None
    # V1 relevance budget: True when T0 ignore — drain performed, complete skipped.
    # session.run_agent() must check this BEFORE incrementing total_steps.
    skipped: bool = False


def format_radio_block(messages: list[Message]) -> str:
    """§3.6 — drained messages merge into ONE user turn wrapped in [radio].

    Message content passes through unchanged; prefixes like URGENT:/FYI:
    travel inside the content itself.
    """
    lines = ["[radio]"]
    lines.extend(f"from {m['author']}: {m['content']}".rstrip() for m in messages)
    return "\n".join(lines)


def format_radio_block_skim(
    messages: list[Message],
    max_chars: int = 400,
) -> str:
    """V1 T1 skim: digest summary + latest 1 message, truncated to *max_chars*.

    "Latest" is chosen by ``seq`` (review P2-1), falling back to list order
    when ``seq`` is absent. Only that one message is included in full
    (trimmed to *max_chars* if needed).
    """
    if not messages:
        return "[radio — skim]\n(skim: 0 messages)"

    authors = sorted({m["author"] for m in messages})
    lines = [
        "[radio — skim]",
        f"(skim: {len(messages)} message(s) from {', '.join(authors)})",
    ]
    latest = max(
        enumerate(messages),
        key=lambda pair: (
            pair[1].get("seq") is not None,
            pair[1].get("seq", -1),
            pair[0],
        ),
    )[1]
    content = latest.get("content") or ""
    if len(content) > max_chars:
        content = content[:max_chars] + "…"
    lines.append(f"from {latest['author']}: {content}")
    return "\n".join(lines)


def _needs_model_reply(agent_id: str, drained: list[Message]) -> bool:
    """True when a gate-parked agent was addressed and must answer once.

    Peer votes and general chatter are read but not answered — that is the
    whole point of the done-set. A human broadcast does NOT wake everyone:
    with N agents that would cost N completions per remark.
    """
    for message in drained:
        content = str(message.get("content") or "")
        if "URGENT:" in content or "(URGENT)" in content:
            return True
        if agent_id in (message.get("mentions") or []):
            return True
    return False


@dataclass(frozen=True)
class LocalTool:
    """A non-server tool bound directly to the agent (e.g. search)."""

    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]  # sync or async → JSON-serializable


class AgentLoop:
    """One agent's turn loop. The ONLY inbox consumer is step()."""

    def __init__(
        self,
        agent_id: str,
        server: MessageServer,
        backend: ModelBackend,
        system_prompt: str | None = None,
        local_tools: list[LocalTool] | None = None,
        on_tool_call: Callable[[str, str, dict[str, Any], Any], None] | None = None,
        allowed_roots: list[str] | None = None,
        policy: ToolPolicy | None = None,
        role_prompt: str = "",
        has_human: bool = False,
        approvals: ApprovalStore | None = None,
        has_interact_surface: Callable[[], bool] | None = None,
        on_approval_request: Callable[[Any], None] | None = None,
        # V1 relevance budget (AGENT_RELEVANCE_BUDGET_DESIGN.md §5)
        # attention_policy: RelevancePolicy | None — injected by Session.from_config
        # attention_config: dict — deep-merged attention section for this agent
        attention_policy: Any | None = None,
        attention_config: dict[str, Any] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.server = server
        self.backend = backend
        self.policy = policy  # v0.7 — may be None (legacy callers)
        self.tools = ToolBox(server, allowed_roots=allowed_roots, policy=policy)
        self.local_tools: dict[str, LocalTool] = {t.name: t for t in local_tools or []}
        self.approvals = approvals
        self.has_interact_surface = has_interact_surface
        self.on_approval_request = on_approval_request
        self.conversation: list[Message] = [
            {
                "role": "system",
                "content": system_prompt or render_system_prompt(
                    agent_id, role_prompt=role_prompt, has_human=has_human
                ),
            }
        ]
        self._custom_system_prompt = system_prompt is not None
        self._role_prompt = role_prompt
        self._has_human = has_human
        self._human_approval_phases: list[str] = []
        # thread ids this agent created, in creation order ($thread:N source)
        self.created_threads: list[str] = []
        # gate-aware execution state (injected by Session each step)
        self.gate_open: bool = True
        self.gate_thread_id: str | None = None
        self.gate_thread_name: str | None = None
        # Chatter reduction: live views injected by Session each iteration.
        # Sets (never None) so ``in`` is always safe.
        self.gate_approvals: AbstractSet[str] = frozenset()
        self.ready_states: AbstractSet[str] = frozenset()
        # Signal the current gate still needs before any vote counts (e.g.
        # "FINAL:" on P5). None once it has arrived / is not required.
        self.gate_needs_signal: str | None = None
        # The gate's entry signal regardless of whether it has arrived
        # (prompt wording); gate_needs_signal is the "still missing" view.
        self.gate_entry_prefix: str | None = None
        # Live view of who already posted this gate's draft (first writer
        # wins). A COPY would be stale by the time the tool runs: Session
        # injects before step(), and the model call in between takes
        # seconds, so parallel agents would all see None and all draft.
        self.gate_draft_author_fn: Callable[[], str | None] | None = None
        # What P2 agreed this agent would do, and who posts the P5 draft.
        self.assignment: str | None = None
        self.submitter_id: str | None = None
        # waiting-at-a-gate AND this agent already signalled — Session computes
        # it; the loop never re-derives it.
        self.protocol_done: bool = False
        # v0.2: current protocol phase (injected by Session each step)
        self.current_phase: str = ""
        # v0.3: user language (injected by Session at start, propagated to all agents)
        self.language: str = ""
        # Real-time tool event callback (fires immediately on each tool execution)
        self.on_tool_call = on_tool_call
        # V1 relevance budget (AGENT_RELEVANCE_BUDGET_DESIGN.md §5)
        # attention_policy: injected by Session.from_config when attention.enabled=true
        # attention_config: deep-merged attention section (dict; supports .get())
        # phase_floor: injected by Session.run_agent() before each step
        self._attention_policy: Any | None = attention_policy  # RelevancePolicy | None
        self._attention_config: dict[str, Any] = dict(attention_config or {})
        self.phase_floor: float = 0.0

    # -- tool spec passthrough (mode-aware) ---------------------------------

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        specs = self.tools.specs()
        for tool in self.local_tools.values():
            specs.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "schema": tool.schema,
                }
            )
        return specs

    def _update_phase_in_prompt(self) -> None:
        """Update the system prompt to reflect the current phase, language,
        role, and active tools (P6 — dynamic tool block)."""
        if self._custom_system_prompt:
            return  # user-supplied prompt — don't overwrite
        if self.conversation and self.conversation[0]["role"] == "system":
            tool_instructions = render_tool_instructions(self.tool_specs)
            snap = self.server.snapshot()
            threads = [
                {"thread_id": t.get("thread_id"), "name": t.get("name")}
                for t in (snap.get("threads") or [])
                if isinstance(t, dict)
            ]
            ready_tid = self.server.resolve_thread_id("human")
            self.conversation[0]["content"] = render_system_prompt(
                self.agent_id, self.current_phase, self.language,
                role_prompt=self._role_prompt, has_human=self._has_human,
                tool_instructions=tool_instructions,
                human_approval_phases=self._human_approval_phases or None,
                gate_thread_id=self.gate_thread_id,
                gate_thread_name=self.gate_thread_name,
                gate_entry_prefix=self.gate_entry_prefix,
                assignment=self.assignment,
                submitter_id=self.submitter_id,
                session_threads=threads or None,
                ready_thread_id=ready_tid,
            )

    async def step(self) -> StepResult:
        """One model turn. Drains the inbox first; injects a [radio] user turn.

        V1 attention budget (AGENT_RELEVANCE_BUDGET_DESIGN.md §5):
        - T0 ignore: drain performed, backend.complete **skipped**, early return
          with ``skipped=True`` (session MUST NOT increment total_steps).
        - T1 skim: drain + format_radio_block_skim (digest + latest msg only).
        - T2-T3 engage: drain + format_radio_block (unchanged from baseline).

        When ``_attention_policy`` is None or drained is empty, the method
        behaves exactly as before (backward compat).
        """
        # Update system prompt with current phase + active tools
        self._update_phase_in_prompt()
        drained = await self.server.drain_inbox(self.agent_id)

        # ── D5: done at a gate — read the radio, skip the model ───────
        # Runs INSTEAD of the attention branch: T0 may discard messages, but a
        # parked agent still needs the history for when it does wake up.
        if self.protocol_done:
            if drained:
                self.conversation.append(
                    {"role": "user", "content": format_radio_block(drained)}
                )
            if not _needs_model_reply(self.agent_id, drained):
                return StepResult(
                    text=None,
                    drained_count=len(drained),
                    skipped=True,
                )
            # Addressed directly (URGENT / @me) → answer once. Falls through to
            # the normal completion below with the full radio already appended.

        # ── V1 relevance budget branch ────────────────────────────────
        elif drained and self._attention_policy is not None:
            scores = self._attention_policy.score_batch(
                self.agent_id, drained, phase=self.current_phase
            )
            agent_floor = float(
                self._attention_config.get("floors", {}).get("default", 0.0)
            )
            decision = self._attention_policy.decide(
                self.agent_id,
                scores,
                phase=self.current_phase,
                phase_floor=self.phase_floor,
                agent_floor=agent_floor,
            )

            if not decision.run_llm:
                # T0 ignore: drain 했으나 complete 스킵 (§4.3 불변식 2)
                if self._attention_config.get("context", {}).get("t0_digest", False):
                    self.conversation.append(
                        {
                            "role": "user",
                            "content": f"[digest] {len(drained)} message(s) skipped (low relevance)",
                        }
                    )
                return StepResult(
                    text=None,
                    drained_count=len(drained),
                    skipped=True,
                )

            if decision.tier == "skim":
                # T1: digest + 최신 1메시지만
                skim_max = decision.context_max_chars or int(
                    self._attention_config.get("context", {}).get("skim_max_chars", 400)
                )
                self.conversation.append(
                    {
                        "role": "user",
                        "content": format_radio_block_skim(drained, max_chars=skim_max),
                    }
                )
            else:
                # T2 engage / T3 intervene: full radio block (기존 동작)
                self.conversation.append(
                    {"role": "user", "content": format_radio_block(drained)}
                )
        elif drained:
            # attention disabled 또는 policy 없음 → 기존 동작
            self.conversation.append(
                {"role": "user", "content": format_radio_block(drained)}
            )

        completion: Completion = await self.backend.complete(
            self.conversation, self.tool_specs
        )
        if completion.error is not None:
            # Nothing is appended: an API failure must not enter the
            # conversation as an assistant turn. The drained radio above is
            # already in place, so a retry loses nothing.
            return StepResult(
                text=None,
                drained_count=len(drained),
                error=completion.error,
            )
        assistant_msg: Message = {"role": "assistant", "content": completion.text or ""}
        if completion.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in completion.tool_calls
            ]
        self.conversation.append(assistant_msg)

        tool_results: list[Message] = []
        for call in completion.tool_calls:
            args = self._resolve_refs(call.arguments)
            try:
                result = await self._execute_tool(call.name, args)
                if call.name == "create_thread":
                    parsed = json.loads(result)
                    tid = parsed.get("thread_id")
                    if tid:
                        self.created_threads.append(tid)
            except Exception as exc:  # noqa: BLE001 — surfaced to the model verbatim
                result = json.dumps({"error": repr(exc)}, ensure_ascii=False)
            # Fire real-time tool event callback immediately
            if self.on_tool_call is not None:
                self.on_tool_call(self.agent_id, call.name, args, result)
            tool_results.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result,
            })
        if tool_results:
            self.conversation.extend(tool_results)

        nudge = self._unsent_signal_nudge(completion.text, completion.tool_calls)
        if nudge is not None:
            self.conversation.append({"role": "user", "content": nudge})

        return StepResult(
            text=completion.text,
            tool_calls=completion.tool_calls,
            drained_count=len(drained),
            usage=completion.usage,
        )

    async def _execute_tool(self, name: str, args: dict[str, Any]) -> str:
        gated = await self._maybe_gate_approval(name, args)
        if gated is not None:
            return gated
        return await self._run_tool_body(name, args)

    async def _run_tool_body(self, name: str, args: dict[str, Any]) -> str:
        """Execute tool side effects (no approval gate)."""
        if name in self.local_tools:
            tool = self.local_tools[name]
            value = tool.handler(args)
            if hasattr(value, "__await__"):
                value = await value
            return json.dumps(value, ensure_ascii=False, default=str)
        # Protocol sessions: soft-block inventing new threads (reuse by name OK).
        if name == "create_thread" and self._protocol_create_thread_blocked(args):
            return self._protocol_create_thread_denied(args)
        # A pure wait command is the ONE thing that defeats gate-wait park:
        # session.run_agent continues on any tool call, so `sleep` loops forever.
        if name == "run_command" and self._idle_command_blocked(args):
            return json.dumps(
                {
                    "error": "idle_not_allowed",
                    "phase": self.current_phase or "?",
                    "message": (
                        "Gate wait: do not sleep/true to pass time. Reply with "
                        "no tool calls and the runtime will wake you when the "
                        "gate moves."
                    ),
                },
                ensure_ascii=False,
            )
        # Duplicate signal: the vote would not change, but the message would
        # still land on the thread and wake every peer (N^2 chatter).
        if name == "send_message" and not str(args.get("content") or "").strip():
            # An empty broadcast costs every peer a wake-up and says nothing.
            return json.dumps(
                {
                    "error": "empty_message",
                    "message": (
                        "content was empty. Put the actual text in `content` "
                        "-- writing it in your reply instead does not send it."
                    ),
                },
                ensure_ascii=False,
            )
        if name == "send_message":
            dup = self._duplicate_signal_denied(args)
            if dup is not None:
                return dup
        # gate-aware execution: block work-share on non-gate threads while gate is closed
        if name == "send_message" and not self.gate_open:
            thread_id = args.get("thread")
            if thread_id:
                # If gate_thread_id is set, block any non-gate thread
                if self.gate_thread_id is not None and thread_id != self.gate_thread_id:
                    return json.dumps(
                        {
                            "error": "gate_closed",
                            "phase": self.current_phase or "?",
                            "message": (
                                f"Gate is CLOSED. Work-share on thread '{thread_id}' is blocked. "
                                f"Post PROPOSE:/APPROVE: on the gate thread "
                                f"'{self.gate_thread_id}' to open the gate."
                            ),
                        },
                        ensure_ascii=False,
                    )
                # If gate_thread_id is None (initial state, no gate bound yet),
                # only READY messages are allowed (to finish P1).
                # PROPOSE/APPROVE/REJECT are NOT allowed until a gate thread
                # is explicitly bound.
                if self.gate_thread_id is None:
                    content = args.get("content", "")
                    if not is_ready_message(content):
                        return json.dumps(
                            {
                                "error": "gate_closed",
                                "phase": self.current_phase or "?",
                                "message": (
                                    f"Gate is CLOSED. Work-share on thread '{thread_id}' is blocked. "
                                    f"Send READY: (optional text after the colon) to finish P1."
                                ),
                            },
                            ensure_ascii=False,
                        )
        return await self.tools.execute(self.agent_id, name, args)

    # Commands whose only effect is to burn wall-clock.
    _IDLE_COMMANDS = frozenset({"sleep", "true", ":", "timeout"})

    def _idle_command_blocked(self, args: dict[str, Any]) -> bool:
        """True for a pure wait command issued while a gate is closed."""
        if not self._protocol_active() or self.gate_open:
            return False
        command = args.get("command")
        if isinstance(command, list):
            argv = [str(c) for c in command]
        else:
            try:
                argv = shlex.split(str(command or ""))
            except ValueError:
                return False
        if not argv:
            return False
        return argv[0].rsplit("/", 1)[-1] in self._IDLE_COMMANDS

    def _duplicate_signal_denied(self, args: dict[str, Any]) -> str | None:
        """Soft-block a re-``APPROVE:``/``READY:`` from an agent already counted."""
        content = str(args.get("content") or "")
        prefix = self.gate_entry_prefix
        draft_author = (
            self.gate_draft_author_fn() if self.gate_draft_author_fn else None
        )
        if (
            prefix
            and has_signal(content, prefix)
            and draft_author
            and draft_author != self.agent_id
        ):
            # One draft per gate: a second one splits the vote and means
            # everyone ends up approving their own text.
            return json.dumps(
                {
                    "error": "draft_already_posted",
                    "phase": self.current_phase or "?",
                    "author": draft_author,
                    "needs": prefix,
                    "message": (
                        f"{draft_author} already posted the {prefix} "
                        f"draft. Read it and APPROVE: it, or REJECT: to ask "
                        f"for a redo."
                    ),
                },
                ensure_ascii=False,
            )
        if has_signal(content, "APPROVE:") and self.gate_needs_signal:
            # Voting before the gate's entry signal exists cannot open it, and
            # a pile of votes on a gate that will not move reads like a stall.
            needs = self.gate_needs_signal
            return json.dumps(
                {
                    "error": "entry_signal_required",
                    "phase": self.current_phase or "?",
                    "needs": needs,
                    "message": (
                        f"Nothing to approve yet: this gate has no {needs} "
                        f"message. Post the content itself starting with "
                        f"{needs} (anyone may), then approve it."
                    ),
                },
                ensure_ascii=False,
            )
        if has_signal(content, "APPROVE:"):
            if self.gate_open or self.agent_id not in self.gate_approvals:
                return None
            if args.get("thread") != self.gate_thread_id:
                return None
            return json.dumps(
                {
                    "error": "already_approved",
                    "phase": self.current_phase or "?",
                    "approvals": sorted(self.gate_approvals),
                    "message": (
                        "You already APPROVE:d. Stay silent until the gate opens, "
                        "or send REJECT: to reset the votes."
                    ),
                },
                ensure_ascii=False,
            )
        if is_ready_message(content) and self.agent_id in self.ready_states:
            return json.dumps(
                {
                    "error": "already_ready",
                    "phase": self.current_phase or "?",
                    "message": (
                        "You already sent READY:. Stay silent until the other "
                        "agents finish exploring."
                    ),
                },
                ensure_ascii=False,
            )
        return None

    def _unsent_signal_nudge(
        self,
        text: str | None,
        tool_calls: list[Any],
    ) -> str | None:
        """Catch a signal written as prose instead of sent as a message.

        A model that writes ``APPROVE: ...`` in its reply has decided, but the
        protocol only sees ``send_message``. Live: this hung P2 in session
        `d6bcbc56` and P1 in `43addf1b`, both until a human stepped in.

        P1 has no gate object, so its signal (``READY:``) and its done-set
        (``ready_states``) live in different fields than P2-P5's.
        """
        if not text or self.gate_open or not self._protocol_active():
            return None
        if any(call.name == "send_message" for call in tool_calls):
            return None
        if self.gate_thread_id is None:
            done: AbstractSet[str] = self.ready_states
            prefixes = ["READY:"]
            target = self.server.resolve_thread_id("human")
        else:
            done = self.gate_approvals
            prefixes = [p for p in (self.gate_entry_prefix, "APPROVE:") if p]
            target = self.gate_thread_id
        if self.agent_id in done:
            return None  # already counted -- nothing to nudge about
        found = next((p for p in prefixes if has_signal(text, p)), None)
        if found is None:
            return None
        where = f" Send it to thread `{target}`." if target else ""
        return (
            f"[runtime] You wrote {found} in your reply, but nobody received "
            f"it -- only `send_message` actually sends it.{where}"
        )

    def _protocol_active(self) -> bool:
        """True while a P1–P5 collaboration phase is in progress."""
        phase = (self.current_phase or "").strip()
        if not phase or phase in ("COMPLETED", "REJECTED"):
            return False
        return phase.startswith("P")

    def _protocol_create_thread_blocked(self, args: dict[str, Any]) -> bool:
        if not self._protocol_active():
            return False
        name_arg = str(args.get("name") or "").strip()
        if not name_arg:
            return True
        # Same name → MessageServer reuses; allow that path.
        return self.server.resolve_thread_id(name_arg) is None

    def _protocol_create_thread_denied(self, args: dict[str, Any]) -> str:
        snap = self.server.snapshot()
        roster = [
            {"thread_id": t.get("thread_id"), "name": t.get("name")}
            for t in (snap.get("threads") or [])
            if isinstance(t, dict)
        ]
        return json.dumps(
            {
                "error": "protocol_threads_fixed",
                "phase": self.current_phase or "?",
                "message": (
                    f"Do not create a new thread named {args.get('name')!r} "
                    "during P1-P5. Reuse an open session thread id from the "
                    "system prompt (or call create_thread with an existing name)."
                ),
                "threads": roster,
            },
            ensure_ascii=False,
        )

    async def _maybe_gate_approval(self, name: str, args: dict[str, Any]) -> str | None:
        """Return a JSON tool result when approval blocks execution; else None."""
        if self.approvals is None or self.policy is None:
            return None
        if tool_approval_class(name) is None:
            return None
        if not self.policy.requires_approval(name, args=args):
            return None

        has_interact = False
        if self.has_interact_surface is not None:
            has_interact = bool(self.has_interact_surface())
        decision = gate_decision(
            requires_approval=True,
            bypass=self.policy.approval_bypass,
            has_interact_surface=has_interact,
        )
        if decision in ("execute", "bypass"):
            return None
        if decision == "deny_no_channel":
            return json.dumps(
                denied_result("no_approval_channel", tool=name),
                ensure_ascii=False,
            )

        rec, created = self.approvals.request_or_join(
            self.agent_id,
            name,
            args,
            ttl_seconds=self.policy.approval_ttl_seconds,
        )
        if created and self.on_approval_request is not None:
            self.on_approval_request(rec)
        return json.dumps(
            pending_result(rec.approval_id, name, args=args),
            ensure_ascii=False,
        )

    def _resolve_refs(self, args: dict[str, Any]) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for key, value in args.items():
            if isinstance(value, str):
                m = _THREAD_BY_NAME.match(value)
                if m:
                    resolved[key] = self._find_thread_by_name(m.group(1))
                    continue
                m = _THREAD_REF.match(value)
                if m:
                    idx = int(m.group(1))
                    try:
                        resolved[key] = self.created_threads[idx]
                        continue
                    except IndexError:
                        raise ValueError(
                            f"$thread:{idx} has no matching create_thread result "
                            f"(agent {self.agent_id} created {len(self.created_threads)})"
                        ) from None
            resolved[key] = value
        return resolved

    def _find_thread_by_name(self, name: str) -> str:
        """Resolve a thread id from the SSOT by name (what read_resource offers)."""
        for thread in self.server.snapshot()["threads"]:
            if thread["name"] == name:
                return thread["thread_id"]
        raise ValueError(f"no thread named {name!r} exists yet")
