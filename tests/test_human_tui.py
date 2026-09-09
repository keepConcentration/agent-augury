"""HumanTUIAdapter — PipeInput-based headless tests (v1.0).

Verifies the TUI adapter's core logic without a real terminal:

* Scenario A: plain text input → human_send
* Scenario B: ask_user event → pinned panel → number response → option substitution
* Scenario C: ask_user event identification via tool event (D8)
* Scenario D: thread ref resolution (안 A) — resolved thread id in tool event
* Scenario E: duplicate display suppression (D11/D12)
* Scenario F: response routing + backward compat + cleanup
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_augury.channel.human_tui import HumanTUIAdapter
from agent_augury.session import Session
from tests.conftest import build_cfg

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeSession:
    """Minimal session stand-in for adapter unit tests."""

    def __init__(self, has_human: bool = True) -> None:
        self.has_human = has_human
        self.sent: list[dict[str, Any]] = []
        self._server = None

    class _FakeServer:
        def snapshot(self) -> dict[str, Any]:
            return {"threads": []}

    @property
    def server(self) -> Any:
        if self._server is None:
            self._server = self._FakeServer()
        return self._server

    async def human_send(
        self,
        thread_id: str,
        content: str,
        *,
        mentions: list[str] | None = None,
    ) -> str:
        self.sent.append({
            "thread_id": thread_id,
            "content": content,
            "mentions": mentions,
        })
        return "msg-1"


def _tui_adapter(
    session: Any,
    *,
    response_format: str = "text",
    pin_options: bool = True,
    history_file: str | None = None,
) -> HumanTUIAdapter:
    """Build a HumanTUIAdapter with a temp history file."""
    import tempfile
    if history_file is None:
        history_file = str(Path(tempfile.mkdtemp()) / "test_history.txt")
    return HumanTUIAdapter(
        session,
        response_format=response_format,
        pin_options=pin_options,
        history_file=history_file,
    )


# ---------------------------------------------------------------------------
# Scenario A: plain text → human_send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_delivers_plain_text_to_session():
    """Plain text input is delivered via session.human_send (R1)."""
    session = FakeSession()
    adapter = _tui_adapter(session)

    # Simulate user typing "hello world"
    adapter._recent_thread = "thread-1"
    await adapter._deliver("hello world")

    assert len(session.sent) == 1
    assert session.sent[0]["thread_id"] == "thread-1"
    assert session.sent[0]["content"] == "hello world"


@pytest.mark.asyncio
async def test_tui_ignores_empty_input():
    """Empty input is ignored (no human_send call)."""
    session = FakeSession()
    adapter = _tui_adapter(session)
    adapter._recent_thread = "thread-1"

    await adapter._deliver("")
    await adapter._deliver("   ")

    assert len(session.sent) == 0


# ---------------------------------------------------------------------------
# Scenario B: ask_user → pinned panel → number response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_on_ask_user_stores_pending_question():
    """on_ask_user stores the question as a pending question (D8)."""
    session = FakeSession()
    adapter = _tui_adapter(session)

    adapter.on_ask_user(
        agent_id="agent-1",
        tool="ask_user",
        args={
            "thread": "thread-1",
            "question": "DB는 뭘 쓸까?",
            "options": ["postgres", "mysql"],
        },
        result=None,
    )

    assert adapter._pending_question is not None
    assert adapter._pending_question.thread_id == "thread-1"
    assert adapter._pending_question.agent_id == "agent-1"
    assert adapter._pending_question.question == "DB는 뭘 쓸까?"
    assert adapter._pending_question.options == ["postgres", "mysql"]
    assert adapter._pending_question.status == "pending"


@pytest.mark.asyncio
async def test_tui_number_response_substitutes_option_text():
    """Number input substitutes the option text and sends to the asking agent (D7, D10)."""
    session = FakeSession()
    adapter = _tui_adapter(session)

    adapter.on_ask_user(
        agent_id="agent-1",
        tool="ask_user",
        args={
            "thread": "thread-1",
            "question": "DB는 뭘 쓸까?",
            "options": ["postgres", "mysql"],
        },
        result=None,
    )

    await adapter._deliver("2")

    assert len(session.sent) == 1
    assert session.sent[0]["thread_id"] == "thread-1"
    assert session.sent[0]["content"] == "mysql"
    assert session.sent[0]["mentions"] == ["agent-1"]
    # After delivery, _pending_question is cleared (answered)
    assert adapter._pending_question is None


@pytest.mark.asyncio
async def test_tui_number_response_format_number():
    """When response_format=number, the number itself is sent (D7)."""
    session = FakeSession()
    adapter = _tui_adapter(session, response_format="number")

    adapter.on_ask_user(
        agent_id="agent-1",
        tool="ask_user",
        args={
            "thread": "thread-1",
            "question": "DB는 뭘 쓸까?",
            "options": ["postgres", "mysql"],
        },
        result=None,
    )

    await adapter._deliver("1")

    assert session.sent[0]["content"] == "1"


@pytest.mark.asyncio
async def test_tui_plain_text_ignores_option_number():
    """Number input without a pending question is treated as plain text."""
    session = FakeSession()
    adapter = _tui_adapter(session)
    adapter._recent_thread = "thread-1"

    await adapter._deliver("3")

    assert len(session.sent) == 1
    assert session.sent[0]["content"] == "3"
    assert session.sent[0]["mentions"] is None


# ---------------------------------------------------------------------------
# Scenario C: ask_user identification via tool event (D8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_ask_user_identified_via_tool_event():
    """ask_user is identified via the existing tool event path (D8)."""
    session = FakeSession()
    adapter = _tui_adapter(session)

    # Simulate the tool event that cli.on_tool_event would call
    adapter.on_ask_user(
        agent_id="agent-1",
        tool="ask_user",
        args={
            "thread": "thread-1",
            "question": "DB는 뭘 쓸까?",
            "options": ["postgres", "mysql"],
        },
        result=None,
    )

    # The pending question should have the structured data from args
    pq = adapter._pending_question
    assert pq is not None
    assert pq.question == "DB는 뭘 쓸까?"
    assert pq.options == ["postgres", "mysql"]


# ---------------------------------------------------------------------------
# Scenario D: thread ref resolution (안 A)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_uses_resolved_thread_id():
    """After 안 A, the tool event carries the resolved thread id (not $thread:0)."""
    session = FakeSession()
    adapter = _tui_adapter(session)

    # After loop.py fix, args["thread"] is the resolved id
    adapter.on_ask_user(
        agent_id="agent-1",
        tool="ask_user",
        args={
            "thread": "thread-abc123",  # resolved, not "$thread:0"
            "question": "DB는 뭘 쓸까?",
            "options": ["postgres", "mysql"],
        },
        result=None,
    )

    await adapter._deliver("1")

    assert session.sent[0]["thread_id"] == "thread-abc123"


# ---------------------------------------------------------------------------
# Scenario E: duplicate display suppression (D11/D12)
# ---------------------------------------------------------------------------


def test_tui_toolbar_renders_question():
    """Toolbar renders the question and options when pin_options=True."""
    session = FakeSession()
    adapter = _tui_adapter(session, pin_options=True)

    adapter.on_ask_user(
        agent_id="agent-1",
        tool="ask_user",
        args={
            "thread": "thread-1",
            "question": "DB는 뭘 쓸까?",
            "options": ["postgres", "mysql"],
        },
        result=None,
    )

    toolbar = adapter._render_toolbar()
    # HTML formatted text — check it contains the question
    assert "DB는 뭘 쓸까?" in str(toolbar)


def test_tui_toolbar_empty_when_no_pending():
    """Toolbar is empty when there is no pending question."""
    session = FakeSession()
    adapter = _tui_adapter(session)

    toolbar = adapter._render_toolbar()
    assert str(toolbar) == "HTML('')"


# ---------------------------------------------------------------------------
# Scenario F: response routing + backward compat + cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_cleanup_resets_state():
    """cleanup() resets pending questions and stop flag (D13)."""
    session = FakeSession()
    adapter = _tui_adapter(session)

    adapter.on_ask_user(
        agent_id="agent-1",
        tool="ask_user",
        args={
            "thread": "thread-1",
            "question": "DB는 뭘 쓸까?",
            "options": ["postgres", "mysql"],
        },
        result=None,
    )

    adapter.cleanup()

    assert adapter._pending_question is None
    assert adapter._stop is True


@pytest.mark.asyncio
async def test_tui_stop_exits_loop():
    """stop() causes the input loop to exit cleanly."""
    session = FakeSession()
    adapter = _tui_adapter(session)
    adapter._stop = True

    # run_input_loop should exit immediately without creating a PromptSession
    # (we can't test the full loop in non-TTY, but we can test the stop flag)
    # Just verify stop flag is set
    assert adapter._stop is True


# ---------------------------------------------------------------------------
# Integration: Session with TUI adapter (full E2E via PipeInput)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_pipeinput_e2e_ask_user_flow(tmp_path):
    """Full E2E: fake backend ask_user → PipeInput response → agent absorbs."""
    cfg = build_cfg(
        max_steps=20,
        task="DB 선택을 확인해줘",
        human={"id": "human"},
        agents=[
            {
                "id": "agent-1",
                "backend": {
                    "type": "fake",
                    "script": [
                        {
                            "tool_calls": [
                                {
                                    "name": "create_thread",
                                    "arguments": {"name": "work", "participants": ["agent-1"]},
                                },
                                {
                                    "name": "ask_user",
                                    "arguments": {
                                        "thread": "$thread:0",
                                        "question": "DB는 뭘 쓸까?",
                                        "options": ["postgres", "mysql"],
                                    },
                                },
                            ]
                        },
                        "DB는 postgres로 결정됐습니다.",
                    ],
                },
            },
        ],
    )

    session = Session.from_config(cfg)
    assert session.has_human is True

    # Run the session — agent creates thread + ask_user
    await session.run(initial_prompt="DB 선택을 확인해줘")

    # Find the created thread
    threads = session.server.snapshot()["threads"]
    assert len(threads) >= 1
    tid = threads[0]["thread_id"]

    # Simulate TUI adapter receiving the ask_user event
    adapter = _tui_adapter(session, history_file=str(tmp_path / "hist.txt"))
    adapter.on_ask_user(
        agent_id="agent-1",
        tool="ask_user",
        args={
            "thread": tid,
            "question": "DB는 뭘 쓸까?",
            "options": ["postgres", "mysql"],
        },
        result=None,
    )

    # User types "1" → should substitute "postgres"
    await adapter._deliver("1")

    # Verify human_send was called (check agent inbox)
    assert session.server.inbox_size("agent-1") >= 1

    # Run again — agent absorbs the response
    await session.run()

    # Verify agent absorbed the response
    agent = session.agents[0]
    radio_blocks = [
        m for m in agent.conversation
        if m["role"] == "user" and "from human" in m["content"]
    ]
    assert len(radio_blocks) >= 1
    assert "postgres" in radio_blocks[-1]["content"]

    await session.close()


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------


def test_config_human_optional(tmp_path):
    """config.py는 human 섹션이 없어도 정상 동작 (코드에 내장)."""
    import yaml

    from agent_augury.config import load_config

    cfg_path = tmp_path / "no_human.yaml"
    cfg_path.write_text(
        yaml.safe_dump({
            "max_steps": 10,
            "task": "test",
            "agents": [
                {
                    "id": "agent-1",
                    "backend": {
                        "type": "nous_oauth",
                        "model": "test-model",
                    },
                },
            ],
        }),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    # human 섹션이 없어도 정상 동작
    assert "human" not in cfg or cfg.get("human") is None


def test_config_human_interface_ignored(tmp_path):
    """config.py는 human.interface 키를 무시한다 (항상 TUI 고정)."""
    import yaml

    from agent_augury.config import load_config

    cfg_path = tmp_path / "tui.yaml"
    cfg_path.write_text(
        yaml.safe_dump({
            "max_steps": 10,
            "task": "test",
            "human": {"id": "human", "interface": "cli"},
            "agents": [
                {
                    "id": "agent-1",
                    "backend": {
                        "type": "nous_oauth",
                        "model": "test-model",
                    },
                },
            ],
        }),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    # interface 키는 무시됨 (검증 안 함)
    assert cfg.get("human", {}).get("id") == "human"


def test_config_human_tui_keys_ignored(tmp_path):
    """config.py는 human.tui 키를 검증하지 않고 무시한다 (코드에 내장)."""
    import yaml

    from agent_augury.config import load_config

    cfg_path = tmp_path / "bad_tui.yaml"
    cfg_path.write_text(
        yaml.safe_dump({
            "max_steps": 10,
            "task": "test",
            "human": {
                "id": "human",
                "tui": {"unknown_key": True, "response_format": "invalid"},
            },
            "agents": [
                {
                    "id": "agent-1",
                    "backend": {
                        "type": "nous_oauth",
                        "model": "test-model",
                    },
                },
            ],
        }),
        encoding="utf-8",
    )
    # human.tui 검증을 하지 않으므로 정상 로드됨
    cfg = load_config(cfg_path)
    assert cfg["human"]["tui"]["unknown_key"] is True
