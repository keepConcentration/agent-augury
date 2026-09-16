# Protocol chatter reduction — done-set · gate UI · P3+ vote soft-block · light mode

> **Status:** draft **rev.6** (설계 리뷰 5차 반영, 미구현)  
> **Date:** 2026-09-16  
> **Priority:** P1 (실사용 P1–P5 잡담·토큰·지연)  
> **Parent:** `PROTOCOL_GATE_WAIT_PARK_DESIGN.md`, `DESIGN.md` §2.3 / §6  
> **인접:** `AGENT_RELEVANCE_BUDGET_DESIGN.md` (인지 T0 / `StepResult.skipped`),  
>   `HUMAN_APPROVAL_GATE_DESIGN.md`, protocol thread reuse  
> **Code touch (예정):** `session.py`, `agent/loop.py`, `protocol/approval.py`,  
>   `protocol/collaboration.py`, `protocol/phases.py`, `agent/system_prompt.py`,  
>   `config.py`, `gateway/types.py` + Wire schema, `fronts/ink/{App,wire}.tsx`  
> **Tests (예정):** `tests/test_protocol_chatter.py`, Ink `wire.test.ts`  
> **Rev.6 요약:** C0 분할 — **C0a**(`session.gate` 이벤트)를 C2 **앞**,  
>   **C0b**(Ink 상태줄)는 뒤 · park/skip **의도적 스텝 무음** ·  
>   마지막 not-done 사망 시 park 행은 §10 후속(범위 밖) ·  
>   C0a=`EVENT_TYPES`+스키마+발행만(`translate` 불필요); Discord/Slack observe는 V1 비목표

---

## 0. 한 줄

게이트 대기 중 **done 에이전트는 while 최상단에서 park**하고,  
inbox로 깨어나도 **Loop 안에서 drain-only skip**한 뒤 **다시 park**에 걸려  
`backend.complete`가 돌지 않게 한다.  
`sleep`/`true`·중복 표는 툴 계층에서 끊고, Ink에는 게이트 현황을 보여 준다.

---

## 1. 배경 · 재현

| 증상 | 실제 원인 (코드) |
|------|------------------|
| `(대기)` 서술 | **표시**. 서술-only는 이미 park (`session.py` 1252–1275) |
| `sleep` / `true` | **무한 루프**. `tool_calls` → park 우회 |
| READY/APPROVE 후 complete | done 미인지 + 타인 표 웨이크 시 재호출 |
| 중복 `APPROVE:` | set 불변 · 메시지·N² 캐스케이드 |
| P3 조기 투표 | `require_proposal=False` + 투표 습관 |
| “게이트 안 열림” | Ink에 투표 UI 없음 |

---

## 2. 목표 · 비목표

### 2.1 목표 (V1)

1. **C1** 중복 READY/APPROVE soft-block (+ inject 3분기).  
2. **C3-sleep** `idle_not_allowed`.  
3. **prereq** `ready_states` checkpoint (in-place 또는 매 step 재주입 문서화).  
4. **C2+D5 한 묶음** — §3.2.1 park 없이 D5만 넣으면 **토큰 절감 0**.  
5. **C0** Ink 게이트 상태줄 (관측성).  
6. **C4/C5** 리뷰·실측 후.

### 2.2 비목표 / 별 이슈

- Wait-narration → 루프 개입 없음 (V1.1 표시 필터).  
- **Attention T0 후속-iteration complete 갭** — 아래 §3.2.4. 본 문서와 분리해  
  `AGENT_RELEVANCE_BUDGET` 후속으로 추적.  
- Slack Block Kit / NLP 합의.

---

## 3. 설계 1 — Done-set · D5 · sleep

### 3.1 Done-set 정의

| 페이즈 | Done | SSOT |
|--------|------|------|
| P1 | READY | `protocol.ready_states` (공개 읽기 전용; 내부 `_ready_states`) |
| P2–P5 | ∈ approvals | `ConsensusGate.approvals` |
| human_pending | 전원 done | gate flag |

```text
protocol.is_agent_done(agent_id) -> bool
```

새 병렬 set 금지.

### 3.2 C2 + D5 — **분리 불가**

#### 3.2.0 왜 D5만으로는 no-op인가 (현 T0가 증거)

```text
Session:  result.skipped → continue          # session.py:1233
          → while 최상단 → agent.step() 다시
Loop:     drained = []  (이미 비움) → T0/D5 분기 미진입
          → backend.complete()               # 한 iteration 미뤄진 complete
```

`skipped=True`가 주는 건 **반환 타입**이지 절감이 아니다.  
`test_t0_drain_invariance_inbox_empty_after_skip`도 **그 step의** `calls==0`만 본다.

**못 박기:**

- **C2(§3.2.1)와 D5는 한 PR/한 마일스톤.** D5만 머지 → 토큰 **0** 절감.  
- 절감은 §3.2.1이 `skipped→continue` 다음 iteration을 **park로 받아줄 때만** 생긴다.

#### 3.2.1 Done ∧ inbox==0 → park (**while 최상단**)

위치: `run_agent`의 **while 최상단**, gate 주입 블록 (`session.py` ~1196)과 같은 자리.  
**“step() 직전”이 아니라** 이 위치여야 `skipped→continue`가 다시 `step()`으로 떨어지지 않는다.

```text
while True:
    interrupt / max_steps …
    inject …   # §4.3 — protocol_done = waiting ∧ is_agent_done
    if agent.protocol_done:          # 이미 waiting∧done (재추론 금지)
        if server.inbox_size(agent) == 0:
            woke = await _wait_for_gate_wakeup(
                agent, steps_done=lambda: total_steps[0]
            )   # steps_done 필수 — 빠지면 max_steps 후에도 영구 park
            if not woke: break
            continue
    result = await agent.step()
    if result.skipped:
        continue   # → 최상단에서 다시 protocol_done∧inbox==0 → park
```

**기존 nudge와의 관계 (의도적 우회 — 회귀 아님):**

현 park(`session.py` ~1261) 순서: `_is_gate_waiting` → `_maybe_nudge_ready` →  
`_maybe_nudge_gate_thread` → park.

최상단 park는 이 둘을 **건너뛴다**. 결과는 옳다:

| nudge | done 에이전트 |
|-------|----------------|
| `_maybe_nudge_ready` | `has_ready`면 원래 미발화 → 무관 |
| `_maybe_nudge_gate_thread` | done에게도 “Send PROPOSE:/APPROVE:” — **이미 투표한 애에게 재투표 강요**. 죽는 것이 C2의 **숨은 이득** |

문서/테스트에 “넛지 경로 사망 = 회귀”로 읽히지 않도록 **의도**로 명시한다.

#### 3.2.2 D5 — done ∧ inbox>0: **AgentLoop 안에서만**

`loop.py:105`: **The ONLY inbox consumer is step()**.  
Session은 `inbox_size()`만 가능 → drain/판정은 **Loop**.

**단일 플래그 주입** (`Session.run_agent` while — **`_inject_protocol_gate_state` 밖**):

```text
_inject_protocol_gate_state(agent, self.protocol)
agent.protocol_done = (
    self._is_gate_waiting() and self.protocol.is_agent_done(agent.agent_id)
)
# AgentLoop.__init__: self.protocol_done = False
```

Loop 조건은 **`if self.protocol_done:` 하나**. waiting 재추론 금지.  
(§3.4 sleep은 done과 무관 → `gate_open` 추론 유지.)

`AgentLoop.step()` — **순서 확정**:

```text
drained = await drain_inbox(...)

# ── D5: drain 직후 · attention 분기 직전 ─────────────────────────
if self.protocol_done:
    if drained:
        self.conversation.append({
            "role": "user",
            "content": format_radio_block(drained),   # always full; digest 금지
        })
    if not needs_model_reply(drained):
        return StepResult(skipped=True, drained_count=len(drained), …)
    # needs_model_reply: attention 분기 전체 bypass
    # → engage 동등 처리 (이미 full radio append됨) → backend.complete
    # "fall through" ≠ attention 분기로 떨어짐
else:
    # 기존 attention / non-attention 경로 (T0 폐기 가능 — D5와 경로 분리)
    if drained and self._attention_policy is not None:
        ...
    elif drained:
        append format_radio_block(drained)
```

**못 박기 (두 줄):**

1. D5 블록은 **drain 직후, attention 분기 직전**.  
2. `protocol_done`이면 attention 분기 **전체를 건너뛴다**. skip이든 complete든  
   항상 full `format_radio_block` (rev.4 표의 T0 폐기 금지와 동일).

`if drained:` — 빈 리스트면 `format_radio_block([])` → `"[radio]"` 고아 줄 방지.

**T0와의 차이 (메시지 보존):**

| | T0 ignore | D5 skip |
|--|-----------|---------|
| 의도 | 관련 없는 메시지 **폐기** 가능 | 게이트 대기 중 **이력 보존** |
| 기본 | `t0_digest=False`면 conversation 미반영 | **항상** full radio (`if drained`) |
| digest | opt-in 축약 | **금지** |
| 코드 경로 | attention 분기 **안** | attention **밖**(앞) |

REJECT/페이즈 전환으로 깨어난 다음 complete가 “그사이 APPROVE·논의”를 보려면  
inbox에서만 빠지고 대화에 안 남으면 안 된다.  
토큰: 어차피 다음 complete에서 읽을 내용 → 추가 비용 ≈ 0.

`needs_model_reply` V1 (**human 축소**):

| 조건 | complete? |
|------|-----------|
| content `URGENT:` / `(URGENT)` | 예 |
| `mentions` ∋ self | 예 |
| `author=="human"` **∧** (멘션 self **또는** URGENT) | 예 |
| `author=="human"` alone | **아니오** |
| 동료 APPROVE/잡담 | 아니오 (D5 본체; **radio는 conversation에 남음**) |

#### 3.2.3 역할 분담

| 층 | 역할 |
|----|------|
| **Session while 최상단** | inject (`protocol_done` = waiting∧done); `protocol_done`∧inbox==0 → park (+ `steps_done`) |
| **AgentLoop.step** | `if protocol_done:` → full radio append + skipped **또는** needs_model_reply 시 complete |
| Attention T0 | not-done + low relevance; 메시지 폐기 가능 (D5와 **경로 공유 금지**) |

#### 3.2.4 Attention V1 현존 갭 (별 이슈)

비프로토콜에서 T0 skipped 후 다음 iteration이 empty-inbox로 `complete`를 한 번 도는 것은  
**지금 제품 동작**이다. done-set park가 없는 attention-only 경로의 한계.  
→ `AGENT_RELEVANCE_BUDGET` / IMPLEMENTATION_GAP에 **별 항목** (본 chatter C2와 혼동 금지).

### 3.3 Wait-narration

루프 연료 아님. V1 미구현. V1.1 표시 필터만.

### 3.4 sleep / `true` → `idle_not_allowed`

gate-wait 중 `run_command`의 `sleep`/`true`/`:`. 게이트 open 시 미적용.

### 3.5 체크포인트 `ready_states` + 참조 수명

```text
snapshot: "ready_states": sorted(self._ready_states)
```

**restore footgun:** `self._ready_states = set(...)` / `gate.approvals = {...}`는  
에이전트에 꽂아 둔 **옛 set 참조를 끊는다.**

의도를 둘 중 하나로 고정 (문서·코드 주석):

1. **권장 운영:** 참조는 **한 step만** 유효 — `_inject_protocol_gate_state`가  
   **매 while iteration** 재주입 (현 inject 자리). restore 재바인딩 OK.  
2. **또는** restore를 in-place: `self._ready_states.clear(); update(...)`  
   (approvals도 동일) → 장기 참조도 안전.

공개 API:

```text
@property
def ready_states(self) -> AbstractSet[str]: ...   # 읽기 전용 뷰
# has_ready(agent_id) 기존 유지 — 대칭
```

에이전트 주입은 `protocol.ready_states` (property)를 쓰고 `_ready_states` 직접 접근 금지.

---

## 4. 설계 2 — UI + 중복 표 no-op

### 4.1 Wire `session.gate`

(필드 동일.) 구독은 **게이트 구독 이후** 또는 `on_vote` — 순서 테스트 고정.

### 4.2 Ink / 관측 — **C0 분할** (C2가 화면을 죽이기 때문)

#### 왜 쪼개나

C2+D5는 §1의 “게이트 안 열림 착각”을 **악화**시킨다:

```text
result.skipped → continue          # session.py ~1233 — _output_queue.put 미도달
최상단 park    → step 자체 없음    # Ink step 로그 없음
```

예전엔 done 자리에도 “(대기)” 서술이 나와 “돌고 있음”이 보였는데,  
C2는 그걸 없앤다. 4명 중 3명이 done이면 **스텝 로그가 멈춘 것처럼** 보인다.  
관측을 담당할 C0(상태줄)을 C2 **뒤에**만 두면 그 구간이 암흑이다.

#### 택1 + 둘 다 싸면 둘 다

**(a) 권장 — C0 분할 (총량 불변, 관측 선행)**

| ID | 내용 | 시점 |
|----|------|------|
| **C0a** | Wire `session.gate` + 스키마 + `EVENT_TYPES` + 게이트 발행 | **C2 앞** |
| **C0b** | Ink `GateStatusBar` (상태줄 컴포넌트) | C2 **뒤** |

C0a는 `translate.py` 수정 **불필요** — `EVENT_TYPES`에 `"session.gate"`만 넣으면  
기존 passthrough(`translate.py` ~78–80)로 `bridge.publish_core_event`가 통과한다.  
실제 범위 = `types.py` 한 줄 + `events.schema.json` + 게이트 발행 지점.

**C0a 관측 범위** = Wire bus **직접** 구독자(헤드리스 `gateway/headless.py`,  
stdio 소비자, 테스트).  
Discord/Slack observe는 `_MIRROR_TYPES` / `_SLACK_TYPES`가 `message`(등)만이라  
`session.gate`는 버스에서 **배달 자체가 안 됨**. 보이게 하려면 타입 확장+렌더가  
별도인데, **D3 = V1 Ink만 → V1 비목표**.  
(“Discord에 왜 안 보이지” 버그 리포트 방지.)

C2 구간 암흑을 메우는 건 헤드리스·테스트·곧 오는 C0b다. 재배치 결론은 유효.

**(b) 선택 — park/skip 디버그 한 줄**

`AUGURY_DEBUG_GATE=1` (또는 Core `log` debug) 시만  
`gate_park` / `d5_skip` 한 줄. 기본 로그는 **조용** (의도).

**(a)를 기본으로 §8에 넣고, (b)는 C2에 값싼 보험으로 같이 넣어도 된다.**

#### 의도적 무음 (버그 리포트 방지)

| 채널 | C2 이후 기대 |
|------|----------------|
| Ink **step/서술 로그** | done park/D5 skip → **무음** (의도; “(대기)” 대체 아님) |
| Wire **`session.gate`** (C0a+) | 투표·pending·open 갱신 → “안 열린 게 아니라 기다리는 중” |
| Ink **상태줄** (C0b+) | 동일 스냅샷 UI |
| debug (b) | 개발자만 park/skip 가시화 |

§9에 “done 에이전트 park/skip이 사용자 step 로그에 완전 무음인지 = **의도**”를 적는다.

### 4.3 no-op + inject — **3분기 전부**

#### C1 prereq — P2 PROPOSE 지각 재검사 (D10, ready_states급)

`ConsensusGate.on_message`는 만장일치를 **APPROVE: 분기에서만** 검사한다.  
P2는 `require_proposal=True`인데 boot에서 `bind_to_thread`로 이미 묶여 있어  
PROPOSE 없이 APPROVE가 `approvals`에 들어간다.

```text
a1,a2,a3 APPROVE  → approvals=전원, has_proposal=False → 게이트 안 열림
                  → C2: 전원 protocol_done → park
누군가 PROPOSE    → _proposal_received=True, 만장일치 재검사 없음 → 여전히 닫힘
재 APPROVE 복구?  → C1 already_approved → 전송 차단
→ 영구 데드락
```

지금은 park가 없어 에이전트가 돌다가 재 APPROVE로 우연히 풀린다.  
**C1이 탈출구를 막고 C2가 재시도를 없앤다.**

근본 수정 (게이트 한곳 — 모든 호출자 경유):

```python
if content.startswith("PROPOSE:"):
    self._proposal_received = True
    self._maybe_open(message)  # APPROVE 분기와 동일 만장일치·human_pending
```

`_maybe_open` = 기존 APPROVE 분기의  
`if set(participants) <= approvals and (not require_proposal or has_proposal): …`  
블록 추출.

호출자 예외(`already_approved ∧ has_proposal`)보다 게이트 수정이 짧다.

**C1 착수 조건:** 이 `_maybe_open` 선행 (ready_states snapshot과 같은 급 prereq).

#### inject 스니펫

`_inject_protocol_gate_state(agent, protocol)`는 **모듈 함수** — `self` 없음.  
`protocol_done` 대입은 **이 함수 밖** `Session.run_agent` while 주입 자리에서.

```text
# --- _inject_protocol_gate_state(agent, protocol) ---
if gate is not None:
    agent.gate_open / thread_id / name = …
    agent.gate_approvals = gate.approvals
    agent.ready_states = protocol.ready_states
elif phase == P1_EXPLORE:
    agent.gate_open = False
    agent.gate_thread_id = None
    agent.gate_thread_name = None
    agent.gate_approvals = frozenset()
    agent.ready_states = protocol.ready_states
else:
    agent.gate_open = True
    agent.gate_thread_id = None
    agent.gate_thread_name = None
    agent.gate_approvals = frozenset()
    agent.ready_states = frozenset()

# --- Session.run_agent, inject 직후 (protocol is not None일 때만) ---
_inject_protocol_gate_state(agent, self.protocol)
agent.protocol_done = (
    self._is_gate_waiting() and self.protocol.is_agent_done(agent.agent_id)
)
# AgentLoop.__init__: protocol_done = False
```

(대안: `_inject_protocol_gate_state(..., *, gate_waiting: bool)`로 인자 추가 —  
같은 결과, 시그니처만 길어짐. **권장: 블록 밖 한 줄.**)

**첫 갈래만 채우면** stale `already_approved`. empty는 **`frozenset()`** (None 금지).

```text
APPROVE: ∧ agent_id ∈ agent.gate_approvals  → already_approved (미전송)
READY:   ∧ agent_id ∈ agent.ready_states    → already_ready
```

---

## 5. 설계 3 — work-before-vote (C4, D6 대기)

더미 작업 로그 위험 유지. 카운트는 서버 메시지 조회.

**각주 — `phase_entered_seq` 훅 없음:**  
`_setup_gate_for_phase`는 seq를 모름. seq는 `len(self._messages)` (`server.py` ~330)이고  
공개 접근자 없음. C4 착수 시 **`server.current_seq` (또는 동일) 한 줄 선행**.  
급하지 않음 (D6).

---

## 6. 설계 4 — light / off

### 6.1 YAML 정규화

| 입력 | 결과 |
|------|------|
| 키 생략 | protocol 없음 (오늘) |
| `protocol: false` / `null` | off로 정규화 (`Session.protocol=None`) |
| `protocol: true` | **ConfigError** 유지 (`must be a mapping` 또는 명시 메시지) — 표에 명시 |
| `mode: off` → 파서 `False` | off |
| `mode: "off"` / `"false"` | off |
| `mode: on` → `True` | **ConfigError** |
| `mode: full` / `light` | 그대로 |
| 기타 | ConfigError |

`mode == "off"` 문자열만 보면 bare `off`는 영원히 안 걸린다.

### 6.2 light — 하드코딩 3곳

`finish_p1` → P2, transition table, `_on_protocol_gate_open` dict.  
세 곳 모두 mode 분기.

---

## 7. 계층 (rev.6)

```text
1. Session while 최상단: _inject…; protocol_done=…; park(+steps_done)
2. AgentLoop.step: drain → D5(attention 전) → else attention/engage
3. Attention T0 (D5와 경로 분리)
4. tools: idle_not_allowed / already_approved|ready / …
5. 기존 gate-wait park (done은 최상단 — stale nudge 미도달)
```

---

## 8. 착수 순서

| 순서 | ID | 내용 |
|------|-----|------|
| 0a | **C1-prereq** | `ConsensusGate` PROPOSE 후 `_maybe_open` (D10) |
| 0b | prereq | `ready_states` snapshot/restore |
| 1 | C1 | 중복 표 no-op + inject 3분기 + `frozenset()` |
| 2 | C3-sleep | `idle_not_allowed` |
| 3 | **C0a** | `EVENT_TYPES` + 스키마 + 게이트 발행 (`translate` 불필요; Ink UI 없음) |
| 4 | **C2+D5** | while park + Loop D5; 선택: debug park/skip (b) |
| 5 | **C0b** | Ink `GateStatusBar` |
| 6 | C4 | work-before-vote + `server.current_seq` (D6) |
| 7 | C5 | mode light/off |

C0a를 C2 앞에 두는 이유: park/skip이 step 로그를 죽이므로 **투표 이벤트로 관측을 먼저** 확보.

---

## 9. 테스트

| 케이스 | 기대 |
|--------|------|
| **APPROVE×N → PROPOSE** | 게이트 **open** |
| 위 + C1 already_approved | 데드락 없음 |
| **D5 skipped 다음 iteration** | `complete` 추가 호출 없음 |
| **D5 skip 후 REJECT 웨이크** | conversation에 동료 APPROVE 본문 |
| D5 + attention.enabled | T0로 폐기 안 됨 |
| **done park/D5 skip → step 로그** | **완전 무음 = 의도** (회귀로 보고하지 말 것) |
| C0a 발행 | 투표 시 `session.gate` — **직접 구독자**(헤드리스/stdio/테스트)만; Discord/Slack V1 비목표 |
| 이미 APPROVE | gate_thread nudge 미발화 |
| max_steps + done park | `steps_done`으로 종료 |
| empty drained + protocol_done | `"[radio]"` 고아 없음 |
| protocol 없는 세션 / sleep / mode / human | 기존 |

---

## 10. 문서 연동

- IMPLEMENTATION_GAP §5.4 → rev.6 (C0a→C2→C0b).  
- AGENT_RELEVANCE: T0 후속 complete 갭 별 항목.  
- PARK §12 → C2+D5 · D10 · C0 분할.  
- **후속(범위 밖, 각주):** 파킹 중 **살아있는 not-done 에이전트 0명**이면  
  `_wait_for_gate_wakeup`이 interrupt/`_closed`/max_steps/inbox/phase 외로  
  안 깨어나 `gather`가 안 끝남 (기존 PARK 성질; C2가 park 지점을 늘려 노출↑).  
  → “not-done 잔여 0 → 파킹 해제/세션 종료”는 **별 이슈**. 본 chatter V1 비목표.  
- **성공 후 턴 미종료** (COMPLETED인데 나레이션으로 `run()` 유지 · 상태줄 3/4 고착)는  
  chatter 밖 — [`SESSION_TURN_TERMINATION_DESIGN.md`](./SESSION_TURN_TERMINATION_DESIGN.md).

---

## 11. 열린 결정

| # | 기본값 (rev.6) |
|---|----------------|
| D1 wait-narration | V1.1 표시만 |
| D2 light 위저드 | full |
| D5 human | 멘션∨URGENT만 |
| D5 메시지/위치 | full radio; drain 직후·attention 전 |
| D5↔C2 | 분리 불가 |
| D6 C4 | 실측 후; `current_seq` 선행 |
| D7 restore | 매 step 재주입 |
| D8 T0 갭 | 별 이슈 |
| D9 empty approvals | `frozenset()` |
| D10 | C1 전 PROPOSE→`_maybe_open` |
| **D11** | **C0a 선행 + C0b 후행**; step 무음은 의도; (b) debug 선택 |
| D12 | not-done 0명 park 행 → PARK/세션 후속 (범위 밖) |

---

## 12. 요약

rev.6: C2는 step 로그를 죽인다 → **`session.gate`(C0a)를 먼저** 깔고,  
Ink 상태줄(C0b)은 나중에. 무음은 버그가 아니라 설계.  
마지막 일꾼 사망 시 park 행은 알고만 두고 여기선 안 고친다.
