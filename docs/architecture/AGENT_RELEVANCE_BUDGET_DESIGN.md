# Agent relevance · reasoning budget 설계

> **Status:** V1 landed (2026-09-15) — `core/attention.py` · loop/session/config · `tests/test_attention_budget.py`  
> **잔여:** V1.1 (`max_tokens`/tools 캡), V2 sparse 송신, V3 Wire `log.attention`  
> **Date:** 2026-09-15  
> **Parent:** P1–P5 collaboration (`core/protocol`), agent loop (`core/agent/loop.py`)  
> **인접:** `SESSION_RESUME_M4_DESIGN.md` (conversation compact), `MULTI_FRONT_DESIGN.md` (표면≠인지)  
> **전제:** Core·Wire·게이트 계약은 SSOT. 본 설계는 **에이전트 간 인지 자원 배분**이며  
> 채팅 surface display(A6) · Gateway fan-out(A7)과 **축이 다르다**.
> **리뷰:** `AGENT_RELEVANCE_BUDGET_REVIEW.md` (P0 반영: skip≠종료, human 강제 engage, F3 제거).
---

## 0. 한 줄

에이전트가 **모든 동료 메시지를 같은 깊이로 처리하지 않는다.**  
메시지/상대에 대한 **relevance \(r \in [0,1]\)** 를 두고,  
그 값으로 **들을 양 · 응답 여부 · reasoning/tool budget** 을 한 줄기로 차등 배분한다.  
목표는 P1–P5 + N-agent에서 **토큰·지연을 줄이되**, 합의 구간 안전성은 유지하는 것이다.

---

## 1. 동기

| 오늘 (전형) | 비용 |
|-------------|------|
| 스레드 broadcast + 전원 `read_resource` / 동일 step 깊이 | \(O(N^2 \times turns \times ctx)\) |
| 멘션 없어도 “전원에게 말함” 관례 | 무관한 에이전트도 full context |
| `compact` (M4b) | **과거** 압축만 — **이번 턴** 배분은 없음 |

4에이전트 × 동일 모델 × P1–P5는 체감 토큰이 급증한다.  
표면(Discord/Slack) 미러링을 줄여도 **Core 안 LLM 호출**이 병목이면 효과가 작다.

**원칙:** “논문을 재현한다”가 아니라  
**Augury에 맞는 결정론·측정 가능한 budget 노브**를 먼저 넣는다.

---

## 2. 목표 · 비목표

### 2.1 목표

1. 수신 에이전트 \(B\)가 발신/메시지 \(A\)에 대해 \(r_{B\leftarrow A}\) 를 갖는다.  
2. \(r\) 가 **context 주입량 · step 실행 여부 · max tokens / tools** 를 결정한다.  
3. **페이즈-aware floor/ceiling** — 게이트·합의 구간에서 silent drop 금지(또는 floor↑).  
4. V1은 **휴리스틱 \(r\)** (학습/추가 LLM judge 없음).  
5. 절감량을 **측정 가능**하게 (`meta` 또는 로그: skipped steps, truncated ctx chars).

### 2.2 비목표 (V1)

| 제외 | 이유 |
|------|------|
| 학습형 gating / RL communication | 제품·재현성·오프라인 테스트 비용 |
| 매 메시지 LLM “이 메시지 중요?” 판정 | 절약 본전 잠식 |
| Wire에 `relevance` 이벤트 필수 발행 | Core 계약 비대화; V2 옵션 |
| Gateway / chat display 밀도(A6)와 통합 | 축이 다름 (사람 UI vs agent 인지) |
| 송신 sparse topology 전면 (누구에게 보내지 않을지) | V2; 수신 budget이 먼저 ROI |
| A9 다중 human 매핑 | 별 트랙 |
| `ModelBackend.complete` 시그니처에 `max_tokens` / tool allowlist | V1.1 (API 갭); V1은 step skip + context chars만 |

---

## 3. 관련 연구와의 위치

기존 연구는 대개 두 축이다.

```text
          Communication
               │
  ┌────────────┴────────────┐
  │                         │
누가 말할까?              무엇을 들을까?
topology / pruning      attention / relevance
T2MAC, GTD, …           Agent-Radar, SAR, …
```

본 설계의 강조점은 **세 번째 축**:

```text
r ∈ [0,1]
  ├─ listen depth   (context tier)
  ├─ reply policy   (ignore / short / full)
  └─ reason budget  (no-step / low / high + tools)
```

| 논문·방향 (참고) | 겹침 | Augury와의 차이 |
|------------------|------|-----------------|
| Structured Attentive Reasoning (NeurIPS’20) | 수신 relevance | MARL; LLM step budget 아님 |
| Message Pruning (AAAI’20) | 덜 유용한 메시지 drop | bandwidth; 페이즈 게이트 없음 |
| T2MAC (AAAI’24) | targeted / selective engagement | 송신 topology 중심 |
| Agent-Radar (2026) | LLM context relevance | 주로 context/attention; 지속 agent–agent 거리 + budget 묶음은 약함 |
| Graph Diffusion topologies (ACL’26) | sparse task-adaptive graph | 생성 모델; V1 휴리스틱과 거리 |

**주장하는 새로움(제품 관점):**  
\(r\) 하나를 **routing 확률만이 아니라 reasoning/tool budget allocator** 로 쓴다.  
학술 우선순위가 아니라 **토큰 절약 + P1–P5 안전**이 성공 기준이다.

---

## 4. 개념 모델

### 4.1 Relevance

수신자 \(B\), 단서 \(x\) (메시지 또는 상대 에이전트 \(A\)):

\[
r_{B}(x) = \mathrm{clip}_{[0,1]}\big( \sum_i w_i f_i(B, x) \big)
\]

V1 피처 \(f_i\) (가중 합; 학습 없음):

| ID | 피처 | 예시 |
|----|------|------|
| F1 | **명시 멘션** | `mentions`에 \(B\) 포함 → 강한 boost (최소 T2) |
| F2 | **스레드 소속** | \(B\)가 participants인 스레드의 메시지 |
| ~~F3~~ | ~~페이즈 역할~~ | **V1 제거** — 코드에 assignee/proposer 없음. assembler는 per-agent `attention.floor` |
| F4 | **최근 상호작용** | 최근 \(k\)턴 내 \(A{\leftrightarrow}B\) 왕복 |
| F5 | **역할 인접** | V1에서는 제외. V2에서 역할 그래프(예: YAML `role` 기반)로 도입 가능 |
| F6 | **시간 감쇠** | 오래된 broadcast는 \(r\) 하락 (옵션) |

**Human 메시지** (`author == "human"`): 휴리스틱과 무관하게 \( r = 1.0 \) (항상 T2+).  
사용자 입력이 T0으로 무시되면 안 된다 (리뷰 P0-2).

**Broadcast** (`mentions: []`): 전원에게 전달은 유지하되,  
비멘션 수신자는 F1=0 → 기본 \(r\) 낮음. 페이즈 floor가 덮어쓸 수 있다.
### 4.2 Budget tiers (수신 측)

| Tier | \(r\) 구간 (초안) | Context | Agent step | 응답 |
|------|-------------------|---------|------------|------|
| **T0 ignore** | \( r < 0.15 \) | 주입 없음 (또는 1줄 digest 큐) | **`complete` 스킵** (inbox는 drain 필수 — §4.3) | 없음 |
| **T1 skim** | \( 0.15 \le r < 0.45 \) | digest + 최신 1메시지 (`skim_max_chars`) | step 실행; V1은 chars만 제한 (low max_tokens는 V1.1) | optional 한두 문장 |
| **T2 engage** | \( 0.45 \le r < 0.80 \) | 관련 스레드 보통량 | normal | 정상 |
| **T3 intervene** | \( r \ge 0.80 \) | full + 필요 시 read | high / tools 허용 (tools 캡은 V1.1) | 적극 |

구간 경계는 YAML로 조정 가능. **코드 상수 기본값 + 설정 override.**

**경계값 근거 (V1 기본 가정):**
- F1(mention_boost)=0.5, F2(thread_participant)=0.25, F4(recent_interact)=0.2  
  (F3 제거 — assembler는 `attention.floor`)
- `tiers.skim`=0.45 = **T2 engage 하한**; `tiers.engage`=0.80 = **T3 intervene 하한**
- 불변식: `mention_boost >= tiers.skim` (멘션만으로 최소 T2)
- mention 없는 broadcast 수신자 예상 \( r \) 범위:
  - 스레드 비참여 + 무관 페이즈: F2=0, F4≈0 → \( r \approx 0.0 \) → **T0**
  - 스레드 참여 + 무관 페이즈: F2=0.25, F4≈0 → \( r \approx 0.25 \) → **T1 skim**
  - 직접 멘션: F1=0.5 → 최소 **T2** (의도: 멘션 = 최소 engage)
- 페이즈 floor(near_gate=0.5) 활성 시 T0 강제 → T1 이상 (실제 0.5 → T2)
**예시:** 3에이전트 P3에서 agent-3이 agent-1에게만 PROPOSE 보냄([`mentions: [agent-1]`]) — agent-2는 해당 메시지에서 F1=0, F2≈0.25(같은 스레드), F3=0, F4≈0 → \( r \approx 0.25 \) → T1 skim.

### 4.3 페이즈 정책 (안전장치)

| Phase (개념) | 정책 |
|--------------|------|
| 탐색·분업 (예: P2 split 이후 작업) | T0 허용; 무관 에이전트 LLM 억제 |
| 게이트 직전 / 만장일치 창 | **floor \(r_{\min}\)** (예: 0.5) — silent ignore 금지 |
| Assembler / 제출 (예: P5) | assembler는 T2+; 나머지는 T1 가능 |
| Human approval 대기 | 인지 budget과 독립 (사람 경로) |

#### 불변식 (코딩 전 계약)

1. **SSOT 보존:** ConsensusGate가 기대하는 “에이전트가 제안을 본다”는 구간에서  
   T0으로 메시지를 **영구 삭제**하지 않는다. 스킵은 “이번 틱에 LLM을 안 돌림”이지,  
   스레드 SSOT에서 메시지 제거가 아니다.

2. **T0 = drain 필수 + `complete` 스킵 (「`step()` 호출 생략」 단독 금지):**  
   현재 `AgentLoop.step()`은 **안에서** `drain_inbox` 후 `backend.complete`를 호출한다.  
   park wake 조건은 **`inbox_size > 0`** (`session.py` `_wait_for_gate_wakeup`).  
   따라서:
   - T0이어도 inbox는 **반드시 drain**.
   - LLM / `backend.complete`만 스킵.
   - digest는 옵션 큐(또는 conversation에 `[digest]` 1줄).
   - `total_steps` / `max_steps`에는 **세지 않음**.
   - 구현 형태: `step(budget=...)` 분기 **또는** drain-only 헬퍼.  
     **「스케줄러가 `step()` 호출 자체를 생략」만으로는 금지** — drain이 안 되면  
     inbox 잔류 → park 직후 재 wake → **스핀**, 또는 밖에서 drain만 하고  
     conversation 미주입 시 **인지 영구 소실**(SSOT엔 남음).

3. **배치 → 단일 `BudgetDecision`:**  
   한 step에서 drained 메시지가 여러 개일 수 있다.  
   \[
   r_{\mathrm{step}} = \max\{ r(m) \mid m \in \mathrm{drained} \}
   \]  
   (drained 비면 idle/default).  
   floor 적용: \( r_{\mathrm{final}} = \max(r_{\mathrm{step}},\ \mathrm{phase\_floor},\ \mathrm{agent\_floor}) \).  
   `by_message`는 관측/디버그용; **실행 tier는 `r_final` 하나**로 정한다.  
   (평균·마지막·첫 메시지 금지 — 구현자 분기 방지. 멘션 하나라도 있으면 engage 이상으로 올라가게.)

4. **`near_gate` / P1 READY 코드 매핑:**  
   코드에 `near_gate` 플래그는 없다. V1 floor 트리거는:
   - **near_gate floor:** `gate_for(phase) is not None and not gate.is_open and len(gate.approvals) >= 1`  
     (합의 창이 데워진 구간만. **닫힌 게이트 전체**에 걸면 P3 broadcast T0 절감이 사라짐 — §9 시나리오 A)
   - **P1_EXPLORE + READY 미제출:** floor ≥ skim, **T0 금지**  
     (`_maybe_nudge_ready`가 무력화되지 않도록)
   - **assembler:** V1은 **수동 per-agent `attention.floor` override** (예: `floor: 0.8`).  
     `assembler_id` 자동 적용은 하지 않음.
---

## 5. Augury 삽입점

```text
Server SSOT (threads/messages)     ← 변경 최소
        │
        ▼
AgentLoop 스케줄 / prompt 조립     ← V1 주 삽입점
        │
   drain_inbox (항상)
        │
   RelevancePolicy.score(...) → r_step = max(r(m))
        │
   BudgetDecision → tier 결정
        │
   ┌─ T0 ignore: conversation에 주입 없음 (또는 digest 1줄 옵션)
   │             backend.complete 호출 스킵
   │             StepResult(text=None, drained_count=N, skipped=True)
   │
   ├─ T1 skim:   format_radio_block_skim(drained, skim_max_chars)
   │             → digest 1줄 + 최신 1메시지만 conversation에 append
   │             backend.complete 정상 호출
   │
   └─ T2/T3:     format_radio_block(drained) — 기존 동일
                 backend.complete 정상 호출
        │
        ▼
Backend LLM (T0이면 호출 없음)
```

### 5.1 AgentLoop.step() 분기 (V1c pseudocode)

현재 `AgentLoop.step()`은 drain → radio block append → complete → tool execution 순이다.
V1c에서 다음과 같이 분기한다:

```python
async def step(self) -> StepResult:
    self._update_phase_in_prompt()
    drained = await self.server.drain_inbox(self.agent_id)

    # --- V1: relevance budget ---
    if self._attention_policy is not None and drained:
        scores = self._attention_policy.score_batch(
            self.agent_id, drained, phase=self.current_phase
        )
        # phase floor: near_gate / P1 READY 미제출
        phase_floor = self._compute_phase_floor()
        agent_floor = self._attention_config.get("floor", 0.0)
        decision = self._attention_policy.decide(
            self.agent_id, scores,
            phase=self.current_phase,
            phase_floor=phase_floor,
            agent_floor=agent_floor,
        )
        if decision.tier == "ignore":
            # T0: drain 했으나 complete 스킵
            # digest 옵션: conversation에 [digest] 1줄 주입 가능
            if self._attention_config.get("context", {}).get("t0_digest", False):
                self.conversation.append({
                    "role": "user",
                    "content": f"[digest] {len(drained)} message(s) skipped (low relevance)"
                })
            return StepResult(
                text=None, drained_count=len(drained),
                skipped=True,  # total_steps 미증가용
            )
        elif decision.tier == "skim":
            # T1: digest + 최신 1메시지만
            self.conversation.append({
                "role": "user",
                "content": format_radio_block_skim(
                    drained, max_chars=decision.context_max_chars or 400
                )
            })
        else:
            # T2/T3: 전체 radio block (기존 동작)
            self.conversation.append({
                "role": "user",
                "content": format_radio_block(drained)
            })
    elif drained:
        # attention disabled 또는 policy 없음 → 기존 동작
        self.conversation.append({
            "role": "user", "content": format_radio_block(drained)
        })

    # T0이면 여기서 return already (위에서 early return)
    completion = await self.backend.complete(self.conversation, self.tool_specs)
    # ... 이하 기존과 동일 (tool execution 등)
```

### 5.2 session.run_agent() 변경점

```python
# total_steps 증가 로직:
result = await agent.step()
if not getattr(result, 'skipped', False):
    total_steps[0] += 1  # T0 스킵이면 증가하지 않음

# T0 skip → 즉시 다음 루프 반복 (Legacy finish 경로 타지 않도록)
if getattr(result, 'skipped', False):
    await asyncio.sleep(0)
    continue
```

**주의 — `run_agent()` 기존 플로우와의 충돌 방지:**  
기존 루프는 `result.text is None` → break (Legacy finish). T0 스킵에서도 `result.text is None`이므로,  
`skipped` 체크를 Legacy finish 체크보다 먼저 해야 한다. 위 분기는 tool_calls·has_pending 체크 직후,  
gate-wait park 이전에 둔다.

### 5.3 Phase floor 계산 (`_compute_phase_floor`)

```python
def _compute_phase_floor(agent, protocol) -> float:
    if protocol is None:
        return 0.0
    floors = (agent._attention_config or {}).get("floors", {}) or {}
    phase = protocol.phase
    gate = protocol.gate_for(phase)

    # near_gate: 닫힌 게이트 + ≥1 approval (합의 창 warming)
    if gate is not None and not gate.is_open and len(gate.approvals) >= 1:
        return float(floors.get("near_gate", floors.get("default", 0.0)))

    # P1 + READY 미제출 → 최소 skim (T0 금지)
    if phase == "P1_EXPLORE" and not protocol.has_ready(agent.agent_id):
        return float(floors.get("p1_ready_pending", floors.get("default", 0.0)))

    return float(floors.get("default", 0.0))
```

**코드 의존 관계 (G1 결정):** 설계문서 §4.3 불변식과 현재 아키텍처를 고려하여,  
**옵션 A (Session이 `phase_floor`를 계산해 step 전 주입)**을 채택한다.  
`AgentLoop`는 `CollaborationProtocol`을 직접 참조하지 않으며,  
`Session._run_impl → run_agent()`에서 `_compute_phase_floor(agent, protocol)`로 계산 후  
`agent.phase_floor = ...`로 주입한다. `step()`은 `self.phase_floor`를 읽기만 한다.

**참고 — P1_EXPLORE와 near_gate는 동시에 true가 될 수 없다 (G6):**  
P1에는 게이트 개념이 없으므로 (`gate_for(P1_EXPLORE)` → `None`),  
`_compute_phase_floor`의 near_gate 분기와 P1 READY pending 분기는 상호 배타적이다.
### 5.4 `format_radio_block_skim()` 명세

```python
def format_radio_block_skim(messages: list[Message], max_chars: int = 400) -> str:
    """T1 skim: digest 요약 + 최신 1메시지 (max_chars 이내로 자름)."""
    lines = ["[radio — skim]"]
    # 첫 줄: 몇 개 메시지를 요약했는지
    authors = sorted({m['author'] for m in messages})
    lines.append(f"(skim: {len(messages)} messages from {', '.join(authors)})")
    # 최신 메시지 1개만 전문 (max_chars 이내)
    latest = messages[-1]
    content = latest['content']
    if len(content) > max_chars:
        content = content[:max_chars] + "…"
    lines.append(f"from {latest['author']}: {content}")
    return "\n".join(lines)
```

| 계층 | V1 | 비고 |
|------|----|------|
| **Server `send_message`** | 저장·전달 의미 유지 | drop at wire 금지 (감사·재개) |
| **`read_resource` 결과** | V1에서는 soft truncate하지 않음. 대신 skim 수신자는 "최신 1개 메시지만 주입" (전체 스레드 요약 1줄 추가). V2 옵션으로 tier별 read limit 조정 가능. |
| **스케줄러 / `step()`** | T0이어도 drain은 수행; `complete`만 스킵 | 「step 호출 생략」 단독 금지 (§4.3) |
| **system / 관측 프롬프트** | skim이면 digest + 최신 1 | compact와 직교 |
| **Gateway / surfaces** | 무관 | A6는 사람 눈 |

`compact`(M4) = 장기 conversation 길이.  
본 설계 = **단기·상대별** 배분. 둘 다 켜도 됨.

---

## 6. YAML 계약 (제안)

```yaml
# 세션 YAML
attention:
  enabled: true              # 기본 false (오늘 동작 = 전원 동등)
  mode: heuristic            # V1; 이후 learned 금지 전까지 고정
  floors:
    default: 0.0
    near_gate: 0.5           # 게이트 임박 시 전역 floor (트리거: §4.3)
    p1_ready_pending: 0.15   # P1 + READY 미제출 → 최소 skim (T0 금지)
  tiers:
    ignore: 0.15
    skim: 0.45
    engage: 0.80
  features:
    mention_boost: 0.5       # 멘션 = 최소 T2 engage (≥ tiers.skim)
    thread_participant: 0.25
    recent_interact: 0.2
    # phase_role: V1 제거 — assembler는 agents[].attention.floor
  schedule:
    skip_t0_llm: true        # T0: drain 후 complete 스킵 (구 skip_t0_steps)
  context:
    skim_max_chars: 400
    # engage_max_chars: V1 unused (T2/T3 unlimited); V1.1 후보
```
**Per-agent override (V1b에서 지원):**

```yaml
agents:
- id: agent-1
  attention:
    floor: 0.8               # assembler 등 특정 에이전트는 항상 T2 이상
- id: agent-2
  # override 없음 → 전역 설정 상속
```

**동결 (V1 기본):**

| 키 | 기본 | 의미 |
|----|------|------|
| `attention.enabled` | `false` | 옵트인 — 기존 데모/테스트 불변 |
| `mode` | `heuristic` | |
| `skip_t0_llm` | `true` | 절감의 핵심 스위치 (drain은 항상) |
| LLM judge | 없음 | |
| `max_tokens` / tools 캡 | V1 없음 | V1.1 (backend 시그니처 확장 후) |

---

## 7. 런타임 API (초안)

```python
# core/attention.py (신규, 제안)
@dataclass(frozen=True)
class AttentionScores:
    """Per counterpart or per message id → r."""
    by_agent: dict[str, float]
    by_message: dict[str, float] | None = None

@dataclass(frozen=True)
class BudgetDecision:
    tier: Literal["ignore", "skim", "engage", "intervene"]
    r: float
    run_llm: bool                 # V1: tier == 'ignore'와 동치. V1.1 edge case 용 예비 필드.
    max_tokens: int | None        # V1: 항상 None; V1.1에서 사용
    context_max_chars: int | None
    tools_allowed: bool           # V1: tier와 무관하게 오늘 동작; V1.1에서 캡

class RelevancePolicy:
    def score_message(self, receiver_id: str, message: dict, *, phase: str) -> float: ...
    def score_batch(self, receiver_id: str, drained: list[dict], *, phase: str) -> AttentionScores:
        """Score each drained message; caller uses max(r) for decide()."""
        ...
    def decide(self, receiver_id: str, scores: AttentionScores, *, phase: str,
               phase_floor: float = 0.0, agent_floor: float = 0.0) -> BudgetDecision:
        """r_final = max(max(by_message values or idle), phase_floor, agent_floor)."""
        ...
```

- Session/AgentLoop가 phase·pending gate를 넘겨 floor를 적용.  
- 관측용: `BudgetDecision`을 step 메타 또는 debug 로그에 남김 (Wire 필수 아님).  
- **V1 측정 축:** `run_llm=False` 횟수 + 주입 `context` char 수.  
  `max_tokens` 합 비교는 backend API 확장(V1.1) 전까지 테스트 성공 기준으로 쓰지 않는다.

---

## 8. 구현 단계

| ID | 작업 | 산출 |
|----|------|------|
| **V1a** | `RelevancePolicy` 휴리스틱 + tiers + floors + batch max | `core/attention.py` |
| **V1b** | YAML `attention:` 검증 | `config.py` |
| **V1c** | AgentLoop: T0 = drain 필수 + `complete` 스킵 + skim context 슬라이스. **금지:** `step()` 호출 생략만으로 T0 구현. **skim:** `format_radio_block()`에 요약 1줄 + 최신 1메시지 (`skim_max_chars`). `total_steps` 미증가. | `core/agent/loop.py` |
| **V1d** | 페이즈 floor 배선: near_gate(`gate_for`+닫힘), P1 READY 미제출 ≥ skim, assembler floor | `protocol` / session |
| **V1e** | 테스트 + step/`complete` 호출·chars fixture | `tests/test_attention_budget.py` |
| **V1f** | 갭 문서 링크 · 벤치 노트 | docs / `examples/benchmark` |
| **V1.1** | `ModelBackend.complete(..., max_tokens=)` + tools allowlist | backend + BudgetDecision |
| **V2** | 송신 sparse: 비멘션 broadcast를 digest-only로 제안; F5 role graph | server 또는 prompt 힌트 |
| **V2b** | `read_resource` soft truncate by tier | tools |
| **V3** | 옵션: Wire `log.attention` / 학습형 score (연구) | schema · 별도 플래그 |

**권장:** V1만으로 “4에이전트 P1–P5 토큰” 체감 절감을 검증.  
학습·topology 생성은 효과가 숫자로 나온 뒤에.  
**V1c와 V1d는 같은 PR**에 넣는 것을 권장 (floor 없이 T0만 켜면 합의/READY 회귀).

---

## 9. 테스트 계획 (V1)

1. `enabled: false` → 기존 `test_phases` / p1–p5 데모와 동행.  
2. 멘션된 수신자만 T2+, 비멘션은 T0/T1 (`skip_t0_llm`).  
3. `near_gate` floor → T0 금지, 전원 최소 skim.  
4. P1 + READY 미제출 → T0 금지 (nudge 경로 유지).  
5. Assembler id(또는 per-agent floor)는 P5에서 engage 이상.  
6. **T0 drain 불변식:** T0 틱 후에도 inbox size == 0; park↔wake 스핀 없음.  
7. **배치 max:** drained에 멘션 1개 + broadcast 다수 → tier ≥ engage.  
8. **토큰/step 비교 fixture** (`tests/test_attention_budget.py`):
   - **시나리오 A — 4에이전트 P3 broadcast:** 모든 메시지 `mentions: []`. `attention.enabled=true` 시 T0 수신자 비율 ≥ 50% (baseline 대비 **`backend.complete` 호출 횟수** 감소).
   - **시나리오 B — near_gate floor 활성:** 전원 최소 skim → complete 호출 횟수는 baseline과 동일 (floor가 모든 agent를 T1 이상으로 올리므로 T0 skip 없음). **주입 context chars ≤ `skim_max_chars`** (V1).  
     (`max_tokens` 합 ≤ baseline은 **V1.1**로 이월 — 현재 `complete(messages, tools)`에 max_tokens 인자 없음.)
   - **Baseline:** `enabled: false` + 동일 시나리오 3회 평균 complete 횟수 / 주입 chars.

---

## 10. 위험 · 완화

| 위험 | 완화 |
|------|------|
| 합의 누락 (못 듣고 APPROVE 안 함) | phase floor; gate 창 T0 금지 |
| T0 skip + compact → conversation에 없는 메시지가 영구 소실 (G7) | compact은 conversation만 요약 (이미 설계 의도). SSOT 기반 재주입이 필요하면 V2에서 `read_resource` tier별 제한으로 처리 (§8 V2b) |
| T0을 step 생략으로 구현 → park 스핀 | §4.3: drain 필수 + complete만 스킵 |
| “조용한 에이전트”가 영구 소외 | 주기적 refresh tick / 역할 로테이션 floor |
| 휴리스틱 오탐 | `enabled` 옵트인; 경계값 YAML |
| 디버그 불가 | decision 로그; Ink는 여전히 full wire 가능 |
| compact와 이중 삭제 | compact은 history; attention은 주입 슬라이스 — 원본 SSOT 유지 |
| P1 READY nudge 무력화 | READY 미제출 시 floor ≥ skim, T0 금지 |

---

## 11. 결정 로그

| 결정 | 대안 | 이유 |
|------|------|------|
| 수신 budget 우선 | 송신 topology 먼저 | 구현·측정 쉬움; SSOT 보존 |
| 휴리스틱 V1 | LLM judge / RL | 본전·결정론 |
| 기본 `enabled: false` | 기본 on | 회귀·데모 안정 |
| 메시지 삭제 금지 | prune at server | 재개·감사·게이트 |
| T0 = drain + LLM 스킵 | `step()` 호출 생략 | inbox/park 계약과 정합 |
| 배치 \(r\) = max | 평균 / 마지막 | 멘션 누락·구현 분기 방지 |
| V1 측정 = skip + chars | max_tokens 합 | backend API 갭 |
| T0 스킵은 `skipped=True` + `continue` (종료 신호 분리) | `text=None` 재사용 | `result.text is None→break`와 충돌 방지 |
| human 메시지는 휴리스틱과 무관하게 \(r=1.0\) | score 병합 | 사용자 입력 유실 방지 |
| V1에서 F3 제거(assembler floor로 대체) | assignee 매핑 신설 | 코드에 근거 데이터 없음 |
| `context_max_chars`는 "새 주입 블록" 기준, engage/intervene unlimited | 전체 conversation 기준 | compact와 이중 절삭 방지 |
| `mention_boost >= tiers.skim` config 불변식 | `>= tiers.engage` | engage 키는 T3 하한; skim 키가 T2 하한 |
| near_gate = 닫힌 게이트 + ≥1 approval | 닫힌 게이트 전체 | P3 T0 절감(§9 A)과 양립 |
| V1 digest는 conversation 1줄 옵션; checkpoint 미포함 | digest 큐 영속 | V1 단순; 재개 시 SSOT에서 재구성 |
---

## 12. 갭 추적

- 본 문서 = **Core 인지 배분** (신규 트랙; A* 표면 갭과 번호 공유하지 않음).  
- 인접 landed: conversation `compact` (M4b/e), chat `display` (A6).  
- 구현 착수 시 `IMPLEMENTATION_GAP_CONSOLIDATED.md`에 **P2/P3 항목**으로 한 줄 추가 권장.  
- A8/B5/A1 잔여와 **경쟁하지 않는 별 트랙** — 표면 일과 섞지 않음.

---

## 13. 검색 키워드 (후속 문헌)

`selective multi-agent communication`, `communication sparsity`,  
`attention-based multi-agent communication`, `adaptive communication topology`,  
`reasoning budget allocation`, `compute allocation multi-agent`, `selective engagement`.
