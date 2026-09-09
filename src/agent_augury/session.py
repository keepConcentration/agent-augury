"""Session = agent bundle + internal message server + lifecycle (§3.2).

Run loop: parallel independent asyncio tasks; each agent runs one step()
per iteration and yields cooperatively (``await asyncio.sleep(0)``). An
agent is finished when its completion produced neither text nor tool
calls, or when its scripted backend runs dry (IndexError).

v0.2: integrates the P1~P5 collaboration protocol. When a protocol is
configured, the session drives phase transitions and injects phase context
into each agent's system prompt.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

# .env 자동 로딩 — cwd → 프로젝트 루트 순서로 탐색
try:
    from dotenv import load_dotenv
    _cwd_env = Path.cwd() / ".env"
    if _cwd_env.exists():
        load_dotenv(_cwd_env, override=False)
    else:
        _root_env = Path(__file__).resolve().parents[2] / ".env"
        if _root_env.exists():
            load_dotenv(_root_env, override=False)
except ImportError:
    pass

from .agent.loop import AgentLoop
from .auth.token_store import TokenStore
from .backends_factory import build_backend
from .channel.discord_bot import BotManager, DiscordBotAdapter, _format_event
from .channel.discord_mirror import mirror_from_config
from .protocol.approval import ConsensusGate
from .protocol.collaboration import CollaborationProtocol
from .protocol.phases import (
    COMPLETED,
    P1_EXPLORE,
    P2_SPLIT,
    P3_EXECUTE,
    P4_REVIEW,
    P5_SUBMIT,
    Phase,
)
from .server import MessageServer

OnStep = Callable[[str, Any], None]
OnToolEvent = Callable[[dict[str, Any]], None]


class Session:
    def __init__(
        self,
        server: MessageServer,
        agents: list[AgentLoop],
        *,
        task: str | None = None,
        max_steps: int = 0,
        bot_manager: BotManager | None = None,
        has_human: bool = False,
    ) -> None:
        self.server = server
        self.agents = agents
        self.task = task
        self.max_steps = max_steps
        self.has_human = has_human
        self.on_step: OnStep | None = None
        self.on_tool_event: OnToolEvent | None = None
        self.gate: ConsensusGate | None = None
        self.mirror: Any = None
        # v0.2: P1~P5 collaboration protocol
        self.protocol: CollaborationProtocol | None = None
        # v0.3: Discord bot manager (N개 Client)
        self.bot_manager = bot_manager
        # Unified output queue for all display events (tools, steps, read_resource)
        self._output_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._output_task: asyncio.Task | None = None
        self._setup_done: bool = False
        self._closed: bool = False

    # -- assembly ------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        cfg: dict[str, Any],
        on_step=None,
        on_tool_event=None,
        allowed_roots: list[str] | None = None,
        token_store: TokenStore | None = None,
    ) -> Session:
        server = MessageServer()
        agents: list[AgentLoop] = []
        # Shared token store so all backends use the same OAuth tokens
        shared_token_store = token_store or TokenStore()

        # Human-in-the-loop: 항상 내장 (v1.0)
        # config에 human 섹션이 있든 없든, 항상 켜져 있음
        has_human = True
        server.register_human()

        for spec in cfg["agents"]:
            server.register_agent(spec["id"])
            # role 처리: role → roles 프리셋의 prompt 사용, role_custom → 직접 사용
            role_prompt = _resolve_role_prompt(spec, cfg.get("roles"))
            agents.append(
                AgentLoop(
                    agent_id=spec["id"],
                    server=server,
                    backend=build_backend(spec["backend"], token_store=shared_token_store),
                    allowed_roots=allowed_roots,
                    role_prompt=role_prompt,
                    has_human=has_human,
                    on_tool_call=lambda agent_id, tool, args, result, _server=server: (
                        _server._emit_event({
                            "type": "tool",
                            "agent_id": agent_id,
                            "tool": tool,
                            "args": args,
                            "result": result,
                            "timestamp": __import__("time").time(),
                        })
                    ),
                )
            )

        # v0.3: bots 섹션 파싱 → BotManager 구성
        bot_manager: BotManager | None = None
        bots_spec = cfg.get("bots")
        if bots_spec:
            bot_manager = BotManager()
            for bot_entry in bots_spec:
                token = os.environ.get(bot_entry["token_env"], "")
                adapter = DiscordBotAdapter(
                    agent_id=bot_entry["agent_id"],
                    token=token,
                    channel_id=int(bot_entry["channel_id"]),
                )
                bot_manager.register(adapter)

        session = cls(
            server=server,
            agents=agents,
            task=cfg.get("task"),
            max_steps=int(cfg.get("max_steps", 0)),
            bot_manager=bot_manager,
            has_human=has_human,
        )
        session.on_step = on_step
        session.on_tool_event = on_tool_event
        gate_spec = cfg.get("gate")
        if gate_spec:
            session.gate = ConsensusGate(server, thread_name=gate_spec["thread_name"])
            server.subscribe(session.gate.on_message)
            # gate-aware: when gate opens, mark all agents as gate_open=True
            def _on_gate_open() -> None:
                for agent in session.agents:
                    agent.gate_open = True
                    agent.gate_thread_id = session.gate.thread_id
            session.gate.on_open(_on_gate_open)
        # v0.2: P1~P5 collaboration protocol
        protocol_spec = cfg.get("protocol")
        if protocol_spec:
            participant_ids = [a.agent_id for a in agents]
            session.protocol = CollaborationProtocol(
                server=server,
                participants=protocol_spec.get("participants", participant_ids),
                assembler_id=protocol_spec.get("assembler_id"),
            )
            # Wire up gates for each phase
            for phase_name, thread_name in protocol_spec.get("gates", {}).items():
                phase = _phase_from_string(phase_name)
                # P2 requires proposal; P3+ do not (work logs start immediately)
                require_proposal = (phase == P2_SPLIT)
                session.protocol.bind_gate(phase, thread_name, require_proposal=require_proposal)
            # Auto-advance on gate open
            session.protocol.on_gate_open(
                lambda phase: _on_protocol_gate_open(session, phase)
            )
        session.mirror = mirror_from_config(cfg.get("mirror"))
        if session.mirror is not None:
            server.subscribe(session.mirror.on_message)
        # Subscribe server events to unified output queue
        server.subscribe_events(session._on_server_event)
        return session

    # -- lifecycle -----------------------------------------------------------

    async def _setup(self) -> None:
        """One-time initialization: start bots, bind gates, start protocol.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._setup_done:
            return
        self._setup_done = True

        # Start unified output consumer task
        self._output_task = asyncio.create_task(self._output_consumer())

        # v0.3: start bots (login to Discord) — same asyncio loop
        if self.bot_manager:
            await self.bot_manager.start_all()

        # gate-aware: inject gate state into agents
        if self.gate:
            # Pre-create the gate thread and bind it
            participant_ids = [a.agent_id for a in self.agents]
            tid = await self.server.create_thread(
                self.gate.thread_name, participants=participant_ids
            )
            self.gate.bind_to_thread(tid)
            for agent in self.agents:
                agent.gate_open = self.gate.is_open
                agent.gate_thread_id = self.gate.thread_id

        # v0.2: start the collaboration protocol
        if self.protocol:
            # Pre-create threads for each gate and bind them explicitly.
            for gate in self.protocol._gates.values():
                if gate is not None:
                    tid = await self.server.create_thread(
                        gate.thread_name, participants=self.protocol.participants
                    )
                    gate.bind_to_thread(tid)
            self.protocol.start()
            # Inject initial phase context + gate state
            for agent in self.agents:
                agent.current_phase = self.protocol.phase
                _inject_protocol_gate_state(agent, self.protocol)

    async def human_send(
        self,
        thread_id: str,
        content: str,
        *,
        mentions: list[str] | None = None,
    ) -> str:
        """Inject a message from the human participant into the session.

        Convenience passthrough to ``server.human_send`` with ``author="human"``.
        Raises if no human is configured (``has_human`` is False). The reply is
        pushed to agent inboxes and absorbed as a ``[radio]`` block on their
        next ``step()``.
        """
        if not self.has_human:
            raise RuntimeError("human-in-the-loop is not enabled (no 'human:' section in config)")
        return await self.server.human_send(
            thread_id, author="human", content=content, mentions=mentions
        )

    async def run(self, initial_prompt: str | None = None) -> int:
        """Parallel steps until every agent finishes or max_steps is hit.

        Each agent runs as an independent ``asyncio.Task``. All agents share
        a global step budget (``max_steps``); the sum of every agent's steps
        is capped. An agent finishes when it produces neither text nor tool
        calls and has no pending inbox messages — same rule as the prior
        round-robin loop.

        Output events (step summaries, tool calls) are pushed to the unified
        ``_output_queue``; the single consumer task renders them in arrival
        order, so tool logs stream in the order they actually fire.

        v0.3: bots are started (Discord login) at the beginning of the session
        and stopped (connection cleanup) at the end, in the same asyncio loop.

        Reusable: call run() multiple times to continue the conversation.
        The first call initializes bots/gates/protocol; subsequent calls
        reuse them. Call close() when done to release resources.
        """
        await self._setup()
        return await self._run_impl(initial_prompt)

    async def _run_impl(self, initial_prompt: str | None = None) -> int:
        """Core run logic (separated so start/stop wraps it cleanly)."""
        # Broadcast the initial task to ALL agents (not just agents[0]), so
        # every worker gets the same user prompt and acts on it per its role.
        user_text = initial_prompt or self.task or ""
        if user_text:
            for agent in self.agents:
                agent.conversation.append({"role": "user", "content": user_text})

        # v0.3: detect user language from initial prompt → inject into all agents
        from .agent.system_prompt import detect_language
        detected_lang = detect_language(user_text)
        if detected_lang:
            for agent in self.agents:
                agent.language = detected_lang

        # Global step counter. asyncio is single-threaded, so += is atomic;
        # the cap is checked at the top of each agent loop iteration.
        total_steps = 0

        async def run_agent(agent: AgentLoop) -> None:
            """Run one agent's step loop as long as it makes progress and the
            global budget allows."""
            nonlocal total_steps
            while True:
                # Global budget gate — checked before every step.
                if self.max_steps and total_steps >= self.max_steps:
                    break

                # Inject current gate state before each step.
                if self.gate:
                    agent.gate_open = self.gate.is_open
                    agent.gate_thread_id = self.gate.thread_id
                # v0.2: inject current protocol phase + gate state.
                if self.protocol:
                    agent.current_phase = self.protocol.phase
                    _inject_protocol_gate_state(agent, self.protocol)

                try:
                    result = await agent.step()
                except IndexError:
                    # Script exhausted — agent has no more completions.
                    # This is a normal finish, not an error.
                    break
                except Exception as exc:  # noqa: BLE001 — single agent failure
                    # Agent step failed — mark as finished so the session
                    # continues with remaining agents instead of aborting.
                    print(
                        f"  [{agent.agent_id}] step failed: {exc}",
                        flush=True,
                    )
                    break

                # Increment step counter only after a successful step.
                total_steps += 1

                # Step summary queued for display.
                await self._output_queue.put({
                    "type": "step",
                    "agent_id": agent.agent_id,
                    "result": result,
                    "timestamp": __import__("time").time(),
                })

                # An agent is finished only when it produces no output AND
                # has no pending messages to process. If it sent messages,
                # it should stay alive to read responses in future steps.
                has_pending = self.server.inbox_size(agent.agent_id) > 0
                if not result.tool_calls and result.text is None and not has_pending:
                    break

                # Yield control so other agents can make progress.
                # Without this, a single agent whose backend completes
                # synchronously (e.g. cached/fake backends) could monopolize
                # the event loop and starve the others.
                await asyncio.sleep(0)

        # Launch all agents as parallel asyncio tasks.
        tasks = [asyncio.create_task(run_agent(agent)) for agent in self.agents]
        await asyncio.gather(*tasks)

        return total_steps

    async def close(self) -> None:
        """Release resources: stop bots, close mirror, close backends.

        Call when the session is no longer needed. Safe to call multiple
        times — subsequent calls are no-ops.
        """
        if self._closed:
            return
        self._closed = True

        # Shutdown unified output consumer.
        await self._output_queue.put(None)
        if self._output_task:
            await self._output_task

        # v0.3: stop bots (close Discord connections, prevent leaks)
        if self.bot_manager:
            await self.bot_manager.stop_all()

        if self.mirror is not None:
            await self.mirror.aclose()
        for agent in self.agents:
            aclose = getattr(agent.backend, "aclose", None)
            if aclose is not None:
                await aclose()

    def _on_server_event(self, event: dict[str, Any]) -> None:
        """Capture server events and queue them for unified output.

        Also routes events to the Discord bot manager (if configured).
        """
        # v0.3: 봇 라우팅 (발신 전용)
        if self.bot_manager:
            agent_id = event.get("agent_id")
            if agent_id is None:
                # events without agent_id (create_thread, send_message) —
                # route by author if available
                agent_id = event.get("author")
            if agent_id:
                content = _format_event(event)
                if content:
                    self.bot_manager.route_event(agent_id, content)

        event_type = event["type"]
        if event_type == "tool":
            try:
                self._output_queue.put_nowait({
                    "type": "tool",
                    "agent_id": event["agent_id"],
                    "tool": event["tool"],
                    "args": event.get("args", {}),
                    "timestamp": event.get("timestamp", __import__("time").time()),
                })
            except asyncio.QueueFull:
                pass
        elif event_type == "read_resource":
            try:
                self._output_queue.put_nowait({
                    "type": "read_resource",
                    "agent_id": event["agent_id"],
                    "threads": event["threads"],
                    "messages": event["messages"],
                    "timestamp": event.get("timestamp", __import__("time").time()),
                })
            except asyncio.QueueFull:
                pass
        elif event_type == "create_thread":
            try:
                self._output_queue.put_nowait({
                    "type": "create_thread",
                    "thread_id": event["thread_id"],
                    "name": event["name"],
                    "participants": event["participants"],
                    "timestamp": event.get("timestamp", __import__("time").time()),
                })
            except asyncio.QueueFull:
                pass
        elif event_type == "send_message":
            try:
                self._output_queue.put_nowait({
                    "type": "send_message",
                    "message_id": event["message_id"],
                    "thread_id": event["thread_id"],
                    "author": event["author"],
                    "content": event["content"],
                    "delivered_to": event.get("delivered_to", []),
                    "timestamp": event.get("timestamp", __import__("time").time()),
                })
            except asyncio.QueueFull:
                pass

    async def _output_consumer(self) -> None:
        """Consume output events from queue and emit to callbacks."""
        while True:
            event = await self._output_queue.get()
            if event is None:
                break
            event_type = event.get("type")
            if event_type == "tool" and self.on_tool_event:
                self.on_tool_event(event)
            elif event_type == "step" and self.on_step:
                self.on_step(event["agent_id"], event["result"])
            elif event_type == "read_resource" and self.on_tool_event or event_type == "create_thread" and self.on_tool_event or event_type == "send_message" and self.on_tool_event:
                self.on_tool_event(event)
            elif event_type == "create_thread" or event_type == "send_message":
                # Fallback if no on_tool_event
                pass


def _resolve_role_prompt(spec: dict[str, Any], roles: dict[str, Any] | None) -> str:
    """Resolve the role prompt for an agent spec.

    - ``role``: looks up the preset in ``roles`` and returns its ``prompt``.
    - ``role_custom``: returns the inline string directly.
    - neither: returns "" (no role injected).
    """
    role = spec.get("role")
    role_custom = spec.get("role_custom")
    if role_custom is not None:
        return role_custom
    if role is not None and roles is not None:
        preset = roles.get(role, {})
        return preset.get("prompt", "")
    return ""


def _phase_from_string(name: str) -> Phase:
    """Convert a phase string to a Phase constant."""
    mapping = {
        "P1_EXPLORE": P1_EXPLORE,
        "P2_SPLIT": P2_SPLIT,
        "P3_EXECUTE": P3_EXECUTE,
        "P4_REVIEW": P4_REVIEW,
        "P5_SUBMIT": P5_SUBMIT,
    }
    if name not in mapping:
        raise ValueError(f"unknown phase: {name!r}")
    return mapping[name]


def _on_protocol_gate_open(session: Session, phase: Phase) -> None:
    """Handle gate open events from the collaboration protocol."""
    # Auto-advance to the next phase when a gate opens
    transitions = {
        P2_SPLIT: P3_EXECUTE,
        P3_EXECUTE: P4_REVIEW,
        P4_REVIEW: P5_SUBMIT,
        P5_SUBMIT: COMPLETED,
    }
    next_phase = transitions.get(phase)
    if next_phase and session.protocol:
        session.protocol.advance(next_phase)


def _inject_protocol_gate_state(agent, protocol: CollaborationProtocol) -> None:
    """Inject the current phase's gate state into an agent.

    When a phase has a gate, the agent's gate_open/gate_thread_id reflect
    that gate's state. When the gate is closed, work-share on non-gate
    threads is blocked.

    P1_EXPLORE: gate_open=False, gate_thread_id=None — only READY: messages
    are allowed (to finish exploration). All other send_message calls are
    blocked by the gate-closed logic in AgentLoop._execute_tool.
    """
    gate = protocol.gate_for(protocol.phase)
    if gate is not None:
        agent.gate_open = gate.is_open
        agent.gate_thread_id = gate.thread_id
    elif protocol.phase == P1_EXPLORE:
        # P1: only READY: messages allowed to finish exploration
        agent.gate_open = False
        agent.gate_thread_id = None
    else:
        # Other phases without a gate — no restriction
        agent.gate_open = True
        agent.gate_thread_id = None
