# Phase entry signal — 페이즈에 이름이 아니라 내용을 주기

> **Status:** **완료** — E1~E5 · M4a~M4c 구현, **M2 폐기**(§5, 2026-09-17)
> **Date:** 2026-09-16
> **Priority:** P2 (정확도 아님 — 프로토콜이 의미를 갖게)
> **Parent:** `DESIGN.md` §2.3 (P1~P5), `core/protocol/approval.py`
> **인접:** `PROTOCOL_CHATTER_REDUCTION_DESIGN.md` §5 (C4 — 같은 이유로 폐기),
>   `RADIO_DIGEST_DESIGN.md` (M5 — 폐기), `SESSION_TURN_TERMINATION_DESIGN.md`
> **Code touch:** `protocol/approval.py`, `protocol/collaboration.py`,
>   `protocol/signals.py`, `protocol/assignments.py`,
>   `core/session.py`, `agent/loop.py`, `agent/system_prompt.py`
> **Tests:** `tests/test_phase_entry_signal.py`, `tests/test_signal_matching.py`,
>   `tests/test_assignments.py`, `tests/test_gate_drafter_vote.py`
> **결정:** M1 `one(prefix)` 채택 · **M2 `all(prefix)` 폐기** · M3 어셈블러 삭제
> **검증:** 세션 `a858cd97` — 게이트 4개 전부 사람 개입 없이 열림 (§6b.10)

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

## 5. M2 — P4 `all("RESULT:")` · **폐기** (2026-09-17)

> **Status:** **폐기.** 구현된 적 없음 — 지울 코드도 없다.

### 5.1 무엇이었나

§2.3의 P4는 "각자 결과를 근거와 함께 방송"이므로 `one` 이 아니라 **전원 기여**다.
게이트에 `contributions` 집합을 두고, 전원이 `RESULT:` 를 낼 때까지 열지 않는 안이었다.

```python
# 채택하지 않은 설계
self.contributions: set[str] = set()      # approvals 와 대칭
if has_signal(content, self.entry_prefix) and author in self.participants:
    self.contributions.add(author)
# _maybe_open 조건에 추가
if self.entry_mode == "all" and not (set(self.participants) <= self.contributions):
    return
```

### 5.2 왜 폐기하나 — 과녁이 틀렸다

D1은 "M1 실측 후 결정"이었다. 실측이 나왔고, **비용은 M2가 겨냥하던 자리에 없었다.**

세션 `63fec483` 에서 P3(thread-2)이 13메시지로 폭주했을 때, 그 13개를 갈라 보면:

```text
첫 표(APPROVE:) 이전   9개   <- 전부 확인 요청·확인 응답
첫 표 이후             3개   <- 표가 돌자 즉시 종료
```

**들어가는 법(진입 신호)이 없어서가 아니라 나가는 법(종료 신호)을 몰라서** 났다.
`_PHASE_INSTRUCTIONS` 의 P3/P4 블록에는 `APPROVE:` 라는 단어가 아예 없었다 (§6b.8).

문장 두 줄을 넣자 세션 `a858cd97` 에서 **13 → 6 메시지**가 됐다.
M2가 고치려던 비용은 **이미 사라졌다.**

### 5.3 그리고 값이 비싸다

M2는 `require_proposal=True` 를 P3/P4로 넓히는 **장치** 변경이다.
그러면 **새 교착 종류**가 생긴다 — 한 명이 `RESULT:` 를 끝내 안 내면 P4가 안 열린다.
§3.4와 같은 이유로 `is_agent_done` 가드를 `entry` 미충족 전반으로 확장해야 하고,
소프트 차단·스냅샷·복원에 모두 손이 간다.

**이미 없어진 비용을 위해 교착 하나를 사는 거래다.** 하지 않는다.

### 5.4 형제 항목

- **C4 (work-before-vote, `PROTOCOL_CHATTER_REDUCTION_DESIGN` §5)** — 같은 이유로
  폐기. 게다가 C4는 "아무 비시그널 메시지"를 세므로 **더미 로그를 유도**한다는
  약점이 M2보다 크다. 선행 작업으로 잡아둔 `server.current_seq` 도 불필요해졌다.
- **M5 (`RADIO_DIGEST_DESIGN`)** — 별도 이유로 폐기 (radio 의 79%가 작업 로그).

세 항목 모두 **"측정이 설계를 이겼다"** 는 같은 결말이다.

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

### 6b.4b M4b 구현 — `ASSIGN` 까지 확장 (세션 `d24695b5`)

설계는 M4b 를 `SUBMITTER:` 하나로 잡았다. 실행에서 더 큰 구멍이 드러났다.

**관측:** P2 가 "agent-3 이 O 입장을 맡는다"로 합의했는데 agent-3 은 **끝까지
O 논거를 한 번도 내지 않았다.** agent-2 는 "@agent-3 O 입장 기다립니다"를 네 번
반복하다 스스로 X 로 돌아섰고, 최종 검토에 팀이 직접
`agent-3: O 입장 예정이었으나 논거 미제출` 이라고 적었다. 사용자가 요청한
"서로 설득"이 **일어나지 않았다** (4:0 합창).

**원인:** 분담은 P2 스레드의 산문 안에만 있고, **런타임은 그 내용을 전혀 모른다.**
P3 프롬프트는 `"Execute your assigned share (as negotiated in P2)"` 라고만 해서
에이전트가 20턴 전 메시지를 스스로 기억해야 한다.

**구현: `ASSIGN` + `SUBMITTER` 구조화 줄**

```text
PROPOSE: 분할안
ASSIGN agent-1: 공리주의 관점
ASSIGN agent-3: O 입장 옹호      <- 아무도 안 맡았던 쪽
SUBMITTER: agent-2
```

- 파싱: `protocol/assignments.py` — 정규식. **NLP 아님**(§2.2 비목표 유지).
- 시점: `CollaborationProtocol._on_message` 에서 P2 진입 신호를 볼 때.
  REJECT 후 재제안하면 **덮어쓴다**.
- 검증: participants 에 없는 id 는 **버린다** (오타가 P5 를 망가뜨리지 않게).
- 체크포인트: `assignments` / `submitter_id` 스냅샷.
- 사용처: **프롬프트뿐.** P3/P4 에 `Your assigned share: ...`,
  P5 에 선출자 지목("YOU to submit" / "wait for their FINAL:").

**강제하지 않는다 (§6b.5 그대로).** 게이트 조건이 아니므로 `ASSIGN` 줄이 없거나
틀려도 세션이 멈추지 않는다 — 이 저장소가 밟은 교착 두 건이 전부 "게이트에
조건을 하나 더 얹어서" 생겼다.

**남은 것:** 배정을 **지켰는지** 는 여전히 아무도 확인하지 않는다. 그건
M2(P4 `all("RESULT:")`) / C4 의 영역이고, 둘 다 교착 표면이 있어 **사용자 확인
전까지 켜지 않는다.**

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

### 6b.6 M4c — 초안을 쓴 것이 곧 그 초안에 던진 표다 (세션 `d6bcbc56`)

**문제.** 깨끗한 실행에서 게이트가 두 번 멈췄고, 두 번 다 사람이 찔러서 풀었다.

```text
thread-4 (P5)
  23 agent-2  FINAL: ... = 34      <- 초안 작성
  24 agent-4  APPROVE:
  25 agent-3  APPROVE:
  26 agent-1  APPROVE:             <- 3/4. 남은 한 명은 agent-2 자신
     agent-2 (내심) "팀원들의 APPROVE:를 기다리겠습니다"
  27 human    @agent-2 너만 남았어
  28 agent-2  APPROVE:             <- 4/4, 게이트 열림
```

agent-2 는 **자기가 이미 받은 표를 기다리고 있었다.** 착각이 아니라 자연스럽다 —
자기가 쓴 글에 "동의합니다"를 덧붙이는 것은 사람도 하지 않는다.

같은 세션 4개 게이트 중 **3개에서 초안 작성자가 마지막 투표자**였다
(thread-2 seq16 agent-2, thread-3 seq22 agent-3, thread-4 seq28 agent-2).
두 번은 스스로 회복했고, 한 번은 못 했다. **3/4 확률로 아슬아슬했던 것**이지
P5 만의 사고가 아니다.

**변경.** `entry_prefix` 메시지를 작성자 본인의 찬성으로 센다.

```python
# approval.py — entry-signal 분기
self._proposal_received = True
if author in self.participants:
    self.approvals.add(author)
self._maybe_open(message)
```

세 줄이고, 없애는 것은 **게이트마다 왕복 한 번**이다 (4게이트 × 4명 = 16표 → 12표).
재초안(`_reset_for_redo` / 초안 교체)에서는 기존 표와 함께 작성자 표도 지워지고,
새 초안에서 다시 붙는다 — "그 초안에 던진 표"라는 의미가 유지된다.

**부수 효과.** 작성자가 굳이 `APPROVE:` 를 또 보내면 기존 `already_approved`
소프트 차단이 받아낸다. 새 분기 없음.

### 6b.7 남은 두 개 — 말은 했는데 전달이 안 된 경우

같은 세션에서 P2 게이트도 멈췄는데, 원인은 다르다.

```text
seq  9  agent-4 -> thread-1   (빈 메시지)
```

agent-4 는 **`APPROVE:` 를 자기 답변 텍스트에 썼다.** 게이트는 `send_message`
만 본다. 그래서 "말했는데 아무도 못 들은" 상태가 됐고, 역시 사람이 찔러야 했다.

| 조치 | 위치 | 내용 |
|------|------|------|
| `empty_message` | `_run_tool_body` | `content` 가 공백이면 전송 거부. 빈 브로드캐스트는 동료 전원을 깨우고 아무것도 전하지 않는다 |
| `_unsent_signal_nudge` | `step()` 끝 | 답변 텍스트에 신호가 있는데 `send_message` 가 없으면 한 줄 알림을 대화에 붙인다 |

넛지는 **아직 투표하지 않은 에이전트에게만** 뜬다. 표가 들어가는 순간 조용해지므로
반복 잔소리가 되지 않는다.

**한계.** 넛지는 강제가 아니라 유도다. 모델이 계속 무시하면 여전히 멈춘다 —
다만 그 경우에도 D′ idle streak 가 턴을 닫으므로 137턴 폭주로는 가지 않는다.


### 6b.8 P3 는 끝내는 법을 아무도 안 알려줬다 (세션 `63fec483`)

M4c 이후 첫 실행. **사람이 한 번도 개입하지 않고** 게이트 4개가 스스로 열렸다.
그런데 게이트별 비용이 갈렸다.

| 스레드 | 페이즈 | 메시지 | 글자 |
|--------|--------|-----:|-----:|
| thread-1 | P2 | 5 | 633 |
| **thread-2** | **P3** | **13** | **2,632** |
| thread-3 | P4 | 4 | 1,476 |
| thread-4 | P5 | 4 | 1,026 |

P4·P5 의 **4** 는 이론상 최소다 (초안 1 + 찬성 3). P3 만 3배였다.

**원인은 프롬프트다.** `_PHASE_INSTRUCTIONS` 를 나란히 놓으면 드러난다.

| 페이즈 | 끝내는 법을 말하는가 |
|--------|---------------------|
| P2 | ✅ `PROPOSE:`/`APPROVE:` + *"stay silent — do not keep saying that you are waiting"* |
| P5 | ✅ `FINAL:` 하나 + `APPROVE:`/`REJECT:` |
| **P3** | ❌ **`APPROVE:` 라는 단어가 없다.** 멈추는 조건은 *"blocked"* 뿐 |
| **P4** | ❌ 없음 (이번엔 운 좋게 최소였다) |

P3 은 게이트가 있는데 **게이트가 있다는 사실을 에이전트에게 말하지 않았다.**
그래서 "다 했는데 이제 뭐하지"에 대한 답이 없었고, 빈자리를 **확인 요청과
확인 응답**으로 채웠다.

```text
 9 agent-2 (URGENT) agent-3의 제출 준비 상태를 확인해주세요
11 agent-3          최종 계산 결과 정리 및 제출 완료
13 agent-2          agent-4의 검증 확인. agent-3에게 재확인 요청
15 agent-3 (URGENT) 형식 제안 확인 및 최종 제출 완료
16 agent-2          P3 실행 완료 보고
17 agent-3          최종 제출 완료 확인
...
18 agent-1          APPROVE:          <- 첫 표. 열 번째 메시지
20 agent-3          APPROVE:
21 agent-2          APPROVE:          <- 4/4, 3메시지 만에 종료
```

**표를 던지기 시작하자 3메시지 만에 끝났다.** 앞의 9개는 전부 공백 메우기였고,
8단계 풀이를 **여덟 번** 다시 붙여넣었다.

#### 조치 — 새 장치 없이 프롬프트만

P2 가 이미 정답 문장을 갖고 있다. 형제 페이즈에 같은 것을 준다.

```text
P3: - Post ONLY your own work. Do not restate, summarise or acknowledge a
      teammate's result - the radio already delivered it to everyone.
    - When your share is done, say so with `APPROVE:` on the gate thread.
      The phase advances only when ALL agents have. Then stay silent.

P4: - Agreeing needs no message of its own: say `APPROVE:`, do not re-post
      the result you agree with.
    - When your review is done, send `APPROVE:` ...
```

P4 는 이번 실행에서 최소값이었지만 **같은 구멍이 그대로 있다** — 운으로 비껴간
것이므로 형제 자리도 같이 막는다.

#### 왜 M2(`RESULT:` 진입 신호 강제)가 아닌가

M2 는 P3/P4 에 `require_proposal=True` 를 주는 **장치** 변경이다. 측정이 가리키는
것은 다르다: 비용은 *진입 신호가 없어서*가 아니라 **나가는 법을 몰라서** 생겼다.
첫 표 이전에 9개, 이후에 3개다.

장치를 바꾸면 새 교착 종류(아무도 `RESULT:` 를 안 올리면?)와 테스트가 붙는다.
프롬프트는 공짜고 되돌리기 쉽다. **먼저 문장, 다시 측정, 그래도 남으면 M2.**

**D12 갱신:** M2 는 여전히 보류. 다음 실행에서 thread-2 가 **6메시지 이하**로
내려오면 M2 는 폐기 후보다.

#### 부수 관찰 (조치 안 함)

- **seq 4 — 아슬아슬하게 비껴간 초안 경쟁.** agent-2 가 `PROBLEM:` 으로 시작하고
  셋째 줄에 `PROPOSE:` / `SUBMITTER: agent-1` 을 넣었다. `has_signal` 은 **첫 줄만**
  보므로 진입 신호로 치지 않았고, 한 박자 뒤 agent-3 의 `PROPOSE:` 가 초안 주인이
  됐다. 결과는 정상이었으나 **순서가 반대였다면 SUBMITTER 가 갈렸다.**
  현재의 엄격함(첫 줄 = 신호)이 옳다고 보아 그대로 둔다.
- **`(URGENT)` 남용 2건.** 단순 진행 보고에 붙었다. URGENT 는 `_needs_model_reply`
  에서 **재워둔 에이전트를 전부 깨우는** 열쇠다. 이번엔 아무도 park 상태가 아니어서
  손해가 없었다. 위 프롬프트 변경으로 확인 메시지 자체가 줄면 같이 사라질 것이므로
  별도 장치는 만들지 않는다. 남으면 그때 본다.
- **M4c 가 P3 도 살렸다.** agent-4 는 thread-2 에 `APPROVE:` 를 한 번도 보내지 않고
  `PROPOSE:` 만 두 번(seq 10, 12) 보냈다. P3 는 `require_proposal=False` 지만
  `entry_prefix` 기본값은 `PROPOSE:` 로 살아 있어 M4c 가 그것을 agent-4 의 표로
  셌다. **M4c 가 없었으면 3/4 에서 멈췄다** — §6b.6 과 똑같은 모양의 재발이다.


### 6b.9 넛지가 P1 을 못 봤다 (세션 `43addf1b`)

§6b.7 의 `_unsent_signal_nudge` 가 겨냥한 바로 그 사고가 **P1 에서 재발**했다.

```text
server messages
  0 agent-2 thread-5  READY: done
  1 agent-3 thread-5  READY: done
  2 agent-1 thread-5  READY: done
                      <- agent-4 없음
```

agent-4 는 `READY: 392를 찾는 풀이를 제공했습니다.` 를 **자기 답변 텍스트에만**
썼다. 그 뒤 `NO_REPLY` 를 세 번 내고 조용해졌고, P1 이 3/4 인 채로 턴이 끝났다.
(D′ 가 턴을 닫아 폭주는 없었다 — 멈춘 것이지 새는 것이 아니다.)

**넛지는 왜 안 떴나.** P1 에는 게이트 객체가 없다.

| | P2~P5 | **P1** |
|--|-------|--------|
| 진입 신호 | `gate_entry_prefix` | **`READY:`** (필드에 없음) |
| 완료 집합 | `gate_approvals` | **`ready_states`** |
| 대상 스레드 | `gate_thread_id` | **`human` 스레드** |

`_inject_protocol_gate_state` 의 P1 분기는 `gate_entry_prefix = None` 을 넣는다.
그래서 넛지가 보는 후보는 `["APPROVE:"]` 뿐이었고 `READY:` 는 애초에 검사 대상이
아니었다. **P2~P5 만 막고 P1 을 열어둔 셈**이다.

**조치.** 게이트가 바인드됐는지로 두 갈래를 고른다.

```python
if self.gate_thread_id is None:                 # P1
    done, prefixes = self.ready_states, ["READY:"]
    target = self.server.resolve_thread_id("human")
else:                                            # P2~P5
    done = self.gate_approvals
    prefixes = [p for p in (self.gate_entry_prefix, "APPROVE:") if p]
    target = self.gate_thread_id
```

문구도 "gate" 대신 **"only `send_message` actually sends it"** 로 고쳤다 —
P1 에는 게이트가 없어서 원래 문구가 틀린 말이었다.

**교훈.** "신호를 말로만 하고 안 보낸다"는 **페이즈와 무관한 모델 습성**이다.
페이즈마다 다른 필드에 상태를 두면 방어도 페이즈마다 빠뜨리게 된다. 다음에
같은 종류의 방어를 넣을 때는 **P1 의 세 필드부터 확인**할 것.


### 6b.10 검증 (세션 `a858cd97`)

§6b.8(P3/P4 종료 규칙)과 §6b.9(P1 넛지)를 넣고 처음 돌린 실행.

| 스레드 | 페이즈 | 이번 | `63fec483` | `d6bcbc56` |
|--------|--------|-----:|-----:|-----:|
| thread-1 | P2 | 5 | 5 | 5 |
| **thread-2** | **P3** | **6** | **13** | 13 |
| thread-3 | P4 | 5 | 4 | 4 |
| thread-4 | P5 | **4** | 4 | 4(+사람) |
| **합계** | | **24** | 30 | 29 |
| 스텝 | | **46** | 65 | 67 |

§6b.8 이 건 기준(*"thread-2 가 6메시지 이하면 M2 는 폐기 후보"*)을 **정확히 맞췄다.**
`(URGENT)` 남용 0건, "확인 부탁드립니다 / 확인 완료" 왕복 소멸.

**P1 넛지가 실전에서 처음 발동했다.**

```text
agent-4 (답변 텍스트):  "READY:\n\n풀이 과정: ... 정답: 156"   <- send_message 없음

[runtime] You wrote READY: in your reply, but nobody received it
          -- only `send_message` actually sends it. Send it to thread `thread-5`.

seq 3  agent-4  thread-5  "READY: done"                        <- 4/4, P2 진입
```

직전 실행(`43addf1b`)에서 세션을 3/4 로 멈춰세웠던 바로 그 자리다.

#### 고치지 않기로 한 것 — `PROAPPROVE:`

```text
seq 9  agent-1  thread-2  "PROAPPROVE: 곱셈/나눗셈 검증 완료 ..."
```

agent-1 이 `PROPOSE:` 와 `APPROVE:` 를 섞어 썼다. `has_signal` 은 맨 앞이
`APPROVE:` 가 아니므로 받지 않았고, agent-1 은 네 메시지 뒤 seq 13 에서
제대로 된 `APPROVE:` 를 보내 **스스로 복구**했다.

받아주려면 흐릿한 매칭이 필요한데, `signals.py` 가 일부러 피하는 것이 정확히
그것이다 — **남의 말을 인용한 것을 표로 세는 쪽이 신호를 놓치는 쪽보다 훨씬 나쁘다.**
24개 중 1개 낭비이고 자가 복구됐다. 그대로 둔다.


### 6b.11 게이트가 열린 뒤에도 초안을 더 올릴 수 있었다 (세션 `ffb70b4b`)

BSD 추측 증명 요청을 **처음부터** 돌린 실행. REJECT 재작성이 두 번 성사된
좋은 실행인데(§6b.12), 끝에 하나가 샜다.

```text
34 agent-3  thread-4  6970자  FINAL: ... (수정안)
35 agent-1  thread-4         APPROVE:
36 agent-2  thread-4         APPROVE:
37 agent-4  thread-4         APPROVE:      <- 4/4, 게이트 열림 -> COMPLETED
38 agent-3  thread-4  7279자  FINAL: ... (수정안 v2)   <- 아무도 승인하지 않은 답
```

내용은 seq 34 와 거의 같고 ℚ/K 구분 문단만 옮겼다. 그런데 **스레드 맨 아래에
남는 것이 v2** 다 — 제출 스레드의 마지막 `FINAL:` 을 답으로 읽으면
**표를 한 장도 못 받은 초안**을 집게 된다.

**원인은 스냅샷 시점이다.** `gate_open` 은 스텝 시작 때 주입되는 값이다.
agent-3 의 스텝은 seq 37 이 들어오기 **전에** 시작했으므로, 툴을 실행하는
순간에도 "게이트는 아직 모으는 중"이라고 믿고 있었다.

§6b.3c 에서 `draft_author` 를 **살아 있는 뷰**(`gate_draft_author_fn`)로 바꾼
것과 정확히 같은 종류의 경쟁이다. 그때 고친 필드가 하나 모자랐을 뿐이다.

**조치.** 같은 패턴을 하나 더 만든다.

```python
# session.py
agent.gate_is_open_fn = lambda g=gate: g.is_open

# loop.py  _duplicate_signal_denied, 진입 신호 분기 맨 앞
if self.gate_is_open_fn is not None and self.gate_is_open_fn():
    return {"error": "gate_already_open", ...}
```

`draft_already_posted`(남의 초안)보다 **앞**에 둔다. 작성자 본인도 막혀야
하기 때문이다 — 이 사고를 낸 것이 작성자 본인이다.

**한계.** 게이트가 한 번 열리면 되돌릴 방법이 없다(`on_message` 가 조기 반환).
그래서 오류 문구에 `REJECT:` 를 권하지 않는다. 열린 뒤의 정정은 **다음 턴의 일**이다.

### 6b.12 같은 실행에서 잘 된 것

- **REJECT 재작성이 두 번 성사됐다.** 이 로그들에서 처음이다.
  - P2: agent-3 의 분담안이 **존재하지 않는 `agent-5`** 에게 일을 줬고,
    agent-1 이 `REJECT: agent-5가 존재하지 않습니다` 로 잡았다 (seq 7).
    agent-3 이 수정안을 올려(seq 8) 통과했다.
  - P5: agent-4 가 자기 배정 주제 누락을 이유로 `REJECT:`(seq 31),
    agent-1 도 같은 이유로 `REJECT:`(seq 33) → 수정안(seq 34) → 3표 승인.
  `_reset_for_redo` 와 M4c(작성자 자표)가 재작성 경로에서 함께 동작함을 확인.
- 실제 분량 있는 작업 로그가 P3 에 쌓였다 (2,213 / 3,268 / 2,565자).

### 6b.13 고치지 않는 것 — 승인은 여전히 연극이다

이번엔 **증거가 남았다.** seq 35(agent-1)와 seq 37(agent-4)의 승인 본문이
**거의 한 글자도 다르지 않다.**

```text
agent-1: "1. L-함수 부분 완성: ... 2. Iwasawa 이론 및 p-adic 접근: ...
          3. 전체적 구조: 10개 섹체의 구성이 논리적이고 완전합니다."
agent-4: "1. L-함수 부분 완성: ... 2. Iwasawa 이론 및 p-adic 접근: ...
          3. 전체적 구조: 10개 섹션의 구성이 논리적이고 완전합니다."
```

agent-4 는 agent-1 의 승인문을 **그대로 베꼈다**(오타 `섹체`만 고쳐서).
독립 검토가 아니다.

다만 **같은 실행에서 진짜 검토도 있었다** — agent-2 는 seq 32 에서
Gross-Zagier 공식의 분모 누락, 계수 0 증명 2단계의 방향성 오류, 깨진 글자까지
다섯 가지를 짚었다. 게이트가 검토를 **막지는 않는다**. 다만 **보장하지도 않는다.**

런타임이 강제할 수 있는 종류가 아니므로 기록만 남긴다.


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
| D1 | P4 `all("RESULT:")` 채택? | **아니오 — 폐기** (§5). 비용은 진입이 아니라 퇴장에 있었다 |
| D2 | `require_proposal` 이름 유지 vs `entry_required` 개명 | **유지** (호환·diff 최소) |
| D3 | `entry_signal_required` vs 표 허용 후 미개방 | **차단** — 혼란 상태를 만들지 않음 |
| D4 | P2도 `FINAL:`처럼 본문 강제? | 아니오 — `PROPOSE:` 가 이미 그 역할 |
| D5 | 어셈블러 삭제 vs 보존 | **삭제** (§6) |
| D6 | 중복 초안: 차단 vs 방치 | **차단** (M4a) |
| D7 | `SUBMITTER:` 파싱 시점 | P2 게이트 open 시 **1회** |
| D8 | 선출자 침묵 시 타임아웃? | **없음** — M4a 폴백으로 충분 |
| D9 | `REJECT:` 가 초안 리셋 — P2 에도? | **예** (의미상 일관) |
| D10 | 초안 갱신 시 `approvals` 리셋? | **예 (확정)** + 초안 없을 때의 표는 애초에 안 셈 (§6b.3b) |
| D11 | 초안 작성자도 따로 `APPROVE:` 해야 하나 | **아니오 (M4c)** — 초안이 곧 그 표 (§6b.6) |

---

## 10. 요약

게이트에 **"무엇이 먼저 와야 하는가"** 를 한 줄 선언으로 주고(`entry_prefix`),
없으면 **표를 받지 않는다**(`entry_signal_required`).
P5는 `FINAL:` 초안 없이 못 열리므로 **제출 스레드에 최종 답이 반드시 남는다** —
이것이 P5를 P4와 구별하는 최소 변경이다.

그 위에 실사용이 세 가지를 더 가르쳐 줬다.

| | 배운 것 | 값 |
|--|--------|-----|
| M4a (§6b.2) | 초안은 하나 | `FINAL:` 중복 4건 → 1건 |
| M4c (§6b.6) | **초안을 쓴 것이 곧 그 표다** | 게이트당 왕복 1회 절감, P5·P3 교착 제거 |
| §6b.8 | **페이즈마다 끝내는 법을 말해야 한다** | P3 13 → 6 메시지 |

반대로 **P4 전원 기여(M2)는 폐기했다** (§5). 측정해 보니 비용은 진입이 아니라
퇴장에 있었고, 그것은 장치가 아니라 프롬프트 두 줄로 없어졌다.
**설계가 아니라 측정이 무엇을 만들지 정했다.**
