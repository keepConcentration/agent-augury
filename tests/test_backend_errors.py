"""Backend failures are retried, not mistaken for silence.

Live repro: agent-2 hit HTTP 429 four times during P5. Each failure came back
as ``Completion(text="[backend error] ...")``, which looks exactly like an
agent choosing to stay quiet, so it parked at the gate and the session froze
at ``submission 2/4`` until the human quit.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_augury.backend.base import Completion, ModelBackend
from agent_augury.backend.errors import (
    classify_http,
    jittered_backoff,
    network_error,
)
from agent_augury.core.agent.loop import AgentLoop
from agent_augury.core.server import MessageServer
from agent_augury.core.session import MAX_BACKEND_RETRIES, Session

# The exact body from the stalled session.
UPSTREAM_429 = (
    '{"status":429,"message":"The requested model is temporarily at capacity '
    "upstream. This is not your API key's rate limit — please retry shortly.\"}"
)


class FlakyBackend(ModelBackend):
    """Fails ``fail_times`` with *error*, then returns *then*."""

    def __init__(self, error, fail_times: int, then: Completion | None = None):
        self.error = error
        self.fail_times = fail_times
        self.then = then or Completion(text=None)
        self.calls = 0

    async def complete(self, messages, tools=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            return Completion(error=self.error)
        return self.then


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def test_upstream_capacity_is_not_a_throttled_key():
    err = classify_http(429, UPSTREAM_429, model="longcat")
    assert err.kind == "upstream_busy"
    assert err.retryable is True
    # nobody reads this yet; it is what model fallback will branch on
    assert err.should_fallback is True
    assert err.model == "longcat"


@pytest.mark.parametrize(
    "status,body,kind,retryable,fallback",
    [
        (429, "too many requests", "rate_limit", True, False),
        (503, "", "server_error", True, False),
        (401, "", "auth", False, False),
        (404, "", "model_not_found", False, True),
        (400, "bad json", "bad_request", False, False),
        (400, "maximum context length exceeded", "unknown", True, False),
        (418, "", "unknown", True, False),
    ],
)
def test_classification_table(status, body, kind, retryable, fallback):
    err = classify_http(status, body, model="m")
    assert (err.kind, err.retryable, err.should_fallback) == (kind, retryable, fallback)


def test_backoff_grows_and_is_capped():
    lo = [jittered_backoff(i, jitter_ratio=0.0) for i in (1, 2, 3, 4)]
    assert lo == [5.0, 10.0, 20.0, 40.0]
    assert jittered_backoff(20, jitter_ratio=0.0) == 120.0
    # jitter stays within the promised band
    for _ in range(50):
        assert 5.0 <= jittered_backoff(1) <= 7.5


# ---------------------------------------------------------------------------
# loop: the error never becomes conversation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_does_not_enter_the_conversation():
    server = MessageServer()
    server.register_agent("a1")
    backend = FlakyBackend(classify_http(429, UPSTREAM_429, model="m"), fail_times=1)
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)

    result = await agent.step()
    assert result.text is None
    assert result.error is not None and result.error.kind == "upstream_busy"
    assert not [m for m in agent.conversation if m.get("role") == "assistant"]
    assert not any("429" in str(m.get("content")) for m in agent.conversation)


@pytest.mark.asyncio
async def test_radio_survives_a_failed_call():
    """Drain already happened, so a retry must not lose teammate messages."""
    server = MessageServer()
    for a in ("a1", "a2"):
        server.register_agent(a)
    tid = await server.create_thread("t", participants=["a1", "a2"])
    await server.send_message(tid, author="a2", content="중요한 발견")

    backend = FlakyBackend(network_error("boom", model="m"), fail_times=1)
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)
    result = await agent.step()

    assert result.error is not None
    assert result.drained_count == 1
    assert any("중요한 발견" in str(m.get("content")) for m in agent.conversation)


# ---------------------------------------------------------------------------
# session: retry instead of park
# ---------------------------------------------------------------------------


async def _run(session, timeout=10.0):
    session._setup_done = True
    session._output_task = asyncio.create_task(session._output_consumer())
    try:
        return await asyncio.wait_for(session._run_impl(initial_prompt="go"), timeout)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_transient_failure_recovers(monkeypatch):
    monkeypatch.setattr(
        "agent_augury.core.session.jittered_backoff", lambda *a, **k: 0.01
    )
    server = MessageServer()
    server.register_agent("a1")
    backend = FlakyBackend(
        classify_http(429, UPSTREAM_429, model="m"),
        fail_times=2,
        then=Completion(text=None),
    )
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)
    session = Session(server=server, agents=[agent], max_steps=20)

    steps = await _run(session)
    assert backend.calls == 3          # 2 failures + 1 success
    assert steps == 1, "failed calls must not count as progress"


@pytest.mark.asyncio
async def test_retries_are_bounded(monkeypatch):
    monkeypatch.setattr(
        "agent_augury.core.session.jittered_backoff", lambda *a, **k: 0.01
    )
    server = MessageServer()
    server.register_agent("a1")
    backend = FlakyBackend(classify_http(429, UPSTREAM_429), fail_times=99)
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)
    session = Session(server=server, agents=[agent], max_steps=20)

    steps = await _run(session)
    assert backend.calls == MAX_BACKEND_RETRIES + 1
    assert steps == 0


@pytest.mark.asyncio
async def test_fatal_error_does_not_retry(monkeypatch):
    monkeypatch.setattr(
        "agent_augury.core.session.jittered_backoff", lambda *a, **k: 0.01
    )
    server = MessageServer()
    server.register_agent("a1")
    backend = FlakyBackend(classify_http(401, "", model="m"), fail_times=99)
    agent = AgentLoop(agent_id="a1", backend=backend, server=server)
    session = Session(server=server, agents=[agent], max_steps=20)

    await _run(session)
    assert backend.calls == 1, "auth failures repeat identically — do not burn retries"
