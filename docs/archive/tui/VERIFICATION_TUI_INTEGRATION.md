# agent-augury TUI 도입 — 통합/검증 리포트 (agent-1)

> **역할:** agent-1 (통합/검증 관점) — 기존 `Session._output_queue`/`cli.py` 파이프라인과의
> 결합 방식, config 스키마 확장안, 마이그레이션 영향, v1.0 블로커 식별 및 해결안
> **Date:** 2026-09 · **상위 문서:** `TUI_INPUT_DESIGN.md` (agent-3 v0.4 통합본)
> **협업:** agent-2(prompt_toolkit), agent-3(문서 통합), agent-4(textual)

---

## 1. 검증 요약

| # | 항목 | 판정 | 근거 |
|---|------|------|------|
| V1 | ask_user 식별 = 기존 `tool` 이벤트 경로 (D8) | ✅ 동의 | `session.py from_config`의 `on_tool_call` lambda가 이미 `type:"tool", tool:"ask_user", args:{question,options,thread}` emit |
| V2 | tools.py/server.py 변경 0 | ✅ 확정 | 구조적 데이터는 이미 tool 이벤트로 전달됨 — 별도 C안 이벤트 불필요 |
| V3 | **thread ref 블로커** — `on_tool_call`이 resolve 전 원본 args 전달 | ⚠️ **v1.0 블로커** | `loop.py step()`: `on_tool_call(self.agent_id, call.name, call.arguments, result)` — `_resolve_refs` **이전** 값 |
| V4 | 해결안 A: loop.py 1줄 변경 | ✅ 권장 | `call.arguments` → resolve된 `args` |
| V5 | 로그 중복 방지 (TUI 모드 한정) | ✅ 동의 | `[ask-user]` prefix 스킵 + `👤 asks:` 로그 대신 pinned 패널 |
| V6 | config `human.tui` 옵트인 | ✅ 동의 | 기존 동작 100% 호환, 마이그레이션 0 |
| V7 | textual 2안 (전면 개편) | ✅ 동의 | v1.1+ 재평가 — ConPTY 필수, rich 로그 공존 불가 |

---

## 2. 기존 파이프라인 분석 (코드 근거)

```
AgentLoop.step()
  ├─ _resolve_refs(call.arguments)          # $thread:N → 실제 thread id
  ├─ _execute_tool(name, args)              # 실행은 resolve된 값
  │    └─ server.send_message(...)          # → server._emit_event(send_message)
  │         └─ ask_user의 경우 content="[ask-user] ..." (prefix)
  └─ on_tool_call(agent_id, name, call.arguments, result)   # ★ resolve 전 원본!
       └─ session.from_config lambda → _server._emit_event({"type":"tool", ...})
            └─ Session._on_server_event → _output_queue
                 └─ _output_consumer → cli.on_tool_event → _log_tool_event
```

### 2.1 이벤트 순서 (skip-then-show)

같은 asyncio 루프 + FIFO 큐에서 ask_user 호출 시 cli가 받는 순서:

1. `send_message` 이벤트 (content=`[ask-user] DB는 뭘 쓸까? (옵션: ...)`)
   → TUI 모드에서 **스킵** (prefix 감지)
2. `tool(ask_user)` 이벤트 (`args={thread, question, options}`)
   → **pinned 패널 갱신** (하단 고정 표시)

→ "먼저 숨기고, 그다음 고정 표시" 순서가 구조적으로 보장됨.
별도 ask_user 이벤트 없이 문제없음 (agent-3 D8 확정).

---

## 3. ⚠️ v1.0 블로커: thread ref (V3) — 상세

### 3.1 문제

`loop.py step()`:
```python
args = self._resolve_refs(call.arguments)          # ① resolve된 args
result = await self._execute_tool(call.name, args) # ② 실행은 resolve된 값으로
...
self.on_tool_call(self.agent_id, call.name, call.arguments, result)  # ③ ★ resolve 전 원본!
```

- `on_tool_call`에는 **resolve 전 `call.arguments`** 가 전달됨.
- `test_human_in_the_loop.py`의 `_hitl_cfg`처럼 에이전트가 `"thread": "$thread:0"`을 쓰면
  cli는 `$thread:0`을 그대로 받음.
- 이걸 `session.human_send(thread_id="$thread:0", ...)`에 넘기면
  **`KeyError: no such thread: $thread:0`** — v1.0 E2E 실패.

### 3.2 해결안 A (권장) — loop.py 1줄

```python
# loop.py — 기존
self.on_tool_call(self.agent_id, call.name, call.arguments, result)
# 변경
self.on_tool_call(self.agent_id, call.name, args, result)   # resolve된 args
```

- `args`는 이미 `_resolve_refs`를 거친 값 — `$thread:N` → `thread-<n>`, `$thread_by_name:` → 실제 id.
- `session.py` lambda는 파라미터명 `args` 그대로 — **시그니처 불변**, 변경 0.
- 파일 도구(read_file 등)는 ref가 없어 영향 없음. `thread`만 실제 id로 일관.

### 3.3 회귀 확인 포인트

- `test_wiring.py` / `test_parallel.py` / `test_agent_loop.py` 등에서
  `on_tool_call`의 args를 검증하는 테스트가 있다면 resolve 전/후 차이 확인 필요.
- 기대: 파일 도구 args는 동일, `create_thread`/`send_message`/`ask_user`의 thread만 실제 id.

### 3.4 대안 비교 (기각 사유)

| 안 | 방식 | 사유 |
|----|------|------|
| B | cli에서 `$thread:N` 파싱 + 에이전트별 created_threads 추적 | cli가 에이전트별 created_threads 목록을 알 수 없음 (loop 내부 상태) → 부정확 |
| C | cli에서 "최근 활성 스레드" 폴백 | 대부분 동작하나 근사 — ask_user가 온 스레드가 아닐 수 있음 |

---

## 4. TUI 응답 라우팅 설계 (검증 관점)

### 4.1 ask_user 응답 (pinned 패널 → 회신)

```python
# cli.py — pinned 패널 응답 처리 (개념)
async def _deliver_ask_user_reply(session, event, text_or_index):
    thread_id = event["args"]["thread"]          # resolve 후 실제 thread id (안 A)
    agent_id = event["agent_id"]                 # 질문한 에이전트
    content = _resolve_choice(event["args"], text_or_index)  # 번호 → 옵션 텍스트
    await session.human_send(
        thread_id, content=content, mentions=[agent_id]     # 해당 에이전트에게만 회신
    )
```

- `mentions=[질문한 agent_id]` → ask_user를 보낸 에이전트만 수신 (브로드캐스트보다 정밀).
- 기존 `_human_input_loop`가 "첫 번째 스레드"에 보내던 문제를 이 기회에 해결.

### 4.2 일반 메시지 (ask_user 없음)

- 최근 활성 스레드로 fan-out (`mentions` 비움 → 참가자 전체, 기존 §3.5.3 규칙).

### 4.3 번호 선택 규칙

| 입력 | 동작 |
|------|------|
| 활성 선택지 + `1`~`N` | `options[idx]` 텍스트로 치환 후 human_send |
| 활성 선택지 + 일반 텍스트 | 원문 그대로 human_send |
| 비활성 + `1`~`N` | 일반 메시지로 처리 (선택지 아님) |
| `quit`/`exit`/Ctrl+D | 입력 루프 종료 (세션은 계속 — 패시브 원칙) |

---

## 5. 로그 중복 방지 (TUI 모드 한정)

| 이벤트 | 비TUI 모드 (현행) | TUI 모드 (`human.tui: prompt_toolkit`) |
|--------|-------------------|----------------------------------------|
| `send_message` content=`[ask-user]...` | 로그 출력 (`💬 [agent-1 → human] ...`) | **스킵** (prefix 감지) |
| `tool(ask_user)` | `👤 agent-1 asks: ...` 로그 출력 | **pinned 패널 표시** (로그 억제) |
| `pin_options: false` | — | 기존 로그 출력 복귀 (config) |

→ TUI 모드에서 질문이 화면에 **1번만** 나타남. 비TUI 모드는 현행 그대로 (회귀 0).

---

## 6. config 스키마 확장안 (검증 완료)

```yaml
human:
  id: human
  interface: cli        # 기존: cli | discord | file
  tui: prompt_toolkit   # 신규(선택): prompt_toolkit | textual | none(기존 input)
  pin_options: true     # 신규(선택, 기본 true): ask_user 선택지 하단 고정
```

- `tui` 키 없으면 기존 `input()` 동작 그대로 (옵트인).
- `--interactive` + `human.tui: prompt_toolkit` → 새 입력창.
- `--interactive`만 → 기존 input(). **기존 CLI 플래그 그대로** (마이그레이션 0).
- config.py 검증: `human.tui` 허용값 `prompt_toolkit`/`textual`/`none`.

---

## 7. 변경 파일 최종 요약 (v1.0)

| 파일 | 변경 | 비고 |
|------|------|------|
| `agent/loop.py` | **1줄**: `on_tool_call`에 resolve된 `args` 전달 | V3 블로커 해결 (필수) |
| `cli.py` | `_human_input_loop_tui`(PromptSession), pinned 패널, 로그 스킵, `human.tui` 분기 | 중심 변경 |
| `config.py` | `human.tui`/`pin_options` 키 검증 | 소량 |
| `pyproject.toml` | `[project.optional-dependencies] tui = ["prompt_toolkit>=3.0"]` | 의존성 |
| `server.py` | **변경 없음** | SSOT 불변 |
| `session.py` | **변경 없음** | lambda 시그니처 동일 |
| `agent/tools.py` | **변경 없음** | ask_user 그대로 |
| `agent/system_prompt.py` | **변경 없음** | — |

---

## 8. 테스트 계획 (검증 관점)

### 8.1 신규 테스트

```python
# test_tui_ask_user_flow.py (개념)
async def test_ask_user_tool_event_carries_resolved_thread():
    """안 A 검증: on_tool_call이 resolve된 thread id를 전달하는지."""
    session = Session.from_config(_hitl_cfg())   # thread="$thread:0" 사용
    await session.run(initial_prompt="...")
    # tool 이벤트에서 args["thread"]가 실제 thread id여야 함
    tool_events = [e for e in captured if e["type"]=="tool" and e["tool"]=="ask_user"]
    assert tool_events[0]["args"]["thread"].startswith("thread-")

async def test_ask_user_reply_routes_to_asking_agent():
    """pinned 응답 → human_send(thread, mentions=[질문 에이전트]) → [radio] 흡수."""
    await session.human_send(tid, content="postgres", mentions=["agent-1"])
    await session.run()
    assert "from human: postgres" in agent.conversation[-1]["content"]

def test_tui_mode_skips_ask_user_log():
    """TUI 모드에서 [ask-user] prefix send_message 로그 스킵."""
    ...

def test_non_tui_regression():
    """human.tui 미설정 → 기존 input() 경로 + 기존 출력 그대로."""
    ...
```

### 8.2 기존 테스트 영향

- `test_human_in_the_loop.py` — **그대로 통과** (서버/세션 레벨 변경 없음).
- `test_wiring.py` / `test_parallel.py` — `on_tool_call` args가 resolve 후 값으로 바뀌므로
  thread 검증이 있는 테스트는 **예상값 수정** 필요 (안 A 채택 시).
- `test_repl.py` / `test_cli_*` — cli 분기 추가로 인한 영향 검토.

### 8.3 E2E 시나리오 (v1.0 통과 기준)

```
시나리오 T1 (상시 입력):
- fake 백엔드로 에이전트가 도는 동안 PipeInput으로 "방향 바꿔줘" 주입
- human_send 호출 → 에이전트 다음 step() [radio]에 from human: 방향 바꿔줘 포함

시나리오 T2 (선택지 고정 + 회신):
- agent-1이 ask_user(question, options=["postgres","mysql"]) 호출
- tool(ask_user) 이벤트 → pinned 패널 갱신 (하단 고정)
- "2" 입력 → human_send(thread, "mysql", mentions=["agent-1"])
- agent-1 [radio] 흡수, 최종 결과 반영

시나리오 T3 (thread ref):
- ask_user가 thread="$thread:0" 사용 → tool 이벤트 args["thread"]가 실제 thread id
- human_send 성공 (KeyError 없음)

시나리오 T4 (호환성):
- human.tui 미설정 → 기존 input() 경로, 기존 테스트 전부 통과
```

---

## 9. 마이그레이션/호환성 매트릭스

| 항목 | 영향 | 비고 |
|------|------|------|
| 기존 config (`human:` 없음) | ✅ 무영향 | `tui` 키 자체가 옵트인 |
| 기존 config (`human:`만, `tui` 없음) | ✅ 무영향 | 기본값 `none` = 기존 input() |
| `--interactive` 없이 실행 | ✅ 무영향 | 입력 태스크 자체가 안 뜸 |
| 서버 영속화 스키마 | ✅ 무영향 | v1.0은 스키마 불변 (v1.1 B안에서 ALTER TABLE) |
| `on_tool_call` 시그니처 | ✅ 불변 | 파라미터명 `args` 그대로, 전달값만 resolve 후로 |
| 기존 테스트 | ⚠️ 소수 수정 | `on_tool_call` thread 검증 테스트만 예상값 갱신 |
| rich 로그 출력 (비TUI) | ✅ 유지 | TUI 모드에서만 로그 스킵 |

**breaking change: 없음.** 단, `loop.py` 1줄 변경(안 A)은 동작 변화이므로
릴리스 노트에 명시 + 회귀 테스트로 고정.

---

## 10. 결론

agent-1 검증 결과:

1. **D8 합의** (기존 tool 이벤트 경로 사용) — 코드 검증 완료, tools.py/server.py 변경 0.
2. **thread ref 블로커 식별** — `on_tool_call`이 resolve 전 원본 args를 전달 → `loop.py` 1줄
   변경(안 A)으로 해결. **v1.0에서 반드시 포함**.
3. **응답 라우팅** — `human_send(thread=resolve된 id, mentions=[질문 에이전트])`로 정밀 회신.
4. **로그 중복 방지** — TUI 모드 한정 스킵, 비TUI 회귀 0.
5. **변경 파일 최소화** — loop.py(1줄) + cli.py + config.py + pyproject.toml.
   server.py / session.py / tools.py / system_prompt.py 불변 (SSOT·L3 원칙 유지).
