"""Opt-in OpenAI integration and P1~P5 YAML E2E tests.

Default ``pytest tests/`` skips anything that calls external APIs.
Enable real OpenAI smoke / consensus E2E with::

    export AUGURY_RUN_OPENAI_TESTS=1
    export OPENAI_API_KEY=sk-...
    pytest tests/test_integration_openai.py -m openai -v

Offline tests in this module (YAML load + fake-backend P1~P5 E2E) always run.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_augury.backends_factory import build_backend
from agent_augury.config import load_config
from agent_augury.session import Session

OPENAI_TESTS_ENABLED = os.environ.get("AUGURY_RUN_OPENAI_TESTS") == "1"
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

requires_openai = pytest.mark.skipif(
    not (OPENAI_TESTS_ENABLED and OPENAI_API_KEY),
    reason=(
        "OpenAI integration tests are opt-in: set AUGURY_RUN_OPENAI_TESTS=1 "
        "and OPENAI_API_KEY to run (incurs API cost)."
    ),
)

ROOT = Path(__file__).resolve().parents[1]
P1_P5_YAML = ROOT / "examples" / "p1_to_p5_protocol.yaml"
CONSENSUS_OPENAI_YAML = ROOT / "examples" / "consensus_openai.yaml"


# ---------------------------------------------------------------------------
# Offline: P1~P5 protocol YAML
# ---------------------------------------------------------------------------


def test_p1_to_p5_protocol_yaml_loads():
    """YAML mirror of p1_to_p5_demo.py must validate."""
    cfg = load_config(P1_P5_YAML)
    assert cfg["mode"] == "L3"
    assert cfg["protocol"]["assembler_id"] == "agent-1"
    assert set(cfg["protocol"]["gates"]) == {
        "P2_SPLIT",
        "P3_EXECUTE",
        "P4_REVIEW",
        "P5_SUBMIT",
    }
    assert len(cfg["agents"]) == 3
    assert all(a["backend"]["type"] == "fake" for a in cfg["agents"])


@pytest.mark.asyncio
async def test_p1_to_p5_protocol_yaml_e2e_fake():
    """Full P1~P5 protocol via YAML + FakeModelBackend (no external API)."""
    cfg = load_config(P1_P5_YAML)
    session = Session.from_config(cfg)
    protocol = session.protocol
    assert protocol is not None

    await session.run()

    snap = session.server.snapshot()
    by_seq = sorted(snap["messages"], key=lambda m: m["seq"])

    assert protocol.phase == "COMPLETED", f"protocol not completed: {protocol.phase}"
    assert protocol.is_complete

    p2_gate = protocol.gate_for("P2_SPLIT")
    p3_gate = protocol.gate_for("P3_EXECUTE")
    p4_gate = protocol.gate_for("P4_REVIEW")
    p5_gate = protocol.gate_for("P5_SUBMIT")
    for gate, label in [
        (p2_gate, "P2"),
        (p3_gate, "P3"),
        (p4_gate, "P4"),
        (p5_gate, "P5"),
    ]:
        assert gate is not None and gate.is_open, f"{label} gate never opened"

    ready_msgs = [m for m in by_seq if m["content"] == "READY:"]
    propose = next(m for m in by_seq if m["content"].startswith("PROPOSE:"))
    finals = [m for m in by_seq if m["content"].startswith("FINAL:")]

    assert len(ready_msgs) == 3
    assert all(r["seq"] < propose["seq"] for r in ready_msgs)
    assert (
        p2_gate.opened_at_seq
        < p3_gate.opened_at_seq
        < p4_gate.opened_at_seq
        < p5_gate.opened_at_seq
    )
    assert len(finals) == 1
    final_approvals = [
        m
        for m in by_seq
        if m["content"].startswith("APPROVE:") and m["seq"] > finals[0]["seq"]
    ]
    assert len(final_approvals) == 3


def test_cli_p1_to_p5_protocol_yaml(capsys):
    """CLI entry runs the YAML protocol config offline."""
    from agent_augury.cli import main

    rc = main(["--config", str(P1_P5_YAML)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "[agent-1]" in out
    assert "session finished" in out


# ---------------------------------------------------------------------------
# Opt-in: OpenAI API smoke
# ---------------------------------------------------------------------------


@pytest.mark.openai
@requires_openai
@pytest.mark.asyncio
async def test_openai_api_smoke_completion():
    """Minimal chat/completions round-trip against the configured API."""
    backend = build_backend(
        {
            "type": "openai",
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
        }
    )
    completion = await backend.complete(
        [
            {"role": "system", "content": "Reply with exactly OK."},
            {"role": "user", "content": "ping"},
        ],
        tools=[],
    )
    try:
        assert completion.text
        assert "ok" in completion.text.lower()
    finally:
        await backend.aclose()


@pytest.mark.openai
@requires_openai
def test_consensus_openai_yaml_backends_build():
    """consensus_openai.yaml backends construct and accept env-based secrets."""
    cfg = load_config(CONSENSUS_OPENAI_YAML)
    backends = [build_backend(a["backend"]) for a in cfg["agents"]]
    assert len(backends) == 2
    assert all(b.model == "gpt-4o-mini" for b in backends)


@pytest.mark.openai
@requires_openai
@pytest.mark.asyncio
async def test_consensus_openai_yaml_agent_smoke():
    """One completion per consensus_openai agent — lightweight API smoke."""
    cfg = load_config(CONSENSUS_OPENAI_YAML)
    for agent in cfg["agents"]:
        backend = build_backend(agent["backend"])
        try:
            completion = await backend.complete(
                [
                    {
                        "role": "system",
                        "content": "You are a radio agent. Reply with exactly READY:",
                    },
                    {"role": "user", "content": "status?"},
                ],
                tools=[],
            )
            assert completion.text
        finally:
            await backend.aclose()
