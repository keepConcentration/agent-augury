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
from typing import Any, Callable

from .agent.loop import AgentLoop
from .backends_factory import build_backend
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
    REJECTED,
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
        max_steps: int = 20,
    ) -> None:
        self.server = server
        self.agents = agents
        self.task = task
        self.max_steps = max_steps
        self.on_step: OnStep | None = None
        self.on_tool_event: OnToolEvent | None = None
        self.gate: ConsensusGate | None = None
        self.mirror: Any = None
        # v0.2: P1~P5 collaboration protocol
        self.protocol: CollaborationProtocol | None = None
        # Unified output queue for all display events (tools, steps, read_resource)
        self._output_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._output_task: asyncio.Task | None = None

    # -- assembly ------------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: dict[str, Any], on_step=None, on_tool_event=None, allowed_roots: list[str] | None = None) -> "Session":
        server = MessageServer()
        server.set_mode(cfg.get("mode", "L3"))
        agents: list[AgentLoop] = []
        for spec in cfg["agents"]:
            server.register_agent(spec["id"])
            agents.append(
                AgentLoop(
                    agent_id=spec["id"],
                    server=server,
                    backend=build_backend(spec["backend"]),
                    allowed_roots=allowed_roots,
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
        session = cls(
            server=server,
            agents=agents,
            task=cfg.get("task"),
            max_steps=int(cfg.get("max_steps", 20)),
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
        """
        # Start unified output consumer task
        self._output_task = asyncio.create_task(self._output_consumer())

        if initial_prompt:
            self.agents[0].conversation.append({"role": "user", "content": initial_prompt})
        elif self.task:
            self.agents[0].conversation.append({"role": "user", "content": self.task})

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
            for phase, gate in self.protocol._gates.items():
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

        # Global step counter. asyncio is single-threaded, so += is atomic;
        # the cap is checked at the top of each agent loop iteration.
        total_steps = 0

        async def run_agent(agent: AgentLoop) -> None:
            """Run one agent's step loop as long as it makes progress and the
            global budget allows."""
            nonlocal total_steps
            while True:
                # Global budget gate — checked before every step.
                if total_steps >= self.max_steps:
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

        # Shutdown unified output consumer.
        await self._output_queue.put(None)
        if self._output_task:
            await self._output_task

        return total_steps

    def _on_server_event(self, event: dict[str, Any]) -> None:
        """Capture server events and queue them for unified output."""
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
            elif event_type == "read_resource" and self.on_tool_event:
                self.on_tool_event(event)
            elif event_type == "create_thread" and self.on_tool_event:
                self.on_tool_event(event)
            elif event_type == "send_message" and self.on_tool_event:
                self.on_tool_event(event)
            elif event_type == "create_thread":
                # Fallback if no on_tool_event
                pass
            elif event_type == "send_message":
                # Fallback if no on_tool_event
                pass


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
