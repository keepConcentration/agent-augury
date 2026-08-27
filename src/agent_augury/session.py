"""Session = agent bundle + internal message server + lifecycle (§3.2).

Run loop: round-robin over agents; each turn is one step(). An agent is
finished when its completion produced neither text nor tool calls, or when
its scripted backend runs dry (IndexError).

v0.2: integrates the P1~P5 collaboration protocol. When a protocol is
configured, the session drives phase transitions and injects phase context
into each agent's system prompt.
"""

from __future__ import annotations

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
        self.gate: ConsensusGate | None = None
        self.mirror: Any = None
        # v0.2: P1~P5 collaboration protocol
        self.protocol: CollaborationProtocol | None = None

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
        return session

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> int:
        """Round-robin steps until every agent finishes or max_steps is hit.

        Returns the total number of completed steps.
        """
        if self.task:
            self.agents[0].conversation.append({"role": "user", "content": self.task})

        # gate-aware: inject gate state into agents each step
        if self.gate:
            for agent in self.agents:
                agent.gate_open = self.gate.is_open
                agent.gate_thread_id = self.gate.thread_id

        # v0.2: start the collaboration protocol
        if self.protocol:
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
                except IndexError:
                    # scripted backend exhausted → treat as finished
                    finished[agent.agent_id] = True
                    continue

                total_steps += 1
                progressed = True
                if self.on_step:
                    self.on_step(agent.agent_id, result)
                if not result.tool_calls and result.text is None:
                    finished[agent.agent_id] = True

            if not progressed:
                break
        return total_steps


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
    threads is blocked. Phases without a gate (P1) leave the agent unrestricted.
    """
    gate = protocol.gate_for(protocol.phase)
    if gate is not None:
        agent.gate_open = gate.is_open
        agent.gate_thread_id = gate.thread_id
    else:
        # Phase has no gate (e.g. P1_EXPLORE) — no restriction
        agent.gate_open = True
        agent.gate_thread_id = None
