"""Session = agent bundle + internal message server + lifecycle (§3.2).

Run loop: round-robin over agents; each turn is one step(). An agent is
finished when its completion produced neither text nor tool calls, or when
its scripted backend runs dry (IndexError).
"""

from __future__ import annotations

from typing import Any, Callable

from .agent.loop import AgentLoop
from .backends_factory import build_backend
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
        return cls(
            server=server,
            agents=agents,
            task=cfg.get("task"),
            max_steps=int(cfg.get("max_steps", 20)),
        )

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> int:
        """Round-robin steps until every agent finishes or max_steps is hit.

        Returns the total number of completed steps.
        """
        if self.task:
            self.agents[0].conversation.append({"role": "user", "content": self.task})

        finished = {a.agent_id: False for a in self.agents}
        total_steps = 0

        while total_steps < self.max_steps and not all(finished.values()):
            progressed = False
            for agent in self.agents:
                if finished[agent.agent_id]:
                    continue
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
