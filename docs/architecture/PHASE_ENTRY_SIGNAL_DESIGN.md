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

## 6b. M4 — 초안은 하나, 작성자는 P2에서 정한다

> **Status:** 설계만 (M4a 우선, M4b는 그 위)

### 6b.1 문제 (실측)

E1~E4 적용 후 첫 실행(98 문제) P5 스레드:

```text
agent-4 -> FINAL: ... = 98
agent-3 -> FINAL: ... = 98      <- 같은 내용
agent-2 -> FINAL: ... = 98      <- 같은 내용
agent-1 -> (FINAL 제출했다고 서술)
그 다음 APPROVE x4
```

게이트가 `FINAL:` 을 **요구**하게는 됐지만 **하나로 제한**하지 않았다.
넷이 동시에 초안을 썼고, 결과적으로 **각자 자기 초안을 자기가 승인**했다.
"초안 -> 팀이 검토 -> 승인"이라는 P5의 의미가 다시 사라진다.

### 6b.2 M4a — first-writer-wins (기계, 강제)

게이트가 **첫 진입 신호의 작성자**를 기억한다.

```python
# ConsensusGate
self.draft_author: str | None = None      # approvals 와 같은 게이트 상태

if content.startswith(self.entry_prefix):
    if self.draft_author is None:
        self.draft_author = author
    self._proposal_received = True
    self._maybe_open(message)
```

소프트 차단 (`gate_closed` / `already_approved` 와 같은 계층):

```text
content.startswith(entry_prefix)
  and draft_author is not None
  and author != draft_author
-> error: draft_already_posted
```

```json
{ "error": "draft_already_posted",
  "author": "agent-4",
  "needs": "FINAL:",
  "message": "agent-4 already posted the FINAL: draft. Read it and APPROVE: it, or REJECT: to ask for a redo." }
```

- **작성자 본인의 갱신은 허용** (`author == draft_author`) — 초안 수정은 정당하다.
- 주입: `agent.gate_draft_author: str | None` — 스칼라 하나, Loop 재추론 금지 (§3.4와 동일).
- 체크포인트: `draft_author`는 게이트 내부 상태 -> `snapshot()` / `restore_state()` 에 한 줄.
  `approvals` 와 대칭이므로 **새 병렬 set 아님**.

### 6b.3 전제조건 — `REJECT:` 가 초안도 리셋해야 한다

현재 `REJECT:` 는 `approvals` 만 지우고 `_proposal_received` 는 **남긴다**:

```python
elif content.startswith("REJECT:"):
    self.approvals.clear()
    self.human_pending = False       # _proposal_received 그대로
```

M4a 를 넣으면 REJECT 후 **새 초안을 올릴 수 없어 교착**이다. 그러므로:

```python
elif content.startswith("REJECT:"):
    self.approvals.clear()
    self.human_pending = False
    self._proposal_received = False
    self.draft_author = None         # 다시 누구나 쓸 수 있다
```

의미상으로도 이쪽이 맞다 — REJECT 는 "다시 제안하라"는 뜻이다.
**P2 에도 동일 적용** (REJECT 후 새 `PROPOSE:` 필요).

**마이그레이션 영향 (실측, 정확히 1건):**

`tests/test_protocol.py::test_reject_clears_collected_approvals` 가 이 계약에 걸린다.

```python
PROPOSE: v1
APPROVE (a)
REJECT  (b)        # approvals 비워짐
APPROVE (b)        # not open
APPROVE (a)        # -> assert gate.is_open   <- 재-PROPOSE 없이 열림
```

D9 적용 후에는 **새 `PROPOSE:` 없이는 안 열린다.** 그런데 이 테스트는 본문에
`"APPROVE: v2 ok"` 라고 써놓고 **v2 를 제안한 적이 없다** — 테스트 자신의 픽션이
새 계약 쪽이 맞다는 증거다. `PROPOSE: v2` 한 줄을 넣어 갱신한다.

영향 없는 것들:
- `test_protocol.py:149` — `require_proposal=False` 게이트. `_maybe_open` 이
  `has_proposal` 을 보지 않으므로 리셋이 무해.
- `test_human_approval_gate.py:137` — human REJECT (stage-2 분기). `approvals`
  비움만 검증. 단 **그 분기에도 초안 리셋을 같이 넣어야** 일관된다.

### 6b.3b 구현 중 확정 — 초안 없는 표는 아예 세지 않는다 (D10 확장)

설계는 D10을 "초안 **교체** 시 approvals 리셋"으로만 잡았다. 구현해 보니
그것만으로는 구멍이 남는다:

```text
PROPOSE: v1 / APPROVE(a) / REJECT(b)     -> 초안·표 모두 리셋
APPROVE(b) / APPROVE(a)                  -> 초안이 없는데 표가 쌓임
PROPOSE: v2                              -> _maybe_open -> 2/2 -> 열림
```

아무도 v2를 본 적 없이 v2가 제출된다. 그런데 이 상태(`draft_author is None`
+ `approvals` 비어있지 않음)는 **D10(지각 PROPOSE) 상태와 완전히 동일**해서
둘을 구분할 수 없다. 규칙을 하나 골라야 한다.

**확정: 초안이 없을 때 던진 `APPROVE:` 는 `approvals` 에 넣지 않는다.**

```python
elif content.startswith("APPROVE:"):
    ...
    if self.require_proposal and not self.has_proposal:
        return          # 투표할 대상이 없다
```

M4의 취지("초안 하나를 팀이 검토·승인")와 일치한다.

**교착 위험 없음** — 이유가 둘:
1. `entry_signal_required`(§3.4)가 그 전송을 툴 계층에서 이미 막는다.
2. `is_agent_done` 가드가 초안 없는 동안 **아무도 park 시키지 않는다.**

D10의 `_maybe_open`-on-entry-signal 은 **안전망으로 유지**한다 (사람 경로 등
소프트 차단이 안 걸리는 통로).

**계약 변경 영향 (실측):** 내가 D10/entry-signal 때 쓴 테스트 4건이
"초안 전 표가 쌓인다"를 전제하고 있었다 — 전부 새 계약으로 갱신.
`require_proposal=False` 게이트(P3/P4)는 첫 APPROVE가 `has_proposal` 을
세우므로 이 규칙에 걸리지 않는다.

### 6b.3c 실행 후 수정 2건 (윤리 딜레마 세션 `c06dfe95`)

M4a 적용 첫 실행에서도 `FINAL:` 이 **4개** 들어갔다. `draft_already_posted` 는
단 1회만 발동.

```text
seq 35 agent-1 FINAL:   -> draft_author = agent-1
seq 36 agent-4 FINAL:   <- 차단 실패
seq 37 agent-3 FINAL:   <- 차단 실패
seq 38 agent-2 FINAL:   <- 차단 실패
```

**(1) 주입값이 낡는다.** `agent.gate_draft_author = gate.draft_author` 는
**문자열 복사**다. 주입은 `step()` 전이고 그 사이 모델 호출이 수 초 걸리므로,
병렬 에이전트 넷이 전부 `None` 을 들고 출발한다.
`gate_approvals` 는 **set 참조**라 이 문제가 없었다 — 같은 실수를 스칼라에서 반복.

**수정:** 라이브 뷰로 주입한다.

```python
agent.gate_draft_author_fn = lambda g=gate: g.draft_author   # 값이 아니라 뷰
```

**남는 창:** 체크는 동기지만 `await tools.execute(...)` 지점에서 다른 태스크가
끼어들 수 있다. 창이 **수 초 -> 한 번의 await** 로 줄 뿐 완전 제거는 아니다.
완전하려면 `send_message` 자체가 거부 가능해야 하는데 과하다 —
**게이트 판정은 이미 정확하다**(메시지 seq 순 첫 번째가 소유). 소프트 차단은
정확성이 아니라 **잡음 제거**용이라는 점을 명시한다.

**(2) 경쟁 초안이 정당한 표를 지웠다 (잠재).** 초안 교체 시 approvals 를 비우는
분기에 **작성자 확인이 없었다.**

```python
elif author != self.draft_author:
    return          # 지는 초안은 게이트가 통째로 무시 — 표를 건드리지 않는다
elif self.approvals:
    self.approvals.clear()
```

이번 실행에서는 seq 36~38 시점에 표가 비어 터지지 않았지만, 표가 모인 뒤
경쟁 초안이 오면 날아간다.

**(3) REJECT 로 소유권 탈취 — 수정 안 함.** agent-2 가 내용 반려가 아니라
"내 초안을 승인해 달라"는 뜻으로 `REJECT:` 를 보내 초안·표를 리셋하고 작성자가
됐다(seq 42-43). REJECT 는 **침묵하는 작성자를 대체할 유일한 통로**라 막으면
교착 위험이 생긴다(D8). (1) 을 고치면 경쟁 초안 자체가 안 생기므로 이 혼란의
**증상** 으로 본다. 재현되면 그때 재검토.

### 6b.3d 신호 매칭 정규화 (세션 `d24695b5`)

라이브 뷰 수정은 먹혔다 — `draft_already_posted` 가 1회 -> **8회**, agent-3 은
초안을 **한 번도** 통과시키지 못했다. 그런데 `FINAL:` 은 여전히 3개 들어갔다.

```text
seq 29 agent-2  '[FINAL: ...'       -> startswith("FINAL:") = False
seq 30 agent-1  'FINAL: ...'        -> True   (유일하게 초안으로 인정)
seq 31 agent-4  '**FINAL: [X] ...'  -> False
```

**차단 실패가 아니라 인식 실패다.** 게이트가 두 개를 초안으로 보지 않았다.

전 세션 측정:

| signal | 정확히 매칭 | 장식 때문에 놓침 |
|--------|-----------|----------------|
| `FINAL:` | 10 | **2** (17%) |
| `APPROVE:` | 116 | 0 |
| `PROPOSE:` | 47 | 0 |
| `READY:` | 40 | 0 |

`FINAL:` 만 유독 실패한다 — 긴 서식 답변의 **제목 자리**라 모델이 꾸민다.
짧은 한 줄 신호는 멀쩡하다.

**꼬리 위험:** 넷 다 꾸며 쓰면 게이트가 초안을 못 보고 -> `entry_signal_required`
가 모든 표를 막고 -> `is_agent_done` 가드로 아무도 park 하지 않아 무한히 돈다.

**수정:** `signals.py` 에 공용 `has_signal(content, prefix)` — 앞쪽
공백/`*`/`_`/`[`/`(`/`#`/`-`/backtick 제거 후 대소문자 무시 비교.
`>`(인용)와 따옴표는 **일부러 남긴다** (남의 글 인용을 자기 주장으로 오인 방지).

기존에 `READY:` 만 관대했고(`strip().upper()`) 나머지는 글자 그대로였다 —
일관성 문제이기도 했다. **게이트와 소프트 차단 9곳 전부** 같은 헬퍼를 쓴다:
둘이 어긋나면 게이트가 무시할 메시지로 에이전트가 차단당한다.

### 6b.4 M4b — `SUBMITTER:` (정보, 강제 아님)

P2 는 이미 분담을 협상한다. 거기서 **P5 에 말할 사람**도 같이 정한다.

```text
PROPOSE: 분할안
- agent-1: 괄호 계산
- agent-2: 곱셈/나눗셈
...
SUBMITTER: agent-3
```

- 파싱: `^SUBMITTER:\s*(\S+)$` — 정규식 한 줄. **NLP 아님** (§2.2 비목표 유지).
- 시점: **P2 게이트가 열릴 때 1회**. 열린 PROPOSE 본문에서 읽어 `protocol.submitter_id` 에 저장.
- 검증: `participants` 에 없는 id면 **무시 + `log` warning** (오타로 P5 를 망가뜨리지 않는다).
- 체크포인트: `protocol.snapshot()` 에 `submitter_id` 한 줄.
- 사용처: **P5 프롬프트뿐.**

```text
- The team chose **agent-3** to submit. agent-3 posts `FINAL:`;
  everyone else reviews that draft and approves it.
```

`SUBMITTER:` 가 없으면 기존 문구("Anyone may compose...") 그대로.

### 6b.5 왜 M4b 를 강제하지 않나

선출을 게이트 조건으로 만들면 **선출자가 침묵할 때 P5 가 교착**한다.
이 저장소는 같은 모양의 교착을 이미 두 번 밟았다:

| 사례 | 원인 |
|------|------|
| D10 | 지각 `PROPOSE:` 를 만장일치 재검사 안 함 |
| P2 파킹 데드락 | 제안 없이 전원 APPROVE -> 전원 done -> 아무도 못 씀 |

M4a 가 바닥에 깔려 있으면 선출이 실패해도 **누군가 쓰면 그게 초안**이라 세션이 진행된다.
**강제는 M4a 하나, M4b 는 그 위의 힌트.** 기계를 두 개 만들지 않는다.

### 6b.6 재우기는 이미 동작한다

"한 명이 쓰고 나머지는 재운다"에서 **재우는 쪽은 신규 작업이 아니다.**
`APPROVE:` -> `is_agent_done` -> `protocol_done` -> `run_agent` 최상단 park
(chatter C2). 안 자는 것은 아직 승인하지 않은 에이전트뿐이고, 그건 자면 안 된다.

### 6b.7 F3 각주

`AGENT_RELEVANCE_BUDGET_DESIGN.md:522` 는 F3(phase_role) 제거 사유를
**"코드에 assignee/proposer 없음"** 으로 적고 있다. `submitter_id` 가 생기면
그 근거 데이터가 생긴다 — P5 에서 선출자의 `attention.floor` 를 올리는 식.
**본 설계 범위 아님.** 각주로만 남긴다.

---

## 7. 마일스톤

| 순서 | ID | 내용 | 완료 조건 |
|------|-----|------|-----------|
| 1 | **E1** | `entry_prefix` 주입 + `bind_prefixes` 삭제 | P2 기존 동작 불변 |
| 2 | **E2** | `_PHASE_GATE_ENTRY` 표 · P5 = `FINAL:` | `FINAL:` 없이 P5 안 열림 |
| 3 | **E3** | `gate_needs_signal` 주입 + `entry_signal_required` 소프트 차단 | 조기 APPROVE 미전송 |
| 4 | **E4** | 프롬프트·넛지 문구 entry_prefix 인자화 | P5 지시에 `FINAL:` 등장 |
| 5 | **E5** | M3 어셈블러 삭제 + `DESIGN.md` §2.3 갱신 | grep `assembler` = 0 |
| 6 | **M4a** | first-writer-wins + `REJECT:` 초안 리셋 | 초안이 항상 1개; REJECT 후 재작성 가능 |
| 7 | **M4b** | `SUBMITTER:` 파싱 -> P5 프롬프트 | 선출자 침묵해도 세션 진행 |
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
| **M4a** 두 번째 `FINAL:` | `draft_already_posted`, 메시지 미적재 |
| **M4a** 작성자 본인 재게시 | 허용 (초안 수정) |
| **M4a** REJECT 후 | 다른 에이전트가 새 `FINAL:` 가능 (교착 없음) |
| **M4a** 체크포인트 | `draft_author` 복원 |
| **M4b** `SUBMITTER: agent-3` | P5 프롬프트에 agent-3 등장 |
| **M4b** 잘못된 id | 무시 + warning, P5 기본 문구 |
| **M4b** 선출자 침묵 | 다른 에이전트 `FINAL:` 로 게이트 열림 |
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
| D6 | 중복 초안: 차단 vs 방치 | **차단** (M4a) |
| D7 | `SUBMITTER:` 파싱 시점 | P2 게이트 open 시 **1회** |
| D8 | 선출자 침묵 시 타임아웃? | **없음** — M4a 폴백으로 충분 |
| D9 | `REJECT:` 가 초안 리셋 — P2 에도? | **예** (의미상 일관) |
| D10 | 초안 갱신 시 `approvals` 리셋? | **예 (확정)** + 초안 없을 때의 표는 애초에 안 셈 (§6b.3b) |

---

## 10. 요약

게이트에 **"무엇이 먼저 와야 하는가"** 를 한 줄 선언으로 주고(`entry_prefix`),
없으면 **표를 받지 않는다**(`entry_signal_required`).
P5는 `FINAL:` 초안 없이 못 열리므로 **제출 스레드에 최종 답이 반드시 남는다** —
이것이 P5를 P4와 구별하는 최소 변경이다.
P4 전원 기여(`RESULT:`)와 어셈블러 삭제는 분리해서 판단한다.
