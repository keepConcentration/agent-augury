"""Session = agent bundle + internal message server + lifecycle (§3.2).

Run loop: round-robin over agents; each turn is one step(). An agent is
finished when its completion produced neither text nor tool calls, or when
its scripted backend runs dry (IndexError).

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
        # Tool event queue for async output
        self._tool_event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._tool_event_task: asyncio.Task | None = None
        # Unified output queue for all display events (tools, steps, read_resource)
        self._output_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._output_task: asyncio.Task | None = None

    # -- assembly ------------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "Session":
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
                )
            )
        session = cls(
            server=server,
            agents=agents,
            task=cfg.get("task"),
            max_steps=int(cfg.get("max_steps", 20)),
        )
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
        """Round-robin steps until every agent finishes or max_steps is hit.

        Args:
            initial_prompt: If provided, injected as the first user message
                to the first agent.  Takes precedence over ``self.task``.

        Returns the total number of completed steps.
        """
        # Start unified output consumer task
        self._output_task = asyncio.create_task(self._output_consumer())

        if initial_prompt:
            self.agents[0].conversation.append({"role": "user", "content": initial_prompt})
        elif self.task:
            self.agents[0].conversation.append({"role": "user", "content": self.task})

        # gate-aware: inject gate state into agents each step
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
            # This removes the dependency on callback-based binding and ensures
            # gates are ready when the phase begins.
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

        finished = {a.agent_id: False for a in self.agents}
        total_steps = 0

        while total_steps < self.max_steps and not all(finished.values()):
            progressed = False
            for agent in self.agents:
                if finished[agent.agent_id]:
                    continue
                # inject current gate state before each step
                if self.gate:
                    agent.gate_open = self.gate.is_open
                    agent.gate_thread_id = self.gate.thread_id
                # v0.2: inject current protocol phase + gate state
                if self.protocol:
                    agent.current_phase = self.protocol.phase
                    _inject_protocol_gate_state(agent, self.protocol)
                try:
                    result = await agent.step()
                except Exception as exc:  # noqa: BLE001 — single agent failure
                    # Agent step failed — mark as finished so the session
                    # continues with remaining agents instead of aborting.
                    error_msg = str(exc)
                    print(
                        f"  [{agent.agent_id}] step failed: {error_msg}",
                        flush=True,
                    )
                    finished[agent.agent_id] = True
                    continue

                total_steps += 1
                progressed = True
                # Queue tool events FIRST for async display (before step summary)
                if result.tool_calls:
                    for call in result.tool_calls:
                        await self._output_queue.put({
                            "type": "tool",
                            "agent_id": agent.agent_id,
                            "tool": call.name,
                            "args": call.arguments,
                            "timestamp": __import__("time").time(),
                        })
                # Then queue step summary (same queue, preserves order)
                await self._output_queue.put({
                    "type": "step",
                    "agent_id": agent.agent_id,
                    "result": result,
                    "timestamp": __import__("time").time(),
                })
                # An agent is finished only when it produces no output AND
                # has no pending messages to process. If it sent messages,
                # it should stay alive to read responses in the next round.
                has_pending = self.server.inbox_size(agent.agent_id) > 0
                if not result.tool_calls and result.text is None and not has_pending:
                    finished[agent.agent_id] = True

            if not progressed:
                break

        # Shutdown unified output consumer
        await self._output_queue.put(None)
        if self._output_task:
            await self._output_task

        return total_steps

    def _on_server_event(self, event: dict[str, Any]) -> None:
        """Capture server events (read_resource) and queue them for unified output."""
        if event["type"] == "read_resource":
            self._output_queue.put_nowait({
                "type": "read_resource",
                "agent_id": event["agent_id"],
                "threads": event["threads"],
                "messages": event["messages"],
                "timestamp": event.get("timestamp", __import__("time").time()),
            })

    async def _output_consumer(self) -> None:
        """Consume output events from queue and emit to callbacks."""
        while True:
            event = await self._output_queue.get()
            if event is None:
                break
            if event.get("type") == "tool" and self.on_tool_event:
                self.on_tool_event(event)
            elif event.get("type") == "step" and self.on_step:
                self.on_step(event["agent_id"], event["result"])
            elif event.get("type") == "read_resource" and self.on_tool_event:
                # read_resource events are displayed as tool events
                self.on_tool_event(event)
            elif event.get("type") == "read_resource":
                # Fallback: if no on_tool_event, still handle read_resource
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
