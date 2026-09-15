# Gateway fan-out — backpressure · 예외 격리 (A7 / D5)

> **Status:** **A7/D5 landed** (per-surface try/except + chat mailbox)  
> **Date:** 2026-09-15  
> **Parent:** `MULTI_FRONT_DESIGN.md` §6.2, `IMPLEMENTATION_GAP_CONSOLIDATED.md` A7/D5  
> **Code:** `gateway/bus.py`, `gateway/register.py`  
> **Tests:** `tests/test_gateway_bus.py`

---

## 0. 한 줄

`SessionGateway.publish`는 **surface별 try/except**로 fan-out을 끊지 않고,  
채팅 surface는 **bounded mailbox + (가능하면) `call_soon` drain**으로 Core 발행 경로를 느린 `on_event`에서 분리한다.  
가득 차면 **가장 오래된 이벤트부터 drop**.

---

## 1. 문제

| ID | 오늘 | 피해 |
|----|------|------|
| **D5** | `publish`가 `on_event`를 무방비 호출 | 한 surface 예외(예: Ink `BrokenPipeError`)가 **이후 surface 전달 중단** |
| **A7** | 동기 `for` 루프로 전부 전달 | 느린/막힌 handler가 **Core `publish_core_event`를 블로킹** |

Discord outbox 자체는 이미 enqueue로 빠르지만, 포맷·라우팅 예외나 미래 느린 handler, stdout 쓰기는 여전히 Core와 같은 호출 스택에 있다.

---

## 2. 목표 · 비목표

### 2.1 목표

1. **예외 격리 (D5):** surface A 실패 ≠ surface B 미전달.  
2. **Chat backpressure (A7):** chat family는 mailbox에 넣고 Core는 즉시 반환(가능하면).  
3. **Drop 정책 명시:** mailbox full → **drop-oldest**; 카운트 조회 가능.  
4. **UI 기본은 sync:** Ink JSONL 순서·결과 교차 최소화 (`mailbox_max=None`).

### 2.2 비목표

| 제외 | 이유 |
|------|------|
| 완전 비동기 worker 스레드 per surface | 복잡도·순서·테스트 비용 |
| Core 이벤트 전역 큐 / 우선순위 | 이번 범위 밖 |
| Drop 시 Wire `error` 이벤트 필수 | 노이즈; 로그만 + `drop_counts` |
| mailbox 크기 YAML 설정 | 상수 default로 충분; 필요 시 후속 |

---

## 3. 계약

### 3.1 `SurfaceSubscription`

| 필드 | 의미 |
|------|------|
| `mailbox_max` | `None` = publish 경로에서 **동기** 전달. `int > 0` = bounded deque. |

### 3.2 `publish` 알고리즘

```text
validate event
for each matching surface with on_event:
  if mailbox_max:
    append to deque; if len > max: popleft + drop_count++
    schedule drain (call_soon if running loop else drain now)
  else:
    deliver_safe(on_event)   # try/except
return attempted delivery count
```

`deliver_safe`: 예외 swallow + `logging.exception` (surface name).

### 3.3 Defaults (`register.py`)

| Helper | `mailbox_max` |
|--------|----------------|
| `register_chat_surface` | `256` |
| `register_ui_surface` | `None` (sync + bus-level isolation) |

직접 `SurfaceSubscription(...)` 하는 테스트/특수 surface는 기본 `None` (기존 sync 의미).

### 3.4 관측

- `SessionGateway.drop_counts() -> dict[str, int]`
- `SessionGateway.drain_mailboxes()` — 테스트·셧다운용 강제 drain

---

## 4. 왜 Discord가 “덜 막히나”

1. Core → `publish` → chat mailbox append (O(1)) → return.  
2. 같은 루프의 `call_soon`에서 format → `bot.enqueue` (역시 O(1)).  
3. 실제 HTTP send는 `_sender_loop` (이미 분리).  

Burst가 mailbox를 넘치면 **오래된 chat 관찰 이벤트만** 버리고 Core/Ink는 계속 간다.

---

## 5. 테스트

- 한 surface `raise` → 다른 surface는 이벤트 수신 (D5).  
- mailbox full → drop-oldest + `drop_counts`.  
- chat register 기본 mailbox; 루프 없을 때 drain 동기로 observe 테스트 유지.
