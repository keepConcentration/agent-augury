# Backend error classification — 인프라 실패를 모델 발화로 만들지 않기

> **Status:** draft (설계만, 미구현)
> **Date:** 2026-09-17
> **Priority:** P1 (실사용: 429 한 번에 세션이 멈춤)
> **Parent:** `DESIGN.md` §3.2 (Model Backend)
> **인접:** `SESSION_TURN_TERMINATION_DESIGN.md` (턴 종료), `PROTOCOL_GATE_WAIT_PARK_DESIGN.md`
> **선례:** Hermes `agent/error_classifier.py` · `agent/retry_utils.py` (§3)
> **Code touch (예정):** `backend/base.py`, `backend/openai_compat.py`,
>   `backend/nous_portal_oauth.py`, `core/agent/loop.py`, `core/session.py`
> **Tests (예정):** `tests/test_backend_errors.py`

---

## 0. 한 줄

백엔드 오류를 **`Completion.text` 로 바꾸지 않는다.** 구조화해서 올려보내고,
`run_agent` 가 **백오프 재시도 / 세션 에러 종료**를 결정한다.

---

## 1. 재현 (세션 `d24695b5` 직후 실행)

```text
💭 agent-2: [backend error] HTTP 429 from chat/completions. Detail:
            {"status":429,"message":"The requested model is temporarily at
             capacity upstream. This is not your API key's rate limit ..."}
   (같은 오류 4회 반복)
...
P5_SUBMIT · submission 2/4 · pending @agent-2 @agent-3   <- 세션 정지
```

답(-8)은 맞았고 `FINAL:` 도 하나였다. 그런데 **agent-2 가 표를 던지지 못해**
게이트가 2/4 에서 멈췄고, 턴이 끝나지 않았다.

### 1.1 원인

```python
# backend/openai_compat.py:70
return Completion(text=f"[backend error] {hint} Detail: {detail}")
```

인프라 실패가 **모델 발화로 둔갑한다.** 그 뒤로 런타임에는 구분할 정보가 없다:

| 실제 | 런타임이 보는 것 |
|------|------------------|
| 모델 호출이 429 로 실패 | `Completion(text="...")`, tool_calls 없음 |
| 모델이 조용히 있기로 함 | `Completion(text="..."/None)`, tool_calls 없음 |

둘 다 "툴 없는 텍스트 스텝" 이므로 게이트 대기 park 로 간다. 파킹은
**설계상 사람이 `/quit` 할 때까지 열려 있고**(PARK §11), 그래서 세션이 선다.

`[backend error]` 접두사는 7군데에서 **문자열로만** 존재한다 — 타입이 없다.

---

## 2. 목표 · 비목표

### 2.1 목표

1. 백엔드가 실패를 **구조화해서** 반환한다 (텍스트 위장 금지).
2. `run_agent` 가 **재시도 가능 오류를 park 로 착각하지 않는다.**
3. 재시도 소진 시 **턴을 명시적으로 끝낸다** (`turn_done(reason=error)`).
4. 모델 대화에 오류 문자열이 **assistant 발화로 쌓이지 않는다.**

### 2.2 비목표 (V1)

- 자격증명 풀 / 키 로테이션
- 모델 폴백 (다른 모델로 전환)
- 컨텍스트 압축 기반 복구
- 프로바이더별 적응형 백오프
- 20종 분류 체계 — **3종이면 충분** (§4.1)

---

## 3. 선례 — Hermes (`hermes-agent-fork`)

`agent/error_classifier.py` (2,010줄) 머리말:

> *"Replaces scattered inline string-matching with a centralized classifier
> that the main retry loop in run_agent.py consults for every API failure."*

핵심 구조:

```text
API 호출 실패 (예외 그대로 유지)
  -> classify_api_error(exc, provider, model, approx_tokens, ...)
  -> ClassifiedError(reason, retryable, should_compress,
                     should_rotate_credential, should_fallback)
  -> conversation_loop 이 힌트대로 행동 -> continue (같은 턴 재시도)
```

**오류가 모델 출력이 되는 경로가 없다.** 이것이 우리와의 유일하고 결정적인 차이다.

### 3.1 429 하나를 세 갈래로 나눈다

`error_classifier.py:1258-1292`:

| 판정 | 조건 | 복구 |
|------|------|------|
| `overloaded` | 본문이 과부하 패턴 | 같은 키로 백오프 재시도 |
| `upstream_rate_limit` | 애그리게이터가 상위 오류를 래핑 | **다른 모델로 폴백, 키는 그대로** |
| `rate_limit` | 그 외 | 백오프 + 자격증명 교체 |

우리가 맞은 메시지가 정확히 두 번째다 —
`"temporarily at capacity upstream. This is not your API key's rate limit"`.
Hermes 주석:

> *"the user's key is healthy, so marking it exhausted / rotating is wrong and
> burns the key for ~24min."*

### 3.2 규모는 빌리지 않는다

Hermes 에는 20+ 분류 · 자격증명 풀 · 모델 폴백 · 컨텍스트 압축이 붙어 있다.
우리 규모엔 과하다. **빌릴 것은 구조 하나뿐이다:**

> 오류는 **예외/구조체로 유지**하고, **루프가 분류해 처리**한다.

백오프 공식은 그대로 쓸 만하다 —
`jittered_backoff(attempt, base_delay=5.0, max_delay=120.0, jitter_ratio=0.5)`
(`agent/retry_utils.py:90`).

---

## 4. 설계

### 4.1 분류는 3종

| kind | 예 | 행동 |
|------|-----|------|
| `retryable` | 429, 500/502/503, 타임아웃, 네트워크 | 백오프 후 재시도 |
| `fatal` | 401/403, 400, 404(model not found) | 즉시 턴 종료 (재시도 무의미) |
| `unknown` | 그 외 | `retryable` 로 취급하되 한도를 짧게 |

20종으로 나눌 이유가 지금은 없다 — 키 로테이션도 모델 폴백도 없기 때문이다.
**필요해질 때 쪼갠다.** `kind` 옆에 원문 `status` / `detail` 을 실어두면
나중에 세분화가 값싸다.

### 4.2 `Completion` 계약

```python
@dataclass
class BackendError:
    kind: str                 # "retryable" | "fatal" | "unknown"
    status: int | None = None
    message: str = ""         # 사람이 읽을 한 줄
    detail: str = ""          # 원문 (잘라서)

@dataclass
class Completion:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    error: BackendError | None = None      # 신규
```

- `error` 가 있으면 `text` 는 **`None`** 이다. 오류 문자열을 모델 발화로 쓰지 않는다.
- 기존 호출자는 `error` 를 몰라도 깨지지 않는다 (기본값 `None`).

`[backend error]` 문자열 7군데를 전부 `BackendError` 로 바꾼다.

### 4.3 `AgentLoop.step()`

오류 completion 이면 **대화에 아무것도 추가하지 않고** 그대로 올려보낸다:

```text
completion = await backend.complete(...)
if completion.error:
    return StepResult(text=None, error=completion.error,
                      drained_count=len(drained))
```

**drain 은 이미 끝났다** — 받은 메시지는 `[radio]` 로 대화에 들어간 상태다.
재시도해도 유실되지 않는다(chatter §3.2.2 의 보존 원칙과 동일).

### 4.4 `Session.run_agent`

park 판정 **앞에** 둔다. 오류는 "조용함"이 아니다.

```text
result = await agent.step()
if result.error:
    if result.error.kind == "fatal":
        publish session error; break
    if backend_retries >= max_backend_retries:   # 기본 3
        publish session error; break
    await sleep(jittered_backoff(backend_retries + 1))
    backend_retries += 1
    continue            # 같은 턴 재시도 — step 카운터 증가 없음
backend_retries = 0     # 성공 시 리셋
```

- `total_steps` 를 **올리지 않는다** (실패한 호출은 진행이 아니다).
- `idle_streak` 도 **건드리지 않는다** (D′ 는 "할 일 없음"을 세는 것이지
  "호출이 실패함"이 아니다).
- 재시도 중에도 `_interrupt` 는 즉시 듣는다.

### 4.5 턴 종료와의 접속

재시도 소진 / `fatal` 이면 그 에이전트 루프만 끊는다. 다른 에이전트는
계속 돌고, 전원이 끝나면 `SESSION_TURN_TERMINATION` 의 기존 경로로
`session.turn_done(reason="error")` 가 나간다. **reason 표에 이미 `error` 가 있다.**

### 4.6 표면

- Wire `error` 이벤트로 한 줄 (`_publish_session_error` 재사용).
- Ink 로그에 `⚠️ [agent-2] backend 429 — 재시도 2/3` 정도. 상태줄은 건드리지 않는다.
- **모델 대화에는 남기지 않는다** (§4.3).

---

## 5. 이 재현이 실제로 어떻게 끝나는가

```text
agent-2: 429 -> retryable -> 5s 백오프 -> 재시도
         429 -> 12s -> 재시도
         429 -> 27s -> 재시도
         429 -> 재시도 소진 -> 세션 에러 + agent-2 루프 종료
agent-1/3/4: 계속 진행. agent-2 가 빠졌으므로 4/4 는 영영 불가
          -> 게이트 미개방 -> 나머지도 결국 park
          -> 사람이 /quit (PARK §11 의 의도된 탈출구)
```

즉 **429 를 견디면 대부분 살아난다.** 견디지 못해도 지금처럼 "무슨 일인지 모른 채
멈춤"이 아니라 **"agent-2 가 백엔드 오류로 빠졌다"** 가 화면에 남는다.

### 5.1 남는 것 (별 이슈)

전원이 park 이고 살아있는 not-done 이 0명이면 여전히 `/quit` 이 필요하다 —
chatter §10 의 **D12** 로 이미 기록돼 있다. 본 설계는 그 앞단(오류를 오류로
인식)만 고치고, D12 는 건드리지 않는다.

---

## 6. 마일스톤

| 순서 | ID | 내용 | 완료 조건 |
|------|-----|------|-----------|
| 1 | **B1** | `BackendError` + `Completion.error` + 7군데 치환 | 오류 시 `text is None`, `error` 채워짐 |
| 2 | **B2** | `StepResult.error` 전달 | 대화에 오류 문자열 미적재 |
| 3 | **B3** | `run_agent` 백오프 재시도 + `fatal` 즉시 종료 | 429 3회 후 세션 에러; step 카운터 불변 |
| 4 | **B4** | Wire/Ink 표시 | `⚠️ backend ... 재시도 n/3` |

B1~B3 은 한 PR. B4 는 분리 가능.

---

## 7. 테스트

| 케이스 | 기대 |
|--------|------|
| 429 후 성공 | 재시도로 복구, `total_steps` 는 성공분만 |
| 429 × 4 | 3회 재시도 후 그 에이전트만 종료, 세션 에러 1건 |
| 401 | 재시도 **없이** 즉시 종료 |
| 오류 completion | `agent.conversation` 에 `[backend error]` 문자열 없음 |
| 오류 중 drain | 이미 받은 `[radio]` 는 대화에 남아 있음 |
| 오류와 D′ | `idle_streak` 증가하지 않음 |
| 오류와 게이트 | park 로 가지 않음 (재시도가 우선) |
| interrupt | 백오프 중에도 즉시 중단 |
| 기존 호출자 | `Completion(text=...)` 만 쓰는 코드 무변경 |

---

## 8. 열린 결정

| # | 질문 | 제안 |
|---|------|------|
| E1 | 재시도 횟수 | **3** (백엔드 내부 재시도와 별개) |
| E2 | 백오프 | Hermes 식 `jittered_backoff(base=5, max=120)` |
| E3 | 분류 3종 vs 세분화 | **3종.** `status`/`detail` 을 실어 나중에 쪼갤 여지만 |
| E4 | 오류를 대화에 남길까 | **아니오** — 모델이 자기 발화로 오인한다 |
| E5 | 백엔드 내부 재시도(현행 2회)는? | **유지.** 루프 재시도는 그 위층 |
| E6 | `upstream_rate_limit` 구분 | V1 **안 함** (모델 폴백이 없어 행동이 같음). `detail` 에 보존 |

---

## 9. 요약

Hermes 에서 빌릴 것은 20종 분류가 아니라 **"오류는 오류로 유지한다"** 는 구조
하나다. `Completion.error` 한 필드와 `run_agent` 의 백오프 분기면,
429 한 번에 세션이 서는 일은 없어지고 실패도 화면에 남는다.
