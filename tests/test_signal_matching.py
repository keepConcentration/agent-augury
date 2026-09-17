"""Signal matching tolerates markdown / bracket decoration (signals.py).

Live repro (session d24695b5, thread-4): three agents posted a P5 draft but the
gate saw only one, because two wrapped it.

    seq 29  agent-2  '[FINAL: ...'     -> not recognised
    seq 30  agent-1  'FINAL: ...'      -> recognised
    seq 31  agent-4  '**FINAL: [X] ...' -> not recognised
"""

from __future__ import annotations

import pytest

from agent_augury.core.protocol.approval import ConsensusGate
from agent_augury.core.protocol.signals import has_signal, is_ready_message
from agent_augury.core.server import MessageServer


@pytest.mark.parametrize(
    "content",
    [
        "FINAL: 98",
        "**FINAL: [X] 98**",          # markdown bold (observed)
        "[FINAL: 98]",                # bracketed (observed)
        "  FINAL: 98",
        "# FINAL: 98",
        "- FINAL: 98",
        "`FINAL: 98`",
        "final: 98",                  # case
    ],
)
def test_decorated_signals_are_recognised(content):
    assert has_signal(content, "FINAL:")


@pytest.mark.parametrize(
    "content",
    [
        "FINALLY we agree",           # no colon
        "FINAL 98",                   # no colon
        "> FINAL: someone else wrote this",   # blockquote = citing, not claiming
        '"FINAL: quoted"',
        "I will post FINAL: later",
        "",
    ],
)
def test_lookalikes_and_quotes_are_not_signals(content):
    assert not has_signal(content, "FINAL:")


def test_ready_keeps_its_old_contract():
    assert is_ready_message("READY:")
    assert is_ready_message("  ready: done")
    assert not is_ready_message("READYFOO")
    assert not is_ready_message("READY")


@pytest.mark.asyncio
async def test_bold_draft_claims_the_gate():
    """The exact seq-31 message must now count as the draft."""
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    gate = ConsensusGate(
        server, thread_name="submission", require_proposal=True,
        entry_prefix="FINAL:",
    )
    server.subscribe(gate.on_message)
    tid = await server.create_thread("submission", participants=["a1", "a2"])
    gate.bind_to_thread(tid)

    await server.send_message(tid, author="a1", content="**FINAL: [X] 답은 X**")
    assert gate.draft_author == "a1"
    assert gate.has_proposal

    for a in ("a1", "a2"):
        await server.send_message(tid, author=a, content="*APPROVE:* 동의")
    assert gate.is_open


@pytest.mark.asyncio
async def test_gate_and_soft_block_agree_on_decoration():
    """If the two matchers disagree, an agent is blocked for a message the gate
    then ignores (or vice versa). Same helper, same verdict."""
    import json

    from agent_augury.backend.base import Completion, ModelBackend
    from agent_augury.core.agent.loop import AgentLoop

    class Quiet(ModelBackend):
        async def complete(self, messages, tools=None):
            return Completion(text=None)

    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    gate = ConsensusGate(
        server, thread_name="submission", require_proposal=True,
        entry_prefix="FINAL:",
    )
    server.subscribe(gate.on_message)
    tid = await server.create_thread("submission", participants=["a1", "a2"])
    gate.bind_to_thread(tid)
    await server.send_message(tid, author="a1", content="FINAL: 98")

    rival = AgentLoop(agent_id="a2", backend=Quiet(), server=server)
    rival.gate_open = False
    rival.gate_thread_id = tid
    rival.gate_entry_prefix = "FINAL:"
    rival.gate_draft_author_fn = lambda: gate.draft_author

    out = json.loads(
        await rival._execute_tool(
            "send_message", {"thread": tid, "content": "**FINAL: mine**"}
        )
    )
    assert out["error"] == "draft_already_posted"
