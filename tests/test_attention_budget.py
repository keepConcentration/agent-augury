"""V1e — attention budget 통합 테스트.

AGENT_RELEVANCE_BUDGET_DESIGN.md §9 테스트 계획.
8개 시나리오 + 회귀 테스트.
"""

from __future__ import annotations

from agent_augury.backend.base import Completion
from agent_augury.core.agent.loop import AgentLoop
from agent_augury.core.attention import AttentionScores, BudgetDecision, RelevancePolicy
from agent_augury.core.server import MessageServer

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class ScriptedBackend:
    """test_agent_loop.py 패턴 — pre-scripted completions + 호출 기록."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    async def complete(self, messages, tools):
        self.calls.append(
            {"messages": [dict(m) for m in messages], "tool_names": [t["name"] for t in tools]}
        )
        return self.script.pop(0)


def _make_agent(server, agent_id, script, *, attention_policy=None, attention_config=None):
    return AgentLoop(
        agent_id=agent_id,
        server=server,
        backend=ScriptedBackend(script),
        system_prompt="You are a radio agent.",
        attention_policy=attention_policy,
        attention_config=attention_config,
    )


def _zero_features_policy():
    """모든 피처 가중치 0 → r=0 → ignore 구간 유도."""
    return RelevancePolicy(
        features={"mention_boost": 0.0, "thread_participant": 0.0, "recent_interact": 0.0},
        tiers={"ignore": 0.15, "skim": 0.45, "engage": 0.80},
    )


def _mention_only_policy():
    """mention boost만 1.0, 피처 기본값 그대로."""
    return RelevancePolicy(
        features={"mention_boost": 1.0},  # 나머지는 기본값
    )


# ---------------------------------------------------------------------------
# 1. enabled: false → 기존 동작과 동일 (회귀)
# ---------------------------------------------------------------------------


async def test_attention_disabled_unchanged_behavior():
    """attention_policy=None → 기존 step() 동작과 100% 동일."""
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    await server.send_message(tid, author="agent-1", content="FYI: hello", mentions=["agent-2"])

    agent = _make_agent(server, "agent-2", [Completion(text="ok")])
    result = await agent.step()

    assert not result.skipped
    assert result.drained_count == 1
    assert len(agent.backend.calls) == 1
    # [radio] block present (기존 format_radio_block)
    user_msgs = [m for m in agent.backend.calls[0]["messages"] if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert "[radio]" in user_msgs[0]["content"]


# ---------------------------------------------------------------------------
# 2. T0 ignore → skipped=True, complete 호출 없음, drain은 수행
# ---------------------------------------------------------------------------


async def test_t0_ignore_skips_complete_but_drains():
    """T0 ignore: drain 수행, StepResult.skipped=True, backend.complete 미호출."""
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    await server.send_message(tid, author="agent-1", content="low priority broadcast", mentions=[])

    policy = _zero_features_policy()
    agent = _make_agent(
        server, "agent-2", [Completion(text="should not be used")],
        attention_policy=policy, attention_config={},
    )
    result = await agent.step()

    assert result.skipped is True
    assert result.drained_count == 1
    assert result.text is None
    # backend.complete가 호출되지 않았음
    assert len(agent.backend.calls) == 0
    # inbox drained (불변식)
    assert server.inbox_size("agent-2") == 0


# ---------------------------------------------------------------------------
# 3. T1 skim → format_radio_block_skim 사용, complete 호출
# ---------------------------------------------------------------------------


async def test_t1_skim_uses_skim_format():
    """skim tier: format_radio_block_skim → digest + 최신 1메시지, complete 호출."""
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    await server.send_message(tid, author="agent-1", content="some broadcast", mentions=[])

    # mention_boost=0, 나머지 기본 → no-mention r ≈ 0.25+0.3 = 0.55
    # tiers 기본값 → 0.55는 engage. 맞춰서 skim 구간으로 조정
    policy = RelevancePolicy(
        features={"mention_boost": 0.0, "thread_participant": 0.1, "recent_interact": 0.0},
        tiers={"ignore": 0.05, "skim": 0.25, "engage": 0.80},
        server=server,  # F2 needs server ref
    )
    agent = _make_agent(
        server, "agent-2", [Completion(text="skimmed")],
        attention_policy=policy, attention_config={},
    )
    result = await agent.step()

    assert not result.skipped
    assert result.drained_count == 1
    assert len(agent.backend.calls) == 1
    user_msgs = [m for m in agent.backend.calls[0]["messages"] if m["role"] == "user"]
    content = user_msgs[0]["content"]
    assert "[radio — skim]" in content
    assert "(skim:" in content
    # 최신 메시지 일부 포함
    assert "some broadcast" in content


# ---------------------------------------------------------------------------
# 4. T2/T3 engage → format_radio_block (기존 동작)
# ---------------------------------------------------------------------------


async def test_t2_engage_uses_full_radio():
    """engage tier: format_radio_block 전체 사용, complete 호출."""
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    await server.send_message(tid, author="agent-1", content="important", mentions=["agent-2"])

    policy = _mention_only_policy()
    agent = _make_agent(
        server, "agent-2", [Completion(text="got it")],
        attention_policy=policy, attention_config={},
    )
    result = await agent.step()

    assert not result.skipped
    assert result.drained_count == 1
    assert len(agent.backend.calls) == 1
    user_msgs = [m for m in agent.backend.calls[0]["messages"] if m["role"] == "user"]
    assert "[radio]" in user_msgs[0]["content"]
    assert "from agent-1: important" in user_msgs[0]["content"]


# ---------------------------------------------------------------------------
# 5. phase_floor / agent_floor → tier 상승
# ---------------------------------------------------------------------------


async def test_phase_floor_lifts_tier_from_ignore_to_skim():
    """phase_floor=0.3 → r=0 이어도 floor가 올려서 skim 이상."""
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    await server.send_message(tid, author="agent-1", content="broadcast", mentions=[])

    policy = _zero_features_policy()
    agent = _make_agent(
        server, "agent-2", [Completion(text="floor")],
        attention_policy=policy,
        attention_config={"floor": 0.0},  # agent_floor = 0
    )
    # phase_floor 직접 주입 (session이 하는 역할)
    agent.phase_floor = 0.3
    result = await agent.step()

    assert not result.skipped
    assert len(agent.backend.calls) == 1


async def test_agent_floor_lifts_tier():
    """agent_floor=0.6 → r=0 이어도 engage 이상."""
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    await server.send_message(tid, author="agent-1", content="broadcast", mentions=[])

    policy = _zero_features_policy()
    # _zero_features_policy returns a policy factory — we need one with server
    policy = RelevancePolicy(
        features={"mention_boost": 0.0, "thread_participant": 0.0, "recent_interact": 0.0},
        tiers={"ignore": 0.15, "skim": 0.45, "engage": 0.80},
        server=server,
    )
    agent = _make_agent(
        server, "agent-2", [Completion(text="agent floor")],
        attention_policy=policy,
        attention_config={"floors": {"default": 0.6}},  # agent_floor → engage
    )
    result = await agent.step()

    assert not result.skipped
    assert len(agent.backend.calls) == 1
    user_msgs = [m for m in agent.backend.calls[0]["messages"] if m["role"] == "user"]
    assert "[radio]" in user_msgs[0]["content"]  # full radio, not skim


# ---------------------------------------------------------------------------
# 6. T0 drain 불변식: skip 되어도 inbox size == 0, park↔wake 스핀 없음
# ---------------------------------------------------------------------------


async def test_t0_drain_invariance_inbox_empty_after_skip():
    """T0 ignore 후 inbox는 비어있어야 한다 (drain 안 하면 park 스핀)."""
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    # 여러 메시지를 쌓아도 전부 드레인
    for i in range(5):
        await server.send_message(tid, author="agent-1", content=f"msg {i}", mentions=[])

    policy = _zero_features_policy()
    agent = _make_agent(
        server, "agent-2", [Completion(text="unused")],
        attention_policy=policy, attention_config={},
    )
    result = await agent.step()

    assert result.skipped is True
    assert result.drained_count == 5
    assert server.inbox_size("agent-2") == 0
    assert len(agent.backend.calls) == 0


# ---------------------------------------------------------------------------
# 7. 배치 max: 멘션 1개 + broadcast 다수 → tier ≥ engage
# ---------------------------------------------------------------------------


async def test_batch_max_mention_wins():
    """멘션 포함 메시지 하나가 전체 배치 tier를 engage 이상으로 끌어올린다."""
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2", "agent-3"])
    # broadcast 2개
    await server.send_message(tid, author="agent-1", content="noise 1", mentions=[])
    await server.send_message(tid, author="agent-1", content="noise 2", mentions=[])
    # 직접 멘션 1개
    await server.send_message(tid, author="agent-3", content="PROPOSE: plan", mentions=["agent-2"])

    policy = _mention_only_policy()
    agent = _make_agent(
        server, "agent-2", [Completion(text="ack")],
        attention_policy=policy, attention_config={},
    )
    result = await agent.step()

    assert not result.skipped
    assert result.drained_count == 3
    assert len(agent.backend.calls) == 1
    # full radio (T2+)
    user_msgs = [m for m in agent.backend.calls[0]["messages"] if m["role"] == "user"]
    assert "[radio]" in user_msgs[0]["content"]
    # 세 메시지 모두 포함 (한 turn에 merge)
    assert "noise 1" in user_msgs[0]["content"]
    assert "noise 2" in user_msgs[0]["content"]
    assert "PROPOSE: plan" in user_msgs[0]["content"]


# ---------------------------------------------------------------------------
# 8. URGENT boost → URGENT prefix 감지 시 r=1.0 (G4)
# ---------------------------------------------------------------------------


async def test_urgent_prefix_forces_intervene():
    """URGENT로 시작하는 메시지는 mention 없어도 intervene (r=1.0)."""
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    await server.send_message(tid, author="agent-1", content="URGENT: crash detected", mentions=[])

    policy = _zero_features_policy()  # 기본적으로 모든 메시지 r=0
    agent = _make_agent(
        server, "agent-2", [Completion(text="on my way")],
        attention_policy=policy, attention_config={},
    )
    result = await agent.step()

    assert not result.skipped
    assert len(agent.backend.calls) == 1
    # full radio — URGENT가 skim을 건너뜀
    user_msgs = [m for m in agent.backend.calls[0]["messages"] if m["role"] == "user"]
    assert "[radio]" in user_msgs[0]["content"]
    assert "URGENT" in user_msgs[0]["content"]


# ---------------------------------------------------------------------------
# 9. 빈 inbox — attention policy 있어도 정상 동작
# ---------------------------------------------------------------------------


async def test_empty_inbox_with_policy_still_runs_model():
    """drained 메시지 없으면 attention 로직 건너뛰고 정상 complete."""
    server = MessageServer()
    await server.create_thread("t", participants=["agent-1", "agent-2"])

    policy = _zero_features_policy()
    agent = _make_agent(
        server, "agent-2", [Completion(text="idle")],
        attention_policy=policy, attention_config={},
    )
    result = await agent.step()

    assert not result.skipped
    assert result.drained_count == 0
    assert result.text == "idle"
    assert len(agent.backend.calls) == 1


# ---------------------------------------------------------------------------
# 10. t0_digest — T0 ignore 시 [digest] 1줄 주입 옵션
# ---------------------------------------------------------------------------


async def test_t0_digest_injects_summary_line():
    """t0_digest=true → T0 skip 시 conversation에 [digest] 한 줄 추가."""
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    await server.send_message(tid, author="agent-1", content="broadcast", mentions=[])

    policy = _zero_features_policy()
    agent = _make_agent(
        server, "agent-2", [Completion(text="unused")],
        attention_policy=policy,
        attention_config={"context": {"t0_digest": True}},
    )
    result = await agent.step()

    assert result.skipped is True
    assert len(agent.backend.calls) == 0
    # conversation에 [digest] 줄이 추가되었는지
    user_msgs = [m for m in agent.conversation if m["role"] == "user"]
    digest_msg = next((m for m in user_msgs if "[digest]" in m["content"]), None)
    assert digest_msg is not None
    assert "skipped" in digest_msg["content"] or "low relevance" in digest_msg["content"]


# ---------------------------------------------------------------------------
# 11. from_config 경로 검증
# ---------------------------------------------------------------------------


def test_relevance_policy_from_config_enabled():
    p = RelevancePolicy.from_config({"enabled": True})
    assert p is not None


def test_relevance_policy_from_config_none():
    p = RelevancePolicy.from_config(None)
    assert p is None


def test_relevance_policy_from_config_disabled():
    p = RelevancePolicy.from_config({"enabled": False})
    assert p is None


def test_relevance_policy_from_config_custom_tiers():
    p = RelevancePolicy.from_config({
        "enabled": True,
        "tiers": {"ignore": 0.1, "skim": 0.3, "engage": 0.6},
    })
    assert p is not None
    # default tier overrides
    assert p._tier_ignore == 0.1
    assert p._tier_engage == 0.6


def test_relevance_policy_from_config_custom_features():
    p = RelevancePolicy.from_config({
        "enabled": True,
        "features": {"mention_boost": 0.8},
    })
    assert p is not None
    assert p._mention_boost == 0.8
    # 나머지는 기본값
    assert p._thread_participant == 0.25


# ---------------------------------------------------------------------------
# 12. BudgetDecision / AttentionScores dataclass
# ---------------------------------------------------------------------------


def test_budget_decision_defaults():
    d = BudgetDecision(tier="engage", r=0.6, run_llm=True)
    assert d.tier == "engage"
    assert d.r == 0.6
    assert d.run_llm is True
    assert d.max_tokens is None
    assert d.context_max_chars is None
    assert d.tools_allowed is True


def test_attention_scores_defaults():
    s = AttentionScores()
    assert s.by_agent == {}
    assert s.by_message == {}


# ---------------------------------------------------------------------------
# 13. format_radio_block_skim 단위 검증
# ---------------------------------------------------------------------------


def test_format_radio_block_skim():
    from agent_augury.core.agent.loop import format_radio_block_skim

    msgs = [
        {"author": "agent-1", "content": "old message"},
        {"author": "agent-2", "content": "latest update here"},
    ]
    result = format_radio_block_skim(msgs, max_chars=400)
    assert "[radio — skim]" in result
    assert "(skim:" in result
    assert "2 message(s)" in result
    assert "latest update here" in result
    assert "old message" not in result  # 최신 메시지만 포함


def test_format_radio_block_skim_truncates_long_content():
    from agent_augury.core.agent.loop import format_radio_block_skim

    msgs = [
        {"author": "agent-1", "content": "abc" * 200},  # 600 chars
    ]
    result = format_radio_block_skim(msgs, max_chars=50)
    assert "…" in result
    assert len(result) <= 50 + len("[radio — skim]\n(skim: 1 messages from agent-1)\nfrom agent-1: ") + 3


# ---------------------------------------------------------------------------
# 14. 회귀: attention_policy=None → StepResult.skipped=False
# ---------------------------------------------------------------------------


async def test_null_policy_no_skipped_flag():
    """attention_policy=None → 모든 step에서 skipped=False (기존 동작)."""
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    await server.send_message(tid, author="agent-1", content="any", mentions=["agent-2"])

    agent = _make_agent(server, "agent-2", [Completion(text="ok")])
    result = await agent.step()

    assert result.skipped is False  # getattr fallback은 False지만 명시적으로 확인
    assert result.text == "ok"


# ---------------------------------------------------------------------------
# 15. human 메시지 → 항상 T2+ (리뷰 P0-2)
# ---------------------------------------------------------------------------


async def test_human_message_always_engage():
    """author=human 메시지는 휴리스틱과 무관하게 engage+ (T0 금지)."""
    server = MessageServer()
    server.register_human("human")
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    await server.human_send(tid, author="human", content="please look at this", mentions=["agent-2"])

    policy = _zero_features_policy()  # 모든 agent 메시지 r=0
    agent = _make_agent(
        server, "agent-2", [Completion(text="ack human")],
        attention_policy=policy, attention_config={},
    )
    result = await agent.step()

    assert not result.skipped
    assert len(agent.backend.calls) == 1
    user_msgs = [m for m in agent.backend.calls[0]["messages"] if m["role"] == "user"]
    assert "[radio]" in user_msgs[0]["content"]
    assert "from human:" in user_msgs[0]["content"]


# ---------------------------------------------------------------------------
# 16. phase_floor: near_gate / p1_ready_pending
# ---------------------------------------------------------------------------


def test_compute_phase_floor_none_protocol():
    from agent_augury.core.session import _compute_phase_floor

    agent = type("A", (), {"_attention_policy": object(), "_attention_config": {
        "floors": {"near_gate": 0.5, "p1_ready_pending": 0.15, "default": 0.0},
    }, "agent_id": "a1"})()
    assert _compute_phase_floor(agent, None) == 0.0


def test_compute_phase_floor_p1_ready_pending():
    from agent_augury.core.protocol.collaboration import CollaborationProtocol
    from agent_augury.core.protocol.phases import P1_EXPLORE
    from agent_augury.core.session import _compute_phase_floor

    server = MessageServer()
    server.register_agent("a1")
    server.register_agent("a2")
    protocol = CollaborationProtocol(server, participants=["a1", "a2"])
    protocol.start = lambda: None  # type: ignore[method-assign]
    protocol.phase_manager._phase = P1_EXPLORE

    agent = type("A", (), {
        "agent_id": "a1",
        "_attention_policy": object(),
        "_attention_config": {
            "floors": {"near_gate": 0.5, "p1_ready_pending": 0.15, "default": 0.0},
        },
    })()
    assert _compute_phase_floor(agent, protocol) == 0.15


async def test_compute_phase_floor_near_gate_requires_approval():
    """near_gate floor only when closed gate has ≥1 approval (not all closed)."""
    from agent_augury.core.protocol.approval import ConsensusGate
    from agent_augury.core.protocol.collaboration import CollaborationProtocol
    from agent_augury.core.protocol.phases import P2_SPLIT
    from agent_augury.core.session import _compute_phase_floor

    server = MessageServer()
    server.register_agent("a1")
    server.register_agent("a2")
    await server.create_thread("plan", participants=["a1", "a2"])

    protocol = CollaborationProtocol(server, participants=["a1", "a2"])
    protocol.start = lambda: None  # type: ignore[method-assign]
    protocol.phase_manager._phase = P2_SPLIT
    tid = server.resolve_thread_id("plan")
    assert tid is not None
    gate = ConsensusGate(server, thread_name="plan", require_proposal=False)
    gate.bind_to_thread(tid)
    protocol._gates[P2_SPLIT] = gate
    protocol._current_gate = gate

    agent = type("A", (), {
        "agent_id": "a1",
        "_attention_policy": object(),
        "_attention_config": {
            "floors": {"near_gate": 0.5, "p1_ready_pending": 0.15, "default": 0.0},
        },
    })()

    # closed, 0 approvals → default (T0 allowed)
    assert not gate.is_open
    assert len(gate.approvals) == 0
    assert _compute_phase_floor(agent, protocol) == 0.0

    # one approval → near_gate
    gate.approvals.add("a2")
    assert _compute_phase_floor(agent, protocol) == 0.5


async def test_near_gate_floor_lifts_t0_to_skim_in_step():
    """phase_floor=near_gate(0.5) → broadcast도 T0 금지 (engage; ≥ skim)."""
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])
    await server.send_message(tid, author="agent-1", content="noise", mentions=[])

    policy = _zero_features_policy()
    agent = _make_agent(
        server, "agent-2", [Completion(text="floor lift")],
        attention_policy=policy,
        attention_config={"floors": {"default": 0.0}, "context": {"skim_max_chars": 400}},
    )
    agent.phase_floor = 0.5  # near_gate → r≥0.45 → engage
    result = await agent.step()

    assert not result.skipped
    assert len(agent.backend.calls) == 1
    user_msgs = [m for m in agent.backend.calls[0]["messages"] if m["role"] == "user"]
    # 0.5 ≥ skim(0.45) → full radio (engage), not T0
    assert "[radio]" in user_msgs[0]["content"]
    assert "[radio — skim]" not in user_msgs[0]["content"]


# ---------------------------------------------------------------------------
# 17. T0 skip ≠ 에이전트 종료 — 이후 멘션으로 재참여 (리뷰 P0-1)
# ---------------------------------------------------------------------------


async def test_t0_skip_then_mention_still_runs():
    """T0 skip 후에도 같은 agent가 다음 멘션을 engage로 처리한다."""
    server = MessageServer()
    tid = await server.create_thread("t", participants=["agent-1", "agent-2"])

    policy = _mention_only_policy()
    agent = _make_agent(
        server, "agent-2",
        [Completion(text="after mention")],
        attention_policy=policy, attention_config={},
    )

    await server.send_message(tid, author="agent-1", content="noise", mentions=[])
    r1 = await agent.step()
    assert r1.skipped is True
    assert len(agent.backend.calls) == 0

    await server.send_message(tid, author="agent-1", content="hey you", mentions=["agent-2"])
    r2 = await agent.step()
    assert not r2.skipped
    assert len(agent.backend.calls) == 1
    assert r2.text == "after mention"


# ---------------------------------------------------------------------------
# 18. 시나리오 A — 비멘션 broadcast → T0 비율 / complete 감소
# ---------------------------------------------------------------------------


async def test_scenario_a_broadcast_skips_unmentioned():
    """4에이전트 중 멘션 없는 broadcast 수신자 3명은 T0 (complete 미호출)."""
    server = MessageServer()
    agents_ids = ["agent-1", "agent-2", "agent-3", "agent-4"]
    for aid in agents_ids:
        server.register_agent(aid)
    tid = await server.create_thread("exec", participants=agents_ids)

    await server.send_message(
        tid, author="agent-1", content="(FYI) status update", mentions=[]
    )

    policy = RelevancePolicy(
        features={"mention_boost": 0.5, "thread_participant": 0.0, "recent_interact": 0.0},
        tiers={"ignore": 0.15, "skim": 0.45, "engage": 0.80},
        server=server,
    )

    skipped = 0
    completes = 0
    for aid in ("agent-2", "agent-3", "agent-4"):
        agent = _make_agent(
            server, aid, [Completion(text="should skip")],
            attention_policy=policy, attention_config={},
        )
        result = await agent.step()
        if result.skipped:
            skipped += 1
        completes += len(agent.backend.calls)

    assert skipped == 3
    assert completes == 0


# ---------------------------------------------------------------------------
# 19. skim 최신 메시지는 seq 기준 (리뷰 P2-1)
# ---------------------------------------------------------------------------


def test_format_radio_block_skim_picks_highest_seq():
    from agent_augury.core.agent.loop import format_radio_block_skim

    msgs = [
        {"author": "a", "content": "newest-by-seq", "seq": 9},
        {"author": "b", "content": "older-but-last-in-list", "seq": 2},
    ]
    result = format_radio_block_skim(msgs, max_chars=400)
    assert "newest-by-seq" in result
    assert "older-but-last-in-list" not in result

