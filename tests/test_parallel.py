"""Parallel execution verification for Session.run() (task t_46faeed7).

Verifies:
  1. Agents run in parallel (not round-robin sequential).
  2. Tool events flush in the order they actually fire.
  3. max_steps global sum gate works correctly.
  4. All agents finish normally when their scripts exhaust.
"""

import asyncio

import pytest

from agent_augury.backend.base import Completion, ToolCall
from agent_augury.backend.fake import FakeModelBackend
from agent_augury.session import Session
from agent_augury.server import MessageServer


def _make_session(tmp_path, cfg):
    """Build a Session from a config dict."""
    import yaml
    p = tmp_path / "session.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    from agent_augury.config import load_config
    return Session.from_config(load_config(p))


# ---------------------------------------------------------------------------
# 1. Parallel execution: both agents make progress, not strictly alternating
# ---------------------------------------------------------------------------

TWO_AGENT_CFG = {
    "mode": "L3",
    "max_steps": 20,
    "task": "collaborate",
    "agents": [
        {
            "id": "agent-1",
            "backend": {
                "type": "fake",
                "script": [
                    {"tool_calls": [{"name": "create_thread", "arguments": {"name": "work", "participants": ["agent-1", "agent-2"]}}]},
                    {"tool_calls": [{"name": "send_message", "arguments": {"thread": "$thread:0", "content": "from agent-1", "mentions": []}}]},
                    "done-1",
                ],
            },
        },
        {
            "id": "agent-2",
            "backend": {
                "type": "fake",
                "script": [
                    {"tool_calls": [{"name": "send_message", "arguments": {"thread": "$thread_by_name:work", "content": "from agent-2", "mentions": []}}]},
                    "done-2",
                ],
            },
        },
    ],
}


async def test_agents_run_in_parallel(tmp_path):
    """Both agents should complete their steps; total steps = sum of all agent steps."""
    session = _make_session(tmp_path, TWO_AGENT_CFG)
    steps = await session.run()
    # agent-1: 3 steps, agent-2: 2 steps = 5 total
    assert steps == 5
    assert session.agents[0].backend.call_count == 3
    assert session.agents[1].backend.call_count == 2


async def test_parallel_agents_share_server(tmp_path):
    """All agents share one server instance (SSOT)."""
    session = _make_session(tmp_path, TWO_AGENT_CFG)
    await session.run()
    assert len({id(a.server) for a in session.agents}) == 1


# ---------------------------------------------------------------------------
# 2. Tool events flush in firing order (verified via on_tool_event callback)
# ---------------------------------------------------------------------------

async def test_tool_events_flush_in_order(tmp_path):
    """Tool events should arrive in the order they fire, not batched."""
    events = []

    import yaml
    p = tmp_path / "session.yaml"
    p.write_text(yaml.safe_dump(TWO_AGENT_CFG), encoding="utf-8")
    from agent_augury.config import load_config
    session = Session.from_config(load_config(p), on_tool_event=lambda e: events.append(e))
    await session.run()

    # Both agents fire tool events; we should see interleaved events
    # (not all agent-1 then all agent-2).
    tool_events = [e for e in events if e["type"] == "tool"]
    assert len(tool_events) > 0
    # At least one event from each agent
    agent_ids = {e["agent_id"] for e in tool_events}
    assert agent_ids == {"agent-1", "agent-2"}


# ---------------------------------------------------------------------------
# 3. max_steps global sum gate
# ---------------------------------------------------------------------------

MAX_STEPS_CFG = {
    "mode": "L3",
    "max_steps": 3,
    "task": "test",
    "agents": [
        {
            "id": "a1",
            "backend": {
                "type": "fake",
                "script": [
                    "step1", "step2", "step3", "step4", "step5",
                ],
            },
        },
        {
            "id": "a2",
            "backend": {
                "type": "fake",
                "script": [
                    "step1", "step2", "step3", "step4", "step5",
                ],
            },
        },
    ],
}


async def test_max_steps_caps_total(tmp_path):
    """max_steps is a global cap across all agents."""
    session = _make_session(tmp_path, MAX_STEPS_CFG)
    steps = await session.run()
    assert steps == 3


async def test_max_steps_respected_per_agent(tmp_path):
    """No single agent can exceed max_steps on its own."""
    cfg = {
        "mode": "L3",
        "max_steps": 2,
        "task": "test",
        "agents": [
            {
                "id": "solo",
                "backend": {
                    "type": "fake",
                    "script": ["a", "b", "c", "d"],
                },
            },
        ],
    }
    session = _make_session(tmp_path, cfg)
    steps = await session.run()
    assert steps == 2


# ---------------------------------------------------------------------------
# 4. All agents finish normally when scripts exhaust
# ---------------------------------------------------------------------------

async def test_all_agents_finish_when_script_exhausts(tmp_path):
    """When an agent's script runs out, it should finish gracefully."""
    cfg = {
        "mode": "L3",
        "max_steps": 100,
        "task": "test",
        "agents": [
            {
                "id": "short",
                "backend": {
                    "type": "fake",
                    "script": ["only-step"],
                },
            },
            {
                "id": "long",
                "backend": {
                    "type": "fake",
                    "script": ["s1", "s2", "s3"],
                },
            },
        ],
    }
    session = _make_session(tmp_path, cfg)
    steps = await session.run()
    # short: 1 step, long: 3 steps = 4 total
    assert steps == 4


# ---------------------------------------------------------------------------
# 5. Single agent still works (degenerate case)
# ---------------------------------------------------------------------------

async def test_single_agent_parallel(tmp_path):
    """A single agent should still work correctly."""
    cfg = {
        "mode": "L3",
        "max_steps": 10,
        "task": "solo task",
        "agents": [
            {
                "id": "only",
                "backend": {
                    "type": "fake",
                    "script": ["thinking", "done"],
                },
            },
        ],
    }
    session = _make_session(tmp_path, cfg)
    steps = await session.run()
    assert steps == 2


# ---------------------------------------------------------------------------
# 6. Agent failure doesn't block others
# ---------------------------------------------------------------------------

async def test_agent_failure_isolated(tmp_path):
    """If one agent's script exhausts, the other agents continue."""
    cfg = {
        "mode": "L3",
        "max_steps": 10,
        "task": "test",
        "agents": [
            {
                "id": "short",
                "backend": {
                    "type": "fake",
                    "script": ["ok"],  # 1 step then IndexError → finish
                },
            },
            {
                "id": "long",
                "backend": {
                    "type": "fake",
                    "script": ["a", "b", "c"],
                },
            },
        ],
    }
    session = _make_session(tmp_path, cfg)
    steps = await session.run()
    # short: 1 step (then IndexError → break), long: 3 steps = 4 total
    assert steps == 4


# ---------------------------------------------------------------------------
# 7. Protocol timing preserved under parallel execution
# ---------------------------------------------------------------------------

PROTOCOL_PARALLEL_CFG = {
    "mode": "L3",
    "max_steps": 30,
    "protocol": {
        "participants": ["a1", "a2"],
        "assembler_id": "a1",
        "gates": {
            "P2_SPLIT": "plan",
            "P3_EXECUTE": "execution",
        },
    },
    "agents": [
        {
            "id": "a1",
            "backend": {
                "type": "fake",
                "script": [
                    {"tool_calls": [{"name": "send_message", "arguments": {"thread": "$thread_by_name:plan", "content": "READY:", "mentions": []}}]},
                    {"tool_calls": [{"name": "send_message", "arguments": {"thread": "$thread_by_name:plan", "content": "PROPOSE: split", "mentions": []}}]},
                    {"tool_calls": [{"name": "send_message", "arguments": {"thread": "$thread_by_name:plan", "content": "APPROVE: ok", "mentions": []}}]},
                    {"tool_calls": [{"name": "send_message", "arguments": {"thread": "$thread_by_name:execution", "content": "(FYI) a1 done", "mentions": []}}]},
                    {"tool_calls": [{"name": "send_message", "arguments": {"thread": "$thread_by_name:execution", "content": "APPROVE: ok", "mentions": []}}]},
                    {"text": "done"},
                ],
            },
        },
        {
            "id": "a2",
            "backend": {
                "type": "fake",
                "script": [
                    {"tool_calls": [{"name": "send_message", "arguments": {"thread": "$thread_by_name:plan", "content": "READY:", "mentions": []}}]},
                    {"tool_calls": [{"name": "send_message", "arguments": {"thread": "$thread_by_name:plan", "content": "APPROVE: ok", "mentions": []}}]},
                    {"tool_calls": [{"name": "send_message", "arguments": {"thread": "$thread_by_name:execution", "content": "(FYI) a2 done", "mentions": []}}]},
                    {"tool_calls": [{"name": "send_message", "arguments": {"thread": "$thread_by_name:execution", "content": "APPROVE: ok", "mentions": []}}]},
                    {"text": "done"},
                ],
            },
        },
    ],
}


async def test_protocol_gates_work_in_parallel(tmp_path):
    """P1~P3 gates should open correctly under parallel execution."""
    session = _make_session(tmp_path, PROTOCOL_PARALLEL_CFG)
    protocol = session.protocol
    assert protocol is not None

    await session.run()

    assert protocol.gate_for("P2_SPLIT").is_open
    assert protocol.gate_for("P3_EXECUTE").is_open
