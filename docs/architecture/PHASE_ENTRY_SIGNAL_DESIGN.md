# Phase entry signal — 페이즈에 이름이 아니라 내용을 주기

> **Status:** **E1~E5 구현 완료** (M2만 보류)
> **Date:** 2026-09-16
> **Priority:** P2 (정확도 아님 — 프로토콜이 의미를 갖게)
> **Parent:** `DESIGN.md` §2.3 (P1~P5), `core/protocol/approval.py`
> **인접:** `PROTOCOL_CHATTER_REDUCTION_DESIGN.md` §5 (C4 work-before-vote, D6 보류),
>   `SESSION_TURN_TERMINATION_DESIGN.md`
> **Code touch (예정):** `protocol/approval.py`, `protocol/collaboration.py`,
>   `core/session.py`, `agent/loop.py`, `agent/system_prompt.py`
> **Tests (예정):** `tests/test_phase_entry_signal.py`
> **결정:** M1 `one(prefix)` 채택 · M2 `all(prefix)`는 실측 후 · M3 어셈블러 삭제

---

## 0. 한 줄

게이트마다 **"열리려면 무엇이 먼저 있어야 하는가"** 를 선언하고,
없으면 **투표 자체를 툴 계층에서 막는다.**
P5는 `FINAL:` 초안 없이는 열리지 않는다.

---

## 1. 배경 — 실측

수학 P1–P5 실사용(`01c9b8a3`, 33메시지 / 62스텝)의 게이트 로그:

```text
seq  4-10  thread-1 plan        PROPOSE ×2, APPROVE ×4   → open(10)
seq 11-16  thread-2 execution   작업로그 2, APPROVE ×4    → open(16)
seq 17-22  thread-3 review      APPROVE ×2 → PROPOSE ×2 → APPROVE ×2 → open(22)
seq 23-32  thread-4 submission  초안 3, PROPOSE 3, APPROVE ×4 → open(32)
```

답(165)은 맞았다. 게이트도 정직하게 4/4씩 열렸다. **문제는 페이즈가 서로 구별되지 않는다는 것.**

| 증상 | 근거 |
|------|------|
| P4(교차검토)에서 **검토 대상보다 승인이 먼저** | seq 17·18 APPROVE가 19·20 PROPOSE보다 앞 |
| P3(실행)에 **분담이 없음** | seq 11·12는 같은 최종답을 통째로 재게시 |
| `RESULT:` / `FINAL:` **미사용** | `collaboration.py:25-26` 주석에만 존재. 읽는 코드 0줄 |
| 어셈블러 **없음** | `assembler_id`는 저장만 되고 미사용. 프롬프트는 `"no fixed assembler role"` 로 개념을 스스로 취소 |

`DESIGN.md` §2.3이 정의한 각 페이즈의 *내용*(분할 협상 / 자기 몫 수행 / 근거 방송 / 초안 조립)을
코드는 **하나도 확인하지 않는다.** 확인하는 것은 "전원 APPROVE" 하나뿐.

### 1.1 현재 게이트가 아는 것

```python
# approval.py — 제안 신호가 PROPOSE: 로 하드코딩
if content.startswith("PROPOSE:"):
    self._proposal_received = True
    self._maybe_open(message)
...
elif content.startswith("APPROVE:"):
    if not self.require_proposal:
        self._proposal_received = True   # P3+ 는 첫 표가 제안을 대신
```

`bind_prefixes`는 **사실상 죽은 코드**다 — `on_message`에서 `thread_id is None`일 때만 쓰이는데
Session이 boot에서 `bind_to_thread()`로 미리 묶는다.

---

## 2. 목표 · 비목표

### 2.1 목표

1. 게이트가 **진입 신호(entry signal)** 를 선언한다: "이 접두사가 먼저 와야 표가 의미를 갖는다".
2. **P5 = `FINAL:` 초안 1개 + 전원 APPROVE** — P4와 구조적으로 달라진다.
3. 신호 없이 `APPROVE:` 하면 **툴 계층에서 소프트 차단** (프롬프트는 최후 수단).
4. `bind_prefixes` 죽은 코드 정리.

### 2.2 비목표 (V1)

- P3 실행 내용 검증 (C4 work-before-vote — chatter §5, D6 보류 유지)
- 분할이 **실제로** 나뉘었는지 판정 (NLP 필요)
- 페이즈별 역할 자동 배정
- P4 `all(RESULT:)` — **M2로 분리** (§5)

---

## 3. 설계 — entry signal

### 3.1 모양

게이트가 여는 조건은 두 축이다.

```text
open ⟺ (entry signal 충족) ∧ (전원 APPROVE)
```

entry signal 종류:

| 종류 | 뜻 | 쓰는 곳 |
|------|-----|---------|
| `none` | 신호 불필요 | P3 (자유 작업) |
| `one("PROPOSE:")` | **누군가 하나** | P2 (분할안 하나) |
| `one("FINAL:")` | **누군가 하나** | **P5 (최종 초안 하나)** |
| `all("RESULT:")` | **전원 각자 하나** | P4 — **M2** (§5) |

V1은 `none` / `one(prefix)` 만. `all`은 M2.

### 3.2 `ConsensusGate` 변경

`require_proposal: bool` 은 **유지**(호환), 접두사만 주입 가능하게:

```python
def __init__(
    self,
    server, thread_name, *,
    require_proposal: bool = True,
    entry_prefix: str = "PROPOSE:",      # 신규 — require_proposal일 때 필요한 접두사
    await_human_after_agents: bool = False,
) -> None:
    ...
    self.entry_prefix = entry_prefix
```

`on_message`의 하드코딩을 치환:

```python
if content.startswith(self.entry_prefix):
    self._proposal_received = True
    self._maybe_open(message)
elif content.startswith("REJECT:"):
    ...
elif content.startswith("APPROVE:"):
    ...
```

**`bind_prefixes` 삭제** — 죽은 코드(§1.1). `bind_to_thread` 경로만 남긴다.

체크포인트: `entry_prefix`는 **설정이지 상태가 아니다.** `snapshot()`에 넣지 않는다
(게이트는 config로 재구성되고 `restore_state`가 `approvals`/`proposal_received`만 채운다).

### 3.3 페이즈별 기본값 (Session)

`session.py`의 `require_proposal = phase == P2_SPLIT` 자리를 표 하나로:

```python
_PHASE_GATE_ENTRY: dict[Phase, tuple[bool, str]] = {
    P2_SPLIT:   (True,  "PROPOSE:"),
    P3_EXECUTE: (False, ""),          # 자유 작업 — 신호 없음
    P4_REVIEW:  (False, ""),          # M2에서 all("RESULT:")
    P5_SUBMIT:  (True,  "FINAL:"),    # ← 이번 변경의 본체
}
```

`mode: light`는 P1 → P5 이므로 **`FINAL:` 가 그대로 적용**된다 (가벼운 세션에도
"초안 → 승인"은 남는다).

### 3.4 소프트 차단 — `entry_signal_required`

신호 없이 표를 던지면 **전송 자체를 막는다.** `gate_closed` / `already_approved`와 같은 계층.

Session이 스칼라 **하나**만 주입 (Loop에서 재추론 금지 — chatter §3.2.2와 동일 원칙):

```python
# _inject_protocol_gate_state
agent.gate_needs_signal = (
    gate.entry_prefix
    if (gate.require_proposal and not gate.has_proposal)
    else None
)
```

`AgentLoop._duplicate_signal_denied` 옆에서:

```python
if content.startswith("APPROVE:") and self.gate_needs_signal:
    return {
        "error": "entry_signal_required",
        "phase": ...,
        "needs": self.gate_needs_signal,          # 예: "FINAL:"
        "message": "This gate has no FINAL: draft yet. Post the final answer "
                   "starting with FINAL: (or approve one someone else posted).",
    }
```

효과:

- 표가 **쌓이지 않는다** → 지난 실행처럼 `4/4 · awaiting PROPOSE:` 라는 혼란스러운 상태가 애초에 안 생김.
- 모델은 **틀린 행동을 한 그 순간** 정확히 무엇이 빠졌는지 듣는다.
- `is_agent_done`의 `require_proposal ∧ ¬has_proposal → False` 가드(데드락 수정)는
  **그대로 둔다** — 이중 방어. REJECT 이후 등 경로가 남는다.

### 3.5 프롬프트 (보조)

`_PHASE_INSTRUCTIONS["P5_SUBMIT"]`:

```text
- Anyone may compose the final answer — there is no fixed assembler.
- Post it prefixed `FINAL:` on the gate thread. The gate cannot open without it.
- Then everyone approves with `APPROVE:` (or `REJECT:` to redo).
```

`_phase_instructions_with_gate`의 고정 문구 `"send PROPOSE: / APPROVE: only to this thread"`를
**entry_prefix 인자화**:

```text
While the gate is closed, send `{entry_prefix}` / `APPROVE:` only to this thread id.
```

`_maybe_nudge_gate_thread`도 동일하게 무엇이 빠졌는지 말하게 한다.

---

## 4. 왜 P5가 P4와 달라지나

| | P4_REVIEW (현행 유지) | P5_SUBMIT (변경 후) |
|--|----------------------|---------------------|
| 열리는 조건 | 전원 APPROVE | **`FINAL:` 1개** + 전원 APPROVE |
| 첫 APPROVE | 제안을 대신함 | **차단됨** (`entry_signal_required`) |
| 산출물 | 없음(투표만) | 스레드에 **최종 답 본문이 반드시 남음** |

지난 실행에서 P5 스레드에 초안이 있긴 했다(seq 23·24·30). 다만 **우연**이었고,
게이트는 그것 없이도 열렸을 것이다. 이 변경 후에는 **없으면 못 연다.**

---

## 5. M2 (별도 결정) — P4 `all("RESULT:")`

§2.3의 P4는 "각자 결과를 근거와 함께 방송"이다. 이는 `one`이 아니라 **전원 기여**다.

```python
# ConsensusGate
self.contributions: set[str] = set()      # approvals와 대칭

if content.startswith(self.entry_prefix) and author in self.participants:
    self.contributions.add(author)
...
# _maybe_open 조건에 추가
if self.entry_mode == "all" and not (set(self.participants) <= self.contributions):
    return
```

- `approvals`와 같은 게이트 내부 상태 → `snapshot()`에 `contributions` 한 줄 추가.
  **새 병렬 set 아님** (게이트가 SSOT).
- C4(work-before-vote)와의 차이: C4는 "아무 비시그널 메시지"라 **더미 로그**를 유도한다
  (chatter §5.1). `all("RESULT:")`는 **타입이 있는 기여**라 그 위험이 작다.
- 위험: 한 명이 `RESULT:`를 안 내면 P4가 안 열린다 → 데드락. `is_agent_done` 가드를
  `entry` 미충족 전반으로 확장해야 함(§3.4와 동일 이유).

**D1: M1 실측 후 결정.** M1만으로 P5가 의미를 가지면 M2는 안 해도 된다.

---

## 6. M3 — 어셈블러 정리

현황: `assembler_id` / `bind_assembler()` 는 저장·`status()` 보고만. 동작에 영향 0.
프롬프트는 `"there is no fixed assembler role"` 로 이미 부정.

**결정: 삭제 (완료).** 설계 당시 예상보다 표면이 넓었다 — 내부 dead code 뿐 아니라
`examples/*.yaml`의 **사용자 설정 키** `protocol.assembler_id` 까지 포함.
동작 영향은 0이라 그대로 삭제했고, 기존 설정에 남아 있어도 조용히 무시된다.
`tests/test_wizard.py`의 `assert "assembler_id" not in cfg["protocol"]` 가 회귀 가드로 남는다.
 `DESIGN.md` §2.3의 "어셈블러가 스레드를 개설하고…" 문장도 현행에 맞게 고친다
(스레드는 Session이 boot에서 생성, 조립은 아무나).

남길 이유가 있다면(역할 기반 확장 대비) 그건 **YAGNI** — 필요해질 때 다시 넣는 게 싸다.

---

## 7. 마일스톤

| 순서 | ID | 내용 | 완료 조건 |
|------|-----|------|-----------|
| 1 | **E1** | `entry_prefix` 주입 + `bind_prefixes` 삭제 | P2 기존 동작 불변 |
| 2 | **E2** | `_PHASE_GATE_ENTRY` 표 · P5 = `FINAL:` | `FINAL:` 없이 P5 안 열림 |
| 3 | **E3** | `gate_needs_signal` 주입 + `entry_signal_required` 소프트 차단 | 조기 APPROVE 미전송 |
| 4 | **E4** | 프롬프트·넛지 문구 entry_prefix 인자화 | P5 지시에 `FINAL:` 등장 |
| 5 | **E5** | M3 어셈블러 삭제 + `DESIGN.md` §2.3 갱신 | grep `assembler` = 0 |
| — | M2 | P4 `all("RESULT:")` | D1 결정 후 |

E1~E4는 한 PR로 묶어도 된다(같은 축). E5는 독립.

---

## 8. 테스트

| 케이스 | 기대 |
|--------|------|
| P2: `PROPOSE:` + 전원 APPROVE | open (기존 동작 불변) |
| P5: 전원 APPROVE, `FINAL:` 없음 | **안 열림** |
| P5: 전원 APPROVE 시도 시 | 전부 `entry_signal_required`, 메시지 미적재 |
| P5: `FINAL:` → 전원 APPROVE | open |
| P5: 전원 APPROVE 후 `FINAL:` 지각 | `_maybe_open`으로 open (D10 경로 유지) |
| `mode: light` | P5에 `FINAL:` 적용 |
| P3 | 신호 없음 — 첫 APPROVE로 진행 (기존) |
| `is_agent_done` | entry 미충족이면 전원 False (데드락 가드 유지) |
| 체크포인트 | `entry_prefix`는 스냅샷에 없음; 재구성으로 복원 |
| 프롬프트 | P5 블록에 `FINAL:`, 게이트 힌트도 `FINAL:` |

---

## 9. 열린 결정

| # | 질문 | 제안 |
|---|------|------|
| D1 | P4 `all("RESULT:")` 채택? | M1 실측 후 |
| D2 | `require_proposal` 이름 유지 vs `entry_required` 개명 | **유지** (호환·diff 최소) |
| D3 | `entry_signal_required` vs 표 허용 후 미개방 | **차단** — 혼란 상태를 만들지 않음 |
| D4 | P2도 `FINAL:`처럼 본문 강제? | 아니오 — `PROPOSE:` 가 이미 그 역할 |
| D5 | 어셈블러 삭제 vs 보존 | **삭제** (§6) |

---

## 10. 요약

게이트에 **"무엇이 먼저 와야 하는가"** 를 한 줄 선언으로 주고(`entry_prefix`),
없으면 **표를 받지 않는다**(`entry_signal_required`).
P5는 `FINAL:` 초안 없이 못 열리므로 **제출 스레드에 최종 답이 반드시 남는다** —
이것이 P5를 P4와 구별하는 최소 변경이다.
P4 전원 기여(`RESULT:`)와 어셈블러 삭제는 분리해서 판단한다.
