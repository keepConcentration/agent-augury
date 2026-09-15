# AGENT_RELEVANCE_BUDGET_DESIGN — P1 리뷰 및 보강 제안

> **작성자:** agent-2
> **Date:** 2026-09-15
> **대상:** `AGENT_RELEVANCE_BUDGET_DESIGN.md` (V1 기준)
> **현황:** V1 landed — G1(옵션 A Session floor 주입), G4(URGENT), G6(P1⊥near_gate) 반영.
> 형제: `AGENT_RELEVANCE_BUDGET_REVIEW.md` (P0/P1 코드 대조 리뷰).

---

## 1. 종합 평가

설계 문서는 **전반적으로 충실**하다. 동기 → 개념 모델 → 코드 삽입점 → YAML 계약 → 테스트 계획 → 위험 완화까지 빠짐없이 다루고 있다. 특히 §4.3의 불변식("T0 = drain 필수 + complete 스킵")과 §5.2의 Legacy finish 충돌 방지 분기는 실제 구현에서 흔히 발생할 수 있는 함정을 잘 지적하고 있다.

다만 **구현 세부사항에서 몇 가지 갭**이 발견되었으며, 아래에 구체적인 보강 제안을 정리한다.

---

## 2. 발견된 갭 및 보강 제안

### G1. `AgentLoop`에 `_protocol` / `_attention_policy` 주입 경로 불명확

**위치:** §5.3 `_compute_phase_floor`, §5.1 pseudocode

**문제:** `_compute_phase_floor`는 `self._protocol`을 참조하지만, 현재 `AgentLoop`는 `CollaborationProtocol`에 대한 참조를 갖고 있지 않다. `Session._inject_protocol_gate_state()`가 매 step마다 `agent.current_phase`, `agent.gate_open`, `agent.gate_thread_id`를 주입하는 것처럼, `_compute_phase_floor`에 필요한 정보(`protocol.phase`, `protocol.has_ready(agent_id)`, `protocol.gate_for(phase).is_open`)도 매 step마다 Session이 계산해서 AgentLoop에 넘겨주는 방식이 현재 아키텍처와 일관된다.

**제안:** 아래 두 가지 옵션 중 하나를 문서에 명시할 것:

- **옵션 A (추천):** AgentLoop에 `_protocol` 참조를 추가하지 않고, `step()`이 호출되기 전에 `Session._run_impl` → `run_agent()`에서 `phase_floor`를 미리 계산하여 `agent.phase_floor`에 주입. `_compute_phase_floor`는 AgentLoop 메서드가 아닌 Session의 private helper로 이동.

```python
# session.py _run_impl → run_agent() 내부
agent.phase_floor = _compute_phase_floor(agent, protocol)
```

- **옵션 B:** AgentLoop 생성자에 `protocol: CollaborationProtocol | None`을 추가하고 `_compute_phase_floor`를 AgentLoop 내부에 둔다. 단, 이 경우 AgentLoop가 Protocol에 대한 순환 의존을 갖게 된다.

**설계문서 수정 제안:** §5.3의 `_compute_phase_floor` 의사코드 아래에 "구현 선택: phase_floor는 Session이 계산하여 step() 호출 전 agent에 주입 (AgentLoop <-> Protocol 직접 의존 회피)" 라는 결정 문장을 추가.

---

### G2. `AttentionPolicy` / `RelevancePolicy` 객체 생성 및 주입 흐름 누락

**위치:** §6 (YAML 계약), §8 (구현 단계) 사이

**문제:** 설계문서는 `RelevancePolicy` 클래스 API는 §7에 정의했지만, 이 객체가 **언제, 어디서** 생성되고 AgentLoop에 어떻게 전달되는지 명시되어 있지 않다. `ToolPolicy`가 `Session.from_config()` → 각 `AgentLoop(..., policy=agent_policy)`로 전달되는 흐름을 참고할 수 있다.

**제안:** §8 (구현 단계) 또는 신규 §5.4에 다음 흐름을 추가:

```text
Session.from_config()
  ├─ attention_cfg = cfg.get("attention") or {}
  ├─ global_attention_config = AttentionConfig.from_yaml(attention_cfg)  # V1b
  ├─ for each agent:
  │    ├─ agent_attention = global_attention_config.merge(spec.get("attention"))
  │    ├─ policy = RelevancePolicy(agent_attention)  # V1a
  │    └─ AgentLoop(..., attention_policy=policy, attention_config=agent_attention)
  └─ ...
```

---

### G3. `StepResult.skipped` 필드 누락

**위치:** §5.1 pseudocode, §5.2 session 변경점

**문제:** `StepResult`는 현재 `text`, `tool_calls`, `drained_count`, `usage`만 갖는다. §5.1에서 `StepResult(..., skipped=True)`를 반환하고 §5.2에서 `getattr(result, 'skipped', False)`로 체크하는데, 이 필드가 `StepResult` 데이터클래스에 정의되어야 한다.

**제안:** §5.1의 StepResult 사용 예시에 아래 주석 추가:

```python
# StepResult에 skipped: bool = False 필드 추가 필요 (V1c)
```

---

### G4. `format_radio_block_skim` — prefix 인식 누락

**위치:** §5.3 `format_radio_block_skim` 명세

**문제:** 현재 skim은 "최신 1개 메시지 전문"을 싣지만, URGENT/FYI prefix를 인식하지 않는다. 팀 커뮤니케이션 규칙상 URGENT 메시지는 수신자가 반드시 처리해야 하므로, URGENT 메시지가 skim으로 인해 1줄 digest + 잘린 최신 메시지로만 전달되는 것은 위험하다.

**제안:** `format_radio_block_skim`에 아래 규칙 추가:

```python
def format_radio_block_skim(messages, max_chars=400):
    # ... digest 줄 ...
    # URGENT 메시지가 drained set 안에 있으면 → T2로 승격 (skim 대신 full radio)
    urgent = [m for m in messages if (m.get("content") or "").lstrip().upper().startswith("URGENT")]
    if urgent:
        # fallback to full format — URGENT는 skim 대상에서 제외
        return format_radio_block(messages)
    # ... 기존 skim 로직 ...
```

또는, 더 간단히: `RelevancePolicy.score_message()`에서 URGENT prefix 감지 시 `r`에 boost를 주는 방식. YAML에 `urgent_boost: 0.5` 같은 피처 추가.

---

### G5. Per-agent `attention:` merge 명세 부재

**위치:** §6 YAML 계약

**문제:** per-agent override 예시만 있고, merge 시맨틱이 명시되어 있지 않다. `ToolPolicy.merge()`는 각 subsection (shell/web/file/approval) 별 deep-merge를 명확히 정의했으나, `attention:`은 `floors`, `tiers`, `features` 등 중첩 키가 있어 merge 방식이 모호하다.

**제안:** §6 하단에 merge 규칙 추가:

```yaml
# Merge 규칙:
# - attention.enabled: agent 값이 명시되었으면 우선, 아니면 전역
# - floors: agent의 floors 키가 전역 floors를 오버라이드 (shallow merge per floor key)
# - tiers: agent 값이 명시되었으면 우선, 아니면 전역
# - features: agent 값이 명시되었으면 우선, 아니면 전역
# - schedule, context: agent 값이 명시되었으면 우선, 아니면 전역
```

---

### G6. `near_gate` floor + P1 READY pending floor 중첩 시 우선순위

**위치:** §4.3, §5.3 `_compute_phase_floor` 의사코드

**문제:** `_compute_phase_floor`는 `near_gate`가 먼저 체크되고, 그 다음 `P1 + READY 미제출`을 체크한다. 하지만 P1에서 near_gate 상황은 존재하지 않으므로(게이트 개념이 P2부터), 실제 충돌은 없다. 다만 코드 독자에게 혼란을 줄 수 있으므로 명시적 주석이 필요.

**제안:** `_compute_phase_floor` 의사코드에 주석 추가:

```python
# P1_EXPLORE에서는 near_gate가 정의되지 않으므로 두 조건이 동시에 true인 경우는 없다.
```

---

### G7. Conversation compact (M4) 와의 상호작용

**위치:** 신규 § 또는 §5 (삽입점), §10 (위험)

**문제:** T0 skip으로 인해 conversation에 주입되지 않은 메시지가 SSOT에는 남아있고, 추후 compact 시 이 메시지들이 conversation 요약에 포함되지 않을 수 있다. 즉, agent가 "못 들은" 메시지가 compact 후에도 "못 들은 상태"로 남는다. 이 자체는 의도된 동작(T0 = 인지하지 않음)이지만, summary agent(LLM compact)가 SSOT 기반 요약을 생성할 경우 conversation과 SSOT 간 불일치가 발생할 수 있다.

**제안:** §10 위험 테이블에 한 줄 추가:

| 위험 | 완화 |
|------|------|
| T0 skip + compact → conversation에 없는 메시지가 영구 소실 | compact은 conversation만 요약 (이미 설계 의도). SSOT 기반 재주입이 필요하면 V2에서 `read_resource` tier별 제한으로 처리 (§8 V2b) |

---

### G8. `RelevancePolicy.decide()` → `BudgetDecision.run_llm` 필드명

**위치:** §7 런타임 API

**문제:** `BudgetDecision`에 `run_llm: bool`이 정의되어 있지만, §5.1 pseudocode에서는 `decision.tier == "ignore"`로 분기한다. `run_llm` 필드는 사용되지 않고 `tier`로만 분기하는데, 필드 중복이다. V1에서는 `tier` 기반 분기면 충분하다.

**제안:** §7에서 `run_llm` 필드에 "V1에서는 `tier == 'ignore'`와 완전히 동치. V1.1에서 `tier`만으로 결정할 수 없는 edge case를 위해预留" 라고 명시. 또는 V1에서는 `run_llm` 필드를 제거하고 `tier`만 사용.

---

### G9. 테스트 시나리오 B — `complete` 호출 횟수 vs 주입 chars 혼동

**위치:** §9 테스트 계획

**문제:** 시나리오 B에서 "complete 호출 횟수는 baseline과 동일(또는 비슷)하되, 주입 context chars ≤ skim_max_chars"라고 되어 있다. 그런데 V1에서는 `complete(messages, tools)`에 `max_tokens` 인자가 없으므로, complete 호출 횟수는 near_gate에서도 **baseline과 동일**하다 (T0 금지 → 전원 최소 skim → 전원 complete 호출). 차이는 **주입되는 context의 char 수**뿐이다. 이 문장은 정확하지만 "또는 비슷"이라는 표현이 모호하다.

**제안:** "complete 호출 횟수는 baseline과 동일"로 명확히 수정. ("비슷" 삭제 — floor가 모든 agent를 T1 이상으로 올리므로 complete를 스킵하는 agent가 없음.)

---

## 3. 추가 제안 (Optional, V1 범위 밖)

### S1. `AttentionScores.by_agent` 활용 방안

현재 `by_agent`는 V1에서 사용되지 않는다. V2에서 송신 topology(sparse)를 도입할 때, "agent-1이 agent-2에게 보낼지 말지"를 결정할 때 `by_agent` 점수를 참조할 수 있다. §7에 "V2 송신 topology에서 사용 예정" 주석 추가를 제안.

### S2. `digest` 큐 구현 방식

§4.3에서 "digest는 옵션 큐(또는 conversation에 `[digest]` 1줄)"이라고 하는데, 큐 접근법은 구현 비용이 높다. V1에서는 `[digest]` 1줄 conversation 주입으로 충분하며, 큐는 V2로 이연하는 것이 좋다.

---

## 4. 코드 의존성 매핑 (현재 코드베이스 기준)

```
신규 파일:
  core/attention.py          — RelevancePolicy, AttentionConfig, BudgetDecision, AttentionScores

수정 파일:
  core/agent/loop.py         — AgentLoop: attention_policy, attention_config, step() 분기,
                               StepResult.skipped 추가, format_radio_block_skim
  core/session.py            — Session.from_config(): attention YAML 파싱, policy 주입
                               _run_impl → run_agent(): phase_floor 계산 및 주입,
                               total_steps skipped 체크
  config.py                  — attention: 섹션 검증 (V1b)

신규 테스트:
  tests/test_attention_budget.py

영향 없음 (읽기만):
  core/agent/system_prompt.py
  core/agent/policy.py       — ToolPolicy와 별개 (attention은 별도 YAML 섹션)
  core/agent/tools.py
  core/agent/web.py
  core/server.py
  core/protocol/*
```

---

## 5. 결론

설계 문서는 **V1 구현에 필요한 대부분의 정보**를 담고 있다. 위에 제시한 9개 갭(G1~G9)은 대부분 **구현 세부사항 수준**의 보강이며, 설계 방향 자체의 변경이 필요한 사항은 아니다. 특히 **G1(protocol 의존성 주입)**과 **G2(policy 생성 흐름)**는 구현 착수 직전에 결정되어야 할 구현 선택지다.

**권장 우선순위:**
1. G1, G2 — 구현 시작 전에 결정 (아키텍처 선택)
2. G3, G4 — 구현 중 자연스럽게 해결
3. G5~G9 — 문서 보강 수준, 구현과 병행 가능