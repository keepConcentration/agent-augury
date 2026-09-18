# 후속 턴에도 프로토콜 — `light` 한 라운드 더

> **Status:** **구현 완료** (2026-09-18)
> **Date:** 2026-09-17
> **Priority:** P2 (정확도. 후속 답이 검증을 못 받는다)
> **Parent:** `SESSION_TURN_TERMINATION_DESIGN.md` §8 (spent 프로토콜), `DESIGN.md` §2.3
> **인접:** `PHASE_ENTRY_SIGNAL_DESIGN.md` §6b (M4a·M4c, FINAL: 진입 신호)
> **Code touch:** `protocol/collaboration.py` (`begin_round`), `protocol/approval.py` (`reset_for_round`),
>   `core/session.py` (`_run_impl`), `agent/system_prompt.py` (터미널 블록)
> **Tests:** `tests/test_session_turn_termination.py`
> **결정:** **`full` 재실행 아님 — `light`(P1→P5)** (§2). 게이트는 **제자리 리셋** (§4.3)
> **D1:** **항상** — 새 질문이 있으면 무조건 새 라운드 (§8.1)

---

## 0. 한 줄

후속 턴은 이미 **P1을 하고 있다.** 빠진 것은 그 뒤의 **P5** — 합치고 서로 승인하는 단계다.

---

## 1. 문제 — 후속 답은 아무도 안 읽는다

세션 `ffb70b4b` 후속 턴(BSD 선행조건 질문). 서버 메시지 **0건**, 스텝 8.
네 에이전트가 각자 긴 답을 쓰고 **끝났다.**

`SESSION_TURN_TERMINATION` §8.3 의 조치(터미널 페이즈 블록)가 의도대로 동작한
결과다. 프로토콜 흉내와 이전 답 유출이 사라졌다(39스텝·16메시지 → 8스텝·0메시지).
**대신 교차 검증도 같이 사라졌다.**

실제로 값을 잃었다. agent-1 의 답에만 있던 오류들을 **다른 두 에이전트는 바르게
썼는데** 아무도 보지 않았다.

| agent-1 | agent-2 / agent-4 | 실제 |
|---------|-------------------|------|
| **닐슨**-타이트 높이 | 네론-타이트 | Néron-Tate |
| 바이어슈트래스 높이 `ĥ(P)` | — | `ĥ` 는 네론-타이트다 |
| **아인슈타인** 급수 | — | 아이젠슈타인 |
| **헤켈** 연산자 | 헤케 | Hecke |
| 군 구조 "**할아버스키**" | — | 조어 |
| Mazur-Wiles (1984) | (1974) | 1984 |

같은 세션의 **첫 턴**에서는 P4/P5 가 실제로 잡아냈다 — agent-1 이 존재하지 않는
`agent-5` 배정을 `REJECT:` 로 막았고(seq 7), agent-2 가 Gross-Zagier 공식의 분모
누락을 짚었다(seq 32). **기구가 없어서 못 잡은 것이지 모델이 못 잡는 것이 아니다.**

---

## 2. 왜 `full` 이 아니라 `light` 인가

`light` 는 **이미 구현돼 있다** (`collaboration.py`).

```python
next_phase_after_p1        -> P5_SUBMIT if mode == "light" else P2_SPLIT
next_phase_after_gate(P5)  -> COMPLETED
_valid_transitions(light)  -> {P1: {P5, REJECTED}, P5: {COMPLETED, REJECTED}}
```

즉 **P1 → P5 → COMPLETED.** 그리고 `_PHASE_GATE_ENTRY[P5_SUBMIT] = (True, "FINAL:")`
이므로 **`FINAL:` 초안 없이는 P5 게이트가 열리지 않는다.** 필요한 것이 정확히 그것이다.

### 2.1 후속 턴은 이미 P1 이다

관측된 후속 턴의 모양:

```text
agent-2 -> 선행조건 정리 (독립)     ┐
agent-1 -> 선행조건 정리 (독립)     │  = P1_EXPLORE
agent-4 -> 선행조건 정리 (독립)     │    (각자 탐색, 아직 합치지 않음)
agent-3 -> 선행조건 정리 (독립)     ┘
                                    <- 여기서 끝났다
```

`READY:` 만 붙이면 그대로 P1 이다. 설계는 **"프로토콜을 다시 태운다"가 아니라
"이미 일어나는 P1 뒤에 P5 를 붙인다"** 이고, 그래서 작다.

### 2.2 P2~P4 를 다시 도는 것은 값을 못 한다

첫 턴에서 P2 분담은 값을 했다 — 넷이 서로 다른 주제를 맡아 P3 에 분량이 쌓였다
(2,213 / 3,268 / 2,565자). 그러나 **후속 질문은 이미 나눈 주제의 연장**이다.
관측된 후속 턴에서 넷이 **같은 질문에 각자 통째로 답한 것**이 그 증거다 —
나눌 것이 없었다.

### 2.3 비용

| | 현재 | **`light` 재시작** | `full` 재시작 |
|--|-----:|------:|------:|
| 서버 메시지 | 0 | **~8** | ~39 |
| 스텝 | 8 | ~12–16 | ~81 |
| 산출 | 답 4개 (검증 없음) | **답 1개 (3표 검증)** | 답 1개 |

`light` 의 8메시지는 P1 `READY:` ×4 + P5 `FINAL:` ×1 + `APPROVE:` ×3 이다.
최근 실행들에서 P5 는 **정확히 4메시지**(이론상 최소)로 닫혔다.

---

## 3. 이미 갖춰진 것

`light` 라운드는 오늘 넣은 방어를 **그대로 물려받는다.**

| 기구 | 이 설계에서의 역할 |
|------|-------------------|
| M4a (`draft_already_posted`) | `FINAL:` 초안 하나. 넷이 각자 제출하는 것을 막는다 |
| M4c (초안 = 작성자 자표) | 작성자가 자기 표를 또 기다리는 교착 제거 |
| `gate_already_open` (§6b.11) | 승인 끝난 뒤 초안 추가 차단 |
| `_unsent_signal_nudge` (§6b.9) | **P1 `READY:`** 를 말로만 하고 안 보내는 것 |
| `empty_message` | 빈 브로드캐스트 |
| `submitter_id` 없음 | light 에는 P2 가 없다 → "아무나 쓰라" + M4a 선착순 |

**새로 만들 기구가 거의 없다.** 필요한 것은 **라운드를 다시 여는 방법**뿐이다.

---

## 4. 설계 — 라운드 리셋

### 4.1 지금 막고 있는 것

```python
# session.py:1189
protocol_spent = bool(
    self.protocol and self.protocol.phase in (COMPLETED, REJECTED)
)
```

이 가드는 **의도적으로 넣은 것**이다(`694182e`). 없으면 후속 질문이
`session: 0 steps` 로 죽었다. 따라서 이 설계는 가드를 **제거**하는 것이 아니라
*"재시작 안 함"* 에서 *"새 `light` 라운드 시작"* 으로 **바꾸는** 것이다.

### 4.2 전이 — `advance()` 로는 못 나간다

```python
_valid_transitions: { ..., COMPLETED: set(), REJECTED: set() }   # 양 모드 공통
```

`advance(P1_EXPLORE)` 는 `ValueError` 다. 한편 `phase_manager.restore()` 는
**콜백을 안 쏜다** — Wire 의 `session.phase` 가 갱신되지 않아 상태줄이 언다
(`SESSION_TURN_TERMINATION` 에서 이미 겪은 증상).

→ 명시적 진입점이 필요하다: `CollaborationProtocol.begin_round(mode="light")`.
전이는 `advance()` 를 타되 이 메서드만 터미널 → P1 을 허용한다.

### 4.3 게이트는 **제자리에서 리셋**한다 (새로 만들지 않는다)

`MessageServer` 에 **`unsubscribe` 가 없다**(`server.py:459` 에 `subscribe` 만 존재).
`bind_gate()` 는 매번 `server.subscribe(gate.on_message)` 를 한다. 따라서 게이트를
다시 만들면 **옛 게이트가 계속 구독된 채로** 남아 표를 이중으로 센다.

→ 선택지가 아니라 **강제**다. `ConsensusGate.reset_for_round()` 를 만든다.

```python
def reset_for_round(self) -> None:
    self._reset_for_redo()      # approvals · draft_author · _proposal_received · human_pending
    self.opened_at_seq = None   # _reset_for_redo 가 안 건드리는 유일한 항목
```

`_reset_for_redo()` 가 이미 4개를 지운다. **한 줄이 모자랄 뿐이다.**

프로토콜 쪽에서 같이 비울 것:

| 항목 | 이유 |
|------|------|
| `_gate_open_fired` | 안 비우면 `_handle_gate_open` 이 조기 반환해 **게이트가 열려도 페이즈가 안 넘어간다** |
| `_assignments` / `submitter_id` | light 에는 P2 가 없어 새로 채워지지 않는다. 지난 턴 값이 남으면 §8.1 재발 |
| `_ready_states` | `finish_p1()` 이 이미 비우지만 중단된 라운드를 대비 |

### 4.4 스레드는 재사용해도 안전하다

게이트 구독은 **미래 메시지만** 받는다(재생 없음). 리셋된 게이트가 `thread-4` 에
다시 붙어도 지난 턴의 `APPROVE:` 를 다시 세지 않는다.

남는 문제는 **읽는 쪽**이다 — 한 스레드에 두 라운드가 섞이면 `read_resource` 와
사람 눈이 헷갈린다. 라운드마다 새 스레드를 만들면 깨끗하지만, 시스템 프롬프트가
열린 스레드 목록을 통째로 싣기 때문에 **모든 프롬프트가 라운드마다 불어난다.**
→ **재사용 채택** (D4, §8.2). 비용은 가독성뿐이고 정확성은 영향받지 않는다.

### 4.5 프롬프트 — 방금 넣은 블록과 충돌한다

`SESSION_TURN_TERMINATION` §8.3 이 넣은 COMPLETED 블록은 이렇게 말한다.

```text
- Do NOT re-run the protocol, and do not repeat the previous answer.
- Answer the user's new question directly in your reply.
```

재시작을 켜면 **이 문장이 정반대**가 된다. COMPLETED 는 이제 *"라운드 사이"* 를
뜻하고, 새 질문이 오면 **에이전트가 스텝을 밟기 전에** P1 로 간다.

→ 조건부로 나누지 않고 **블록 자체를 다시 썼다** (D6, §8.2). 새 문구는 재시작
여부와 무관하게 참이다.

```text
Current phase: **COMPLETED** - this round is finished and the answer is in.
- The gate threads are closed history. ... no gate is listening.
- Say nothing further and do not repeat the answer. A new question from the
  user opens a new round on its own.
```

에이전트가 이 블록을 보는 구간은 이제 **게이트가 열린 뒤 턴이 끝나기 전**뿐이다.

---

## 5. 흐름

```text
[턴 N 종료]  phase = COMPLETED
                │
    새 사용자 메시지
                │
                ▼
    begin_round(mode="light")
      ├─ 게이트 reset_for_round()           (구독은 유지)
      ├─ _gate_open_fired / assignments / submitter_id 비움
      └─ advance(P1_EXPLORE)                (콜백 발화 -> Wire 갱신)
                │
                ▼
    P1_EXPLORE   각자 탐색 -> READY: ×4      <- 지금도 일어나는 일
                │
                ▼
    P5_SUBMIT    FINAL: ×1 + APPROVE: ×3
                 REJECT: 면 초안 리셋 후 재작성    <- 첫 턴에서 두 번 성사됨
                │
                ▼
    COMPLETED    답 하나, 표 네 장
```

---

## 6. 실패 모드

| | 증상 | 이미 있는 방어 |
|--|------|---------------|
| P1 이 3/4 로 정지 | `READY:` 를 말로만 함 | `_unsent_signal_nudge` (§6b.9) |
| `FINAL:` 을 아무도 안 씀 | P5 가 안 열림 | `entry_signal_required` + `is_agent_done` 가드 |
| 초안 경쟁 | 넷이 각자 `FINAL:` | M4a 선착순 |
| 무한 재작성 | REJECT ↔ FINAL 반복 | **없음 — D7** |
| 한 줄 질문에 8메시지 | "뭐해" 에도 라운드 | **D1** |

---

## 7. 비목표

- **`full` 재실행** — §2.2. 후속 질문에 P2 분담은 값을 못 한다.
- **질문 길이·종류로 자동 판정** — NLP 금지. 휴리스틱은 틀렸을 때 설명이 안 된다.
- **대화 이력 절단** — 후속 질문이 이전 답을 참조해야 한다.
- **`unsubscribe` 도입** — §4.3 의 제자리 리셋이면 필요 없다. 필요해지면 그때.

---

## 8. 결정

### 8.1 D1 — **항상** 재시작한다 (새 질문이 있을 때)

설정 키를 새로 만들지 않는다. 근거 셋:

1. **순증이 작다.** 현재도 후속 질문에 네 에이전트가 각자 답한다(관측 8스텝).
   `light` 라운드는 거기에 `READY:` ×4 와 P5 왕복 하나를 더할 뿐이고,
   산출은 **검증 안 된 답 4개 → 3표 받은 답 1개**로 바뀐다.
2. **프로토콜을 켠 사람은 이미 이 값을 산 것이다.** 첫 턴에만 적용하고 그 뒤로는
   끄는 동작을 원한 사람이 아직 없다. 없는 요구에 설정 키를 만들지 않는다.
3. **되돌리기 쉽다.** 키를 나중에 더하는 것은 작은 변경이고, 있던 키를 빼는 것은
   어렵다.

**트리거는 "새 질문이 있는가" 하나다.** `user_text` 가 비면(프롬프트 없는 단순
resume) 라운드를 열지 않는다 — 물어본 것이 없으면 합의할 것도 없다.

`protocol` 이 꺼진 세션은 자동으로 해당 없음(`self.protocol is None`).

### 8.2 나머지

| # | 결정 | 근거 |
|---|------|------|
| D2 | **`light` 고정** | 후속 질문은 이미 나눈 주제의 연장 (§2.2). `mode` 는 세 메서드가 매번 읽으므로 대입 한 줄로 바뀐다 |
| D3 | **제자리 리셋** | `unsubscribe` 부재 (§4.3). 선택이 아니라 강제 |
| D4 | **스레드 재사용** | 라운드마다 새로 만들면 프롬프트의 스레드 목록이 무한히 는다. 게이트 구독은 미래 메시지만 받으므로 지난 라운드 표를 다시 세지 않는다. 비용은 한 스레드에 두 라운드가 섞이는 **가독성**뿐 |
| D5 | **`begin_round()` 전용 진입점** | `advance()` 는 터미널에서 못 나간다. `phase_manager.advance` 로 콜백을 살려 Wire 갱신 유지 |
| D6 | **터미널 블록 재작성** | "프로토콜을 다시 돌리지 마라"가 거짓이 됐다. 이제 *"이 라운드는 끝났다. 새 질문은 알아서 새 라운드를 연다"* |
| D7 | **재작성 상한 없음** | 관측된 재작성은 2회에 수렴. `max_steps` 가 이미 전체를 묶는다. 실제로 새면 그때 |
| D8 | **라운드 번호 없음** | 체크포인트는 리셋된 상태를 그대로 담는다. 필요해진 적 없다 |

---

## 9. 알려진 위험 — D12 노출이 넓어진다

`_wait_for_gate_wakeup` 에는 **생존성 검사가 없다**(`session.py`, docstring 이
*"Does not end the turn merely because every agent is idle"* 라고 명시). 모든
에이전트가 P1 에서 `READY:` 없이 park 하면 턴이 끝나지 않는다.

이것은 **새 버그가 아니다.** 첫 턴도 똑같이 멈춘다 — 확인했다.

```text
1에이전트 · 프로토콜 on · READY: 를 절대 안 보내는 백엔드
  -> TURN-1 HUNG (calls=2, phase=P1_EXPLORE)
```

다만 지금까지는 **턴 1에서만** 닿을 수 있던 경로가 이제 **모든 후속 질문에서**
닿는다. 실사용에서는 `_maybe_nudge_ready`(§6b.9 넛지)와 `max_steps` 가 막아
왔고 관측된 세션은 모두 종료됐지만, **노출 면적이 늘었다는 사실은 그대로다.**

고치려면 park 에 "살아 있는 에이전트가 전부 park 중이면 턴을 닫는다"를 넣어야
하는데, 그러면 **사람이 게이트를 깨워 주는 창**이 사라진다 — 실사용 로그에서
사용자가 *"@agent-4 approve 너만 남았어"* 로 구해 준 그 창이다.
**둘 중 하나를 고르는 결정이므로 D12 로 따로 남긴다.**

---

## 10. 요약

후속 턴은 **이미 P1 을 하고 있다** — 넷이 각자 답을 쓴다. 빠진 것은 그것을 하나로
합치고 서로 승인하는 **P5** 뿐이고, 그 경로(`light`: P1→P5→COMPLETED)는
**이미 구현돼 있다.** 초안 하나(M4a) · 작성자 자표(M4c) · 승인 후 초안 차단 ·
`READY:` 넛지까지 오늘 넣은 방어를 그대로 물려받으므로, **새로 만들 기구는
라운드를 다시 여는 방법 하나**다.

구현하면서 걸린 곳은 예고한 둘 — **`unsubscribe` 부재**(게이트는 제자리 리셋)와
**COMPLETED 프롬프트 블록**(정반대 지시가 되어 다시 씀) — 그리고 예고하지 못한
하나, **`_gate_open_fired`** 다. 이것을 안 비우면 게이트가 열려도 페이즈가
넘어가지 않는다.

남은 위험은 **D12**(§9): 전원이 P1 에서 park 하면 턴이 끝나지 않는 경로가
이제 모든 후속 질문에서 닿는다. 첫 턴에도 있던 것이지만 노출은 넓어졌다.
