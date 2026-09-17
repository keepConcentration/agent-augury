# Session turn termination — protocol COMPLETED + idle streak

> **Status:** **구현 완료 rev.7** (resume 회귀 수정 포함)  
> **Date:** 2026-09-16  
> **Priority:** P0 (실사용: 합의·정답 후에도 `run()`이 안 끝남)  
> **Parent:** `PROTOCOL_GATE_WAIT_PARK_DESIGN.md`, `DESIGN.md` §2.3 / §6  
> **인접:** `PROTOCOL_CHATTER_REDUCTION_DESIGN.md` (대기 중 토큰; **축 분리**)  
> **Code touch (예정):** `core/session.py`, `gateway/types.py` + Wire schema,  
>   `gateway/session_stdio.py`, **`gateway/headless.py`** (`_do_run` finally + `_after_run` 양쪽),  
>   `fronts/ink/src/{App,wire}.tsx`  
> **Tests (예정):** `tests/test_session_turn_termination.py`, Ink wire/App  
> **결정:** **B + D′ + UI + E** — A/C/단독 D 기각  
> **Rev.3:** streak 리셋을 early-continue **앞** · `turn_done` reason 우선순위 ·  
>   reset = `tool_calls|has_pending|drained_count` · in-flight complete ≤N−1 · headless  
> **Rev.4:** `turn_done`을 `_do_run` **finally** (`set_running(False)`와 동일 보장) ·  
>   reason **`error`** · 이벤트 순서 turn_done→summary · §5.1 부분 게이트 D′  
> **Rev.5:** `error` ≠ `CancelledError` · `reason=error` 시 `steps` 미상(0)  
> **Rev.6:** finally `turn_done` publish → **`except Exception: pass`** (Core 관용구)

---

## 0. 한 줄

**프로토콜이 `COMPLETED`/`REJECTED`이면 `run()`을 끝내고**, Ink에는  
**`session.ended`가 아닌 `session.turn_done`** 으로 idle을 알린다.  
게이트 비대기에서는 **명시 침묵(`text is None`)은 즉시 종료**,  
**텍스트 있는 idle은 연속 2회(D′)** 에 종료한다.

---

## 1. 배경 · 재현

수학 P1–P5 실사용:

- submission 4표 `APPROVE:` → 페이즈 `COMPLETED`, 정답 222
- Ink `P5_SUBMIT · submission 3/4 · pending @agent-3` **고착**
- `NO_REPLY` / `정답: 222` 반복 → **턴 미종료**

### 1.1 원인 (확정)

| 축 | 원인 |
|----|------|
| **종료 계약 부재** | `COMPLETED` ≠ `run()` 종료. 레거시는 `text is None` break. 답 재탕은 text라 루프 유지 |
| **UI 고착 (게이트)** | open 직후 `advance(COMPLETED)` → `gate_for(COMPLETED)` None → `session.gate` 미발행 → 3/4 잔존 |
| **UI 고착 (턴)** | `_after_run`만 `turn_done`을 내면 **정상 경로만** 커버. `session.run()` **예외**·루프에서 `_after_run` **건너뜀**(quit 직후 break 등) → `set_running(False)`는 finally인데 Wire 미발행 → Ink `[running]` 고착. **내부 상태는 바뀌었는데 Wire에 안 나감** (게이트 3/4와 동일 병) |

**표 누락 아님 (체크포인트 확정):**

```json
"P5_SUBMIT": {
  "approvals": ["agent-1", "agent-2", "agent-3", "agent-4"],
  "opened_at_seq": 32
}
"phase": "COMPLETED",
"gate_open_fired": ["P2_SPLIT", "P3_EXECUTE", "P4_REVIEW", "P5_SUBMIT"]
```

agent-3 표는 들어갔고 게이트도 열렸다. 추측으로 “표가 샜나?”를 다시 파지 말 것.

### 1.2 chatter와의 관계

대기 중 토큰(chatter) ≠ 성공 후 턴 종료(본 문서). 축이 다르다.

---

## 2. 목표 · 비목표

### 2.1 목표

1. **B:** 단말 페이즈 → 모든 `run_agent` break → `gather` 반환.  
2. **Wire 턴 종료:** `session.turn_done` (프로세스 유지) — Ink `running=false` + resume 안내.  
3. **D′:** 비대기에서 **텍스트 있는** 무툴 idle 연속 2회 → break.  
4. **레거시 유지:** `text is None` → 즉시 break (모호함 0).  
5. **UI 게이트:** open 스냅샷 → advance; 단말 후 상태줄 정책(§6.3).  
6. **E:** `max_steps` = 핑퐁·무한 툴 뚜껑 (D′가 못 막는 것).

### 2.2 비목표

- `session.ended`로 턴 알림 (Ink가 프로세스 exit)  
- COMPLETED에서 park만 (A)  
- wait-narration 휴리스틱 (C)  
- 무툴 1회 무조건 종료 (D)  
- `_protocol_terminal` 병렬 Event (phase SSOT로 충분)

---

## 3. 대안 평가

| ID | 판정 |
|----|------|
| A park on COMPLETED | **기각** — 행 |
| B 턴 종료 | **채택** |
| C 휴리스틱 | **기각** |
| D 무툴 즉시 | **기각** |
| D′ 텍스트 idle 2회 | **채택** (`text is None`과 **병행**) |
| E max_steps | **안전망** (핑퐁 포함) |

---

## 4. 설계 B — 단말 → `run()` 종료 (Event 없음)

### 4.1 계약

```text
protocol.phase ∈ {COMPLETED, REJECTED}
  ⇒ run_agent while 최상단 break
  ⇒ gather 반환
```

### 4.2 구현 (SSOT = phase만)

새 Event/`_signal_protocol_terminal` **불필요**.

```text
# run_agent while 최상단
if self.protocol and self.protocol.phase in (COMPLETED, REJECTED):
    break
```

park 웨이크는 **이미 공짜**:

```text
# _wait_for_gate_wakeup
if protocol.phase != phase0:
    return True   # advance(COMPLETED) → 깨어남 → 루프 top → break
```

chatter §3.1 “새 병렬 set 금지” · phase SSOT와 일관.

### 4.3 Wire — **`session.turn_done`**

| 이벤트 | 의미 | Ink |
|--------|------|-----|
| `session.ended` | 프로세스/세션 수명 종료 | `exit()` |
| **`session.turn_done`** | **한 번의 `run()` 종료**, 프로세스 유지 | `running=false` + resume 안내 |

#### 발행 위치 — `_do_run` finally (rev.4)

`turn_done`은 **`_after_run`이 아니라 `_do_run`의 `finally`** 에 둔다.  
`set_running(False)`와 같은 **항상 실행** 보장.

```text
async def _do_run(prompt):
    self.bridge.set_running(True)
    steps = 0
    run_exc = None
    try:
        steps = await self.session.run(...)
        return steps
    except BaseException as e:
        run_exc = e
        raise
    finally:
        self.bridge.set_running(False)
        try:
            publish session.turn_done(reason=…, steps=…, run_exc=…)  # stdio·headless 공통
        except Exception:  # noqa: BLE001, S110 — never break Core for Wire
            pass
```

**finally에서 publish가 던지면** 원래 예외(`run_exc`·`CancelledError`)가 **가려진다**.  
취소·teardown 중에는 stdout/transport가 이미 닫혀 `BrokenPipeError` 등이 나기 쉬움 —  
`turn_done`은 **UI 편의 신호**이지 제어 흐름이 아니므로, 못 나가도 **원래 예외가 올라가야** 한다.  
`core/session.py` Wire publish와 동일: `except Exception: pass` (4곳).

- **예외:** `_after_run`에 도달하지 못해도 `turn_done`은 **시도** (실패 시 `reason=error`, §표; publish 실패는 삼킴).  
- **Ctrl+C (주 경로):** `request_interrupt` → 에이전트 task만 `cancel` → `gather(..., return_exceptions=True)`가 삼킴 → `run()` **정상 반환** → `reason=interrupted` (`error` 아님).  
- **quit 직후 break:** `_after_run`을 건너뛰어도 finally가 덮음 (`session.ended`와 병행 가능).  
- **러너당 한 군데** — stdio·headless 드리프트 방지.

`_after_run(steps)`는 **summary log·flush_observers만** (turn_done **없음**).

#### 이벤트 순서 (테스트·로그)

```text
1. session.turn_done          ← _do_run finally
2. flush_observers + summary log   ← _after_run (정상 경로만; 예외 시 생략 가능)
```

Ink는 순서에 민감하지 않으나, wire 테스트는 **turn_done이 summary 앞**을 기대할 수 있음.

#### reason 도출 (stdio·headless **동일** 규칙)

`steps`·세션 상태·`_do_run` 예외 플래그에서 위에서부터 첫 매칭:

| 우선 | reason | 조건 |
|------|--------|------|
| 0 | `error` | `run_exc is not None` **and not** `isinstance(run_exc, asyncio.CancelledError)` |
| 1 | `interrupted` | `session.interrupted()` |
| 2 | `protocol_rejected` | `protocol` and `phase == REJECTED` |
| 3 | `protocol_completed` | `protocol` and `phase == COMPLETED` |
| 4 | `max_steps` | `max_steps > 0` and `steps >= max_steps` |
| 5 | `idle` | 그 외 (D′ / `text is None` 등) |

**`CancelledError`:** `BaseException` catch에 포함되지만 **`error`로 분류하지 않음**  
(제어 흐름 취소 — `_do_run`·루프 teardown 등, 드묾). 표 0번 통과 후 `interrupted` 등 다음 행 적용.

**`reason=error`일 때 `steps`:** 스케치처럼 `run()`이 **던지면 반환값을 못 받아** payload `steps`는 **0(미상)** 이다.  
`run()` 내부 `total_steps`는 반환 전 지역값이라 gateway에서 부분 진행량을 꺼낼 수 **없음** — “0스텝 만에 죽었다”로 읽지 말 것.  
테스트는 **`reason=error`에 `steps` 어서션 금지**.

**세션 단위 근사치:** 에이전트마다 break 이유가 다를 수 있음  
(한쪽 idle, 한쪽 max_steps). reason은 “턴이 끝난 지배적 이유”이지  
에이전트별 정밀 진단이 아님 — 문서·테스트에 명시.

페이로드 예:

```json
{
  "dir": "event",
  "type": "session.turn_done",
  "reason": "protocol_completed",
  "steps": 42,
  "phase": "COMPLETED"
}
```

발행: **`session_stdio._do_run` / `headless._do_run` finally** (양쪽 동일 helper 권장).

Ink: `turn_done` → `running=false` + resume 로그.  
**B만 넣고 turn_done 없으면** UI `[running]` 고착 (게이트 3/4와 같은 병).  
**소소 (T1b):** 매 턴 같은 resume 안내가 쌓이면 dim 또는 **직전 줄과 동일하면 생략**  
(Ctrl+C 경로와 동일 문자열 — `App.tsx`).

### 4.4 in-flight complete (구조적 잔여)

`advance(COMPLETED)`는 마지막 `APPROVE:`를 **보낸** 에이전트의 툴 실행 중에 일어남.  
다른 에이전트가 이미 `await backend.complete(...)` 안이면 그 호출은 끝난 뒤에야  
루프 top → break.

- **최대 N−1회** 추가 complete는 구조적으로 남음.  
- task cancel로 막지 않음 (과함).  
- 테스트: `complete` **추가 0**이 아니라 **`≤ N−1` (또는 ≤ N)** 로 느슨히.  
  확정적 어서션: `turn_done` 1회, `run()` 반환.

### 4.4b 이미 끝난 프로토콜로 시작하는 턴 (rev.7 - 실사용 버그)

**B는 "이번 run에서 단말에 도달했을 때"만 적용된다.**

`run_agent` 최상단의 단말 체크를 무조건 걸면, `COMPLETED` 이후의 **모든 후속 턴이
첫 iteration에서 즉시 break** 한다. 실사용 재현: 답 제출 후 질문을 보낼 때마다
`session: 0 steps` 가 나오고 메시지가 에이전트에 **도달조차 하지 않았다**.

§4.4(resume = 자유 대화)와 §4.1(단말 → break)이 충돌한 것. 계약을 좁힌다:

```text
_run_impl 시작 시 1회:
    protocol_spent = protocol and phase in {COMPLETED, REJECTED}

run_agent while 최상단:
    if not protocol_spent and protocol and phase in {COMPLETED, REJECTED}:
        break
```

- 이번 턴에 P5가 열려 COMPLETED가 되면 → 전원 break (B의 목적)
- 이미 COMPLETED인 채 시작한 턴은 → 자유 대화, D'가 닫는다 (§5)

**테스트 주의:** `_run_impl` **호출 전에** phase를 COMPLETED로 세팅하면 그것은
B가 아니라 **resume 케이스**다. rev.6의 테스트가 그렇게 작성돼 이 버그를 고정하고
있었다. B는 run 도중 게이트가 열려 단말로 가는 경로로 검증해야 한다.

### 4.5 resume

다음 `human.send` → `setRunning(true)` + 새 `run()`.  
페이즈는 `COMPLETED` 유지 → 자유 대화 (새 P1 아님).

### 4.6 REJECTED

동일 B + `reason=protocol_rejected`.

---

## 5. 설계 D′ — 텍스트 idle 2회 (+ `text is None` 유지)

### 5.1 적용

`not _is_gate_waiting()` 일 때만.

**부분 게이트 config:** `protocol.gates`에 **중간 페이즈만** 있으면(예: `P2_SPLIT`만)  
P3/P4/P5는 `gate_for(phase) is None` → `_is_gate_waiting()` False → **D′ 작동**.  
그 구성에서는 `_on_protocol_gate_open`이 게이트 open에서만 발화하므로 **페이즈가 advance되지 않음**  
(D′ 없으면 무한 스핀, D′ 있으면 턴이 닫힘). **의도:** D′가 턴을 닫아 주는 쪽이 낫다.  
기본 4게이트·`mode: light`(P1→P5)에서는 발생하지 않음.  
“D′가 프로토콜을 죽였다”로 오해하지 말 것 — **게이트 없는 중간 페이즈** 전제.

### 5.2 규칙 (rev.3) — 리셋은 early-continue **앞**

실제 `run_agent`는 결정 지점이 **셋**으로 갈라져 있다 (`session.py`):

```text
if result.tool_calls: continue
if has_pending:       continue
if gate_waiting:      park …
if result.text is None: break   # legacy
```

streak 리셋을 `else: # 비대기` 안에 두면 `tool_calls`/`has_pending`에 **영원히 도달 못 함**  
→ streak만 증가 → “생각→툴→생각→툴”이 2 idle로 끊김 = **D를 기각한 바로 그 타격**.

**올바른 배치** (continue 이전 리셋):

```text
# step 이후, skipped면 streak 미증가 후 continue
if result.tool_calls:
    idle_streak = 0
    await sleep(0); continue

if has_pending:   # step 후 inbox — 현행 변수명
    idle_streak = 0
    await sleep(0); continue

# 이번 스텝에서 라디오를 실제로 drain했으면 리셋
# (has_pending과 별개; StepResult.drained_count — 새 상태 없음)
if result.drained_count:
    idle_streak = 0

if self._is_gate_waiting():
    … nudge / park …   # D′ 미적용
    continue or break per park

# —— 여기부터 비대기 idle 종료 ——
if result.text is None:
    break                           # 유지 — 명시 침묵
idle_streak += 1                    # 텍스트 있는 idle만
if idle_streak >= 2:
    break
```

리셋 조건 한 줄 요약 (새 상태 없음):

```text
if result.tool_calls or has_pending or result.drained_count:
    idle_streak = 0
```

`has_pending` = **step 후** 값 (현행). “step 전 inbox”와 혼용하지 않음 —  
`drained_count`가 “이번 스텝에 처리한 라디오”를 담당.

**`text is None` break 유지** (`test_no_protocol_empty_text_still_exits`).  
D′는 **텍스트 있는 idle**만 센다.

### 5.3 핑퐁 (D′ 한계)

A `send_message` → B inbox 리셋 → B 응답 → A 리셋 → …  
D′는 **에이전트 로컬** streak라 못 막음. **E(`max_steps`) 담당** — §7에 명시.  
D′가 “모든 idle을 닫는다”고 읽히면 안 된다.

### 5.4 skipped

D5/T0 `skipped` → streak **미증가**.

---

## 6. 설계 UI — 게이트 스냅샷 + 단말 바

### 6.1 open 순서

```text
1. opened_at_seq
2. publish session.gate (open=true, pending=[], phase=아직 P5…)
3. on_open → advance(COMPLETED)
4. session.phase
5. (run 종료 후) session.turn_done
```

권장: Session `on_open` / `_open_gate` 경로에서 **명시 publish** (구독 순서에 의존하지 않음).

### 6.2 단말 후 상태줄 (필수 한 줄)

T0만으로 `submission 4/4 · gate open`이 **영원히** 남으면 끝난 세션에 게이트 UI가 남음.

**권장:** 단말(`COMPLETED`/`REJECTED`) 또는 `session.turn_done` 수신 시 Ink가  
**GateStatusBar를 비우거나** `COMPLETED` / `REJECTED` 한 단어로 교체.

```text
session.phase === COMPLETED|REJECTED  → clear gate bar | show phase name
session.turn_done                     → 동일 (비프로토콜 idle 종료 포함)
```

Core가 단말용 `session.gate`를 한 번 더 보낼 필요 없음 — Ink가 phase/turn_done으로 정리.

---

## 7. 설계 E — max_steps

- 핑퐁·무한 툴·기타 D′/B 밖 루프의 **뚜껑**.  
- D′ 성공으로 착각하지 말 것.  
- README 권장 값 문서화 (T3).

---

## 8. 제어 흐름

```text
while True:
    if interrupt or max_steps: break
    if protocol and phase in (COMPLETED, REJECTED): break   # B
    inject …
    result = step()
    if skipped:
        continue   # streak 미증가

    if result.tool_calls:
        idle_streak = 0
        continue
    if has_pending:
        idle_streak = 0
        continue
    if result.drained_count:
        idle_streak = 0

    if gate_waiting:
        nudge / park …
        continue or break
    if result.text is None:
        break
    idle_streak += 1
    if idle_streak >= 2:
        break
```

`_do_run` finally → reason 표(§4.3) → **`session.turn_done`**  
→ (정상 경로) `_after_run(steps)` → summary log  
(**stdio + headless**).

계층: interrupt/E → B(phase) → tool/pending/drain 리셋 → gate-wait → text None → D′.

---

## 9. 마일스톤

| 순서 | ID | 내용 |
|------|-----|------|
| 1 | **T0** | open 스냅샷→advance; Ink 단말/turn_done 시 게이트 바 clear |
| 2 | **T1a** | B: while top phase check |
| 3 | **T1b** | `session.turn_done` + reason 표 + **stdio·headless** `_do_run` finally + Ink idle (resume 로그 중복 §4.3) |
| 4 | **T2** | D′: 리셋은 continue **앞**; `drained_count`; `text is None` 유지 |
| 5 | **T3** | max_steps 문서 (핑퐁=E) |

T1a만 넣고 T1b 빼면 **금지**.

---

## 10. 테스트

| 케이스 | 기대 |
|--------|------|
| P5 4표 → COMPLETED | `run()` 반환, **`turn_done` 1회** (`reason=protocol_completed`) |
| 추가 complete | **≤ N−1** (in-flight); **0 강제 금지** (flaky) |
| Ink | turn_done 후 `running=false`; `[running]` 잔존 없음 |
| session.ended | quit/eof만 process exit |
| 상태줄 | 단말 후 3/4 없음; gate bar clear/`COMPLETED` |
| `text is None` 1회 | steps==1 유지 |
| text idle 2회 | 2번째 break |
| tool → text → tool → text | streak 리셋되어 **조기 break 없음** |
| drain만 있는 step | `drained_count>0` → streak 0 |
| 핑퐁 | D′로 안 끊김; max_steps 상한 |
| headless | stdio와 동일 `turn_done` reason 규칙 |
| `run()` 예외 | `turn_done` 1회 `reason=error`; `[running]` 고착 없음; **`steps` 어서션 금지** |
| turn_done vs log | **turn_done 먼저**, 그다음 `_after_run` summary |
| resume | turn_done 후 human.send → 새 run |

**회귀:** D′는 기존 `test_parallel` 2건의 계약을 바꾼다  
(`test_all_agents_finish_when_script_exhausts` — 툴로 streak 리셋해 IndexError 경로 유지;  
`test_agent_failure_isolated` — steps 고정 대신 양쪽 ≥1 스텝·세션 생존).  
나머지 스크립트 기반 테스트(broadcast·resume·headless 등)는 툴/메시징으로 streak이 리셋돼 영향 없음.

---

## 11. 문서 연동

- IMPLEMENTATION_GAP P0 · PARK §12 · chatter §10 · DESIGN §7  
- Wire schema + `EVENT_TYPES`에 `session.turn_done`

---

## 12. 열린 결정

| # | 기본값 (rev.3) |
|---|----------------|
| Q1 | `session.turn_done` (`ended` 아님) |
| Q2 | D′ 임계 **2** |
| Q3 | skipped → streak 미증가 |
| Q4 | REJECTED = B |
| Q5 | 단말 상태줄 clear / phase명 |
| Q6 | 이름 `turn_done` |
| Q7 | reason = §4.3 우선순위 (`error`=0, 세션 근사) |
| Q9 | `turn_done` = `_do_run` finally (rev.4) |
| Q8 | in-flight complete ≤ N−1 허용 |

---

## 13. 요약

rev.6: finally **`turn_done` publish는 `except Exception: pass`** (원래 예외 보존);  
rev.5: `error`는 **`CancelledError` 제외**; `reason=error`의 **`steps`=미상(0)**;  
rev.4: `turn_done` **`_do_run` finally**; turn_done → log; §5.1 부분 게이트;  
rev.3: streak **continue 앞**; reason 표; **`drained_count`**; B in-flight ≤N−1;  
stdio·headless **동일 helper**.

---

## 8. 후속 턴 — 끝난 프로토콜이 남긴 것 (세션 `a858cd97`)

사칙연산 세션이 COMPLETED 로 끝난 뒤, 같은 세션에 **BSD 추측 증명 요청**이 들어왔다.
`protocol_spent` 가드 덕에 프로토콜은 **재시작하지 않았다** (체크포인트 `phase: COMPLETED`,
로그에 `[phase]` 전환 없음). 그런데 39스텝, 서버 메시지 16건을 더 썼고 답이 오염됐다.

### 8.1 원인 — 지난 턴의 분담이 프롬프트에 그대로 남았다

체크포인트에 이렇게 남아 있었다.

```json
"phase": "COMPLETED",
"assignments": {
  "agent-2": "괄호 내부 계산 검증 - (7-3)=4, (125-37)=88, ...",
  "agent-3": "곱셈/나눗셈 계산 검증 - 18×4, 24÷6, ..."
},
"submitter_id": "agent-1"
```

`_inject_protocol_gate_state` 는 `assignment` / `submitter_id` 를 **분기 앞에서
무조건** 넣었다. 그리고 `_phase_instructions_with_gate` 는

```python
base = _PHASE_INSTRUCTIONS.get(phase, "")   # COMPLETED -> ""
if assignment:
    base = base + f"
- Your assigned share (agreed in P2): {assignment}"
```

COMPLETED 에는 페이즈 블록이 없으므로, **남은 유일한 프로토콜 문장이 지난 턴의
분담 지시**가 됐다.

> `- Your assigned share (agreed in P2): 괄호 내부 계산 검증 - (7-3)=4 ...`

그래서 agent-2 는 **타원곡선 질문 안에서 사칙연산을 계속 검증**했고,
agent-1 은 `<final_summary>정답: 156</final_summary>` 를 네 번 재발행했다.

### 8.2 조치

터미널 페이즈에서는 분담을 비운다. 분담은 **그 질문의 것**이지 다음 질문의 것이 아니다.

```python
if protocol.phase in (COMPLETED, REJECTED):
    agent.assignment = None
    agent.submitter_id = None
else:
    agent.assignment = protocol.assignment_for(agent.agent_id)
    agent.submitter_id = protocol.submitter_id
```

### 8.3 두 번째 원인 — 터미널 페이즈에 프롬프트가 없었다

§8.2 로 프롬프트 오염은 끊었지만 **흉내는 남았다.** 에이전트는 여전히
`thread-1` 에 `PROPOSE:` 를, `thread-4` 에 `FINAL:` 을 올렸다 (서버 seq 24~39).
그 메시지들은 아무 게이트도 열지 않는다 — 프로토콜은 spent 다.

원인은 §6b.8(P3/P4)과 **똑같은 모양**이다.

```python
base = _PHASE_INSTRUCTIONS.get(phase, "")   # COMPLETED -> "" (항목 자체가 없음)
```

터미널 페이즈에는 블록이 **아예 없었다.** 그래서 후속 턴에서 에이전트를 이끄는
유일한 것이 **방금 끝난 실행으로 가득 찬 대화 이력**이었다. 이력에 `PROPOSE:` /
`APPROVE:` / `FINAL:` 이 100턴 쌓여 있으면 그 패턴을 이어가는 것이 자연스럽다.

**조치 — 항목 두 개.** 장치는 건드리지 않는다.

```text
COMPLETED: - 그 게이트 스레드들은 닫힌 이력이다. 지금 올리는 신호는 아무것도 열지 않는다.
           - 프로토콜을 다시 돌리지 말고, 이전 답을 반복하지 마라.
           - 사용자의 새 질문에 답변으로 바로 답하라. 팀원에게 메시지는 정말 필요할 때만.
REJECTED:  - 합의 없이 끝났음을 밝히고 사용자에게 바로 답하라.
```

**왜 이력 절단이나 턴 경계 표시가 아닌가.** 셋 다 같은 증상을 겨냥하지만 값이 다르다.

| 안 | 값 | 평가 |
|----|-----|------|
| 터미널 페이즈 블록 | 딕셔너리 항목 2개 | **채택.** P1~P5 규칙이 사는 바로 그 자리 |
| 턴 경계 표시(`[new turn]`) | 모든 턴의 사용자 메시지 모양이 바뀜 | 첫 턴까지 영향. 신호도 더 약하다 |
| 이력 절단 | 맥락 손실·복원 규칙 필요 | 후속 질문이 이전 답을 참조할 수 있어야 한다 |
| 게이트 스레드 전송 차단 | 새 차단 종류 | 흉내를 옮길 뿐 없애지 못한다. spent 후 그 스레드는 그냥 스레드다 |

**같은 교훈의 세 번째 사례다** — §6b.8(P3/P4 퇴장 규칙), §6b.9(P1 넛지),
그리고 여기. **상태가 비어 있는 자리마다 방어도 비어 있었다.**

### 8.4 남는 것 (조치 안 함)

- **합의가 검증처럼 보인다.** 제출된 `FINAL:` 의 첫 줄이
  *"결론: 이 정리는 일반적으로 증명되었습니다"* 로 **본문과 정반대**였는데,
  세 에이전트가 모두 *"수학적으로 정확합니다"* 라며 APPROVE 했다. 승인 본문은
  초안을 인용하지 않고 **자기가 이미 믿던 것의 체크리스트**였다.
  게이트는 *전원이 표를 던졌다* 는 사실만 보증한다 — **읽었다는 보증이 아니다.**
  런타임으로 고칠 수 있는 종류가 아니므로 기록만 남긴다.
- **백엔드 모델 열화.** `qa`, `proof`, `ORSURVEILLANCE`, 짝이 맞지 않는
  `</final_summary>`, `유finite성` / `브레uil` 같은 혼종 표기가 다수 새어 나왔다.
  고유명사도 상당수 환각이다(`코일레트`, `코딜레-루빈슈타인`, 함수체 BSD 를
  `천자오(Zhao)` 로). 프로토콜 문제가 아니라 모델 문제다.
