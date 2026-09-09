# agent-augury — 상시 입력창(TUI) 설계 문서 (통합본)

> **Task:** agent-augury에 사용자가 세션 도중 언제든 메시지를 넣을 수 있는 **상시 입력창(TUI)** 도입.
> 목적 ① 사용자가 inbox에 언제든 메시지 주입 ② 에이전트가 `ask_user`로 부여한 **선택지가 에이전트 대화 로그에 밀려 사라지지 않게 고정(pin)**.
> **Date:** 2026-09 · agent-3 (조사/설계/통합) · 협업: agent-2 (prompt_toolkit 조사), agent-4 (textual 조사 + 구현 방향), agent-1 (통합/검증)
> **Status:** **v0.6 — 팀 최종 합의 확정.** 1차 라이브러리 prompt_toolkit 확정, ask_user 식별 D8(tool 이벤트 경로) 확정, thread ref 블로커(안 A: loop.py 1줄) 해결, 변경 파일 5개로 최소화
> **관련 문서:** `LIBRARY_RESEARCH_prompt_toolkit.md` (agent-2), `TUI_INPUT_BAR_DESIGN.md` (agent-4), `TUI_INPUT_DESIGN.md` (agent-2 통합 초안), `VERIFICATION_TUI_INTEGRATION.md` (agent-1 검증 리포트) — 이 문서는 이들의 **통합 상위 문서**
> **산출 경로:** `C:\Users\test\aDocument\agent-augury\TUI_ALWAYS_ON_INPUT_DESIGN.md`

---

## 0. 한 줄 결론

> **`prompt_toolkit` 기반의 "상단 로그 + 하단 고정 선택지 패널(bottom_toolbar) + 하단 상시 입력줄" 구조를 도입한다.**
> - 입력창은 `PromptSession.prompt_async()`로 기존 asyncio `Session.run()`과 **같은 이벤트 루프**에서 동작시켜 `run_in_executor(input())` 구조를 제거한다.
> - `ask_user` 선택지는 로그 스트림이 아닌 **하단 고정 툴바(bottom_toolbar)** 에 렌더링해 밀려 사라지지 않게 한다. (v1.0 레벨 B → v1.1+ full-screen 레벨 C)
> - **textual은 full-screen(alternate screen buffer) 전제라 기존 rich 인라인 로그 파이프라인과 공존 불가 → 1차 도입에서 배제.** "전면 개편(채팅 앱 스타일 TUI)"을 원할 때만 v2.0 옵션으로 전환 경로를 남긴다 (agent-2/agent-4 공동 결론).
> - **SSOT/프로토콜 불변**: TUI는 표시/입력 계층(HumanAdapter)만 교체한다. `server.human_send`/`ask_user`/`[radio]` 흡수 경로는 그대로 재사용.
> - **변경 파일 5개로 최소화** (팀 합의): `loop.py`(1줄, 안 A) / `cli.py` / `config.py` / `pyproject.toml` + 신규 `channel/human_tui.py`. `server.py`/`session.py`/`tools.py`/`system_prompt.py`는 **변경 없음**.

---

## 1. 배경: 왜 지금 상시 입력창이 필요한가

### 1.1 현재 사용자 접점 (코드 근거)

| 접점 | 위치 | 문제점 |
|------|------|--------|
| 인터랙티브 위저드 | `wizard.py` / `cli._run_wizard_flow` | 세션 **시작 전**에만 동작. 도중 입력 불가 |
| `--interactive` HITL | `cli._human_input_loop` | `run_in_executor(input())` — **단일 라인**, 로그와 섞임, 선택지 미고정 |
| `ask_user` 도구 | `agent/tools.py` | fire-and-forget. 질문/선택지는 `send_message` 이벤트로 로그에 흘러감 → **스크롤로 사라짐** |
| REPL 모드 | `cli._run_repl` | 세션 **사이**에만 질문 수용. 세션 도중엔 불가 |

### 1.2 근본 문제 3가지

1. **상시 입력 부재** — `--interactive`가 있지만 `input()` 기반이라 (a) 입력 프롬프트가 로그 맨 아래에 붙어 에이전트 출력과 섞이고, (b) 멀티라인/히스토리/한글 IME/붙여넣기 UX가 부족하고, (c) 비동기 루프와의 결합이 스레드 기반이라 깔끔하지 않다.
2. **선택지 소실** — `ask_user`의 options가 로그에 `💬 [agent-1 → human]` 형태로 출력되다가 다음 메시지들에 밀려 스크롤 밖으로 사라진다. 사용자가 "응답해야 할 선택지"를 놓치게 된다.
3. **응답 경로 분산** — 선택지를 보고도 번호 선택/키 선택 같은 빠른 응답 수단이 없어, 사용자가 선택지를 텍스트로 다시 타이핑해야 한다.

### 1.3 목표

1. 세션 **도중** 언제든 메시지를 넣을 수 있는 **항상 하단에 고정된 입력창**.
2. `ask_user`의 질문/선택지가 **로그 스크롤과 무관하게 고정(pin)** 되어 사용자가 항상 볼 수 있음.
3. 선택지에 **번호/키로 즉시 응답** 가능 (타이핑 최소화).
4. 기존 L3 패시브 어웨어니스 철학 유지 — 사용자 입력도 `server.human_send → inbox push → step() drain` 경로 그대로.
5. 기존 `rich` 로그 출력 파이프라인을 최대한 재사용 (침습 최소화).

---

## 2. 현재 구현 분석 (HITL 상태)

`USER_INTERVENTION_DESIGN.md` v1.1에 따라 **이미 구현된 것**:

- `server.py`: `_humans` 레지스트리 분리, `register_human()`, `human_send()`, `ReservedNameError` (human 예약어 차단) — ✅ 구현
- `agent/tools.py`: `ask_user` 도구 (fire-and-forget, options 포함) — ✅ 구현
- `agent/system_prompt.py`: HITL 규칙 블록 — ✅ 구현
- `session.py`: `has_human`, `human_send()` 패스스루, `Session.from_config`에서 human 등록 — ✅ 구현
- `cli.py`: `--interactive` 플래그, `_human_input_loop` (run_in_executor), ask_user 로그 표시 (`👤 {agent} asks: ...`) — ✅ 구현
- `config.py`: `human:` 섹션 검증 (id=human, interface=cli) — ✅ 구현
- `tests/test_human_in_the_loop.py`, `test_reserved_names.py` — ✅ 구현

### 2.1 현재 출력 파이프라인 (TUI 도입 시 재사용/변경 대상)

```
AgentLoop._execute_tool ──► server._emit_event(tool/send_message/...)
        │                            │
        ▼                            ▼
 Session._on_server_event ──► Session._output_queue (asyncio.Queue)
        │
        ▼
 Session._output_consumer ──► cli.on_step / cli.on_tool_event
        │
        ▼
 cli._log_step / _log_tool_event (rich Console)
```

- `send_message`/`create_thread`/`read_resource`는 **서버 이벤트**로 출력되고, tool 이벤트에선 중복 스킵 (D2-dedup).
- `ask_user`는 tool 이벤트에서 `👤 {agent} asks: {question}` 형태로 출력 (options 포함) — **이미 구조적 데이터(question/options)가 tool 이벤트에 존재** (중요, §6).

### 2.2 ⚠️ thread ref 블로커 (V3 — agent-1 검증, v1.0 필수 해결)

`loop.py step()` 실제 코드:

```python
args = self._resolve_refs(call.arguments)          # ① resolve된 args
result = await self._execute_tool(call.name, args) # ② 실행은 resolve된 값으로
...
self.on_tool_call(self.agent_id, call.name, call.arguments, result)  # ③ ★ resolve 전 원본!
```

- `on_tool_call`에는 `_resolve_refs` **이전**의 `call.arguments`(원본)가 전달된다.
- `test_human_in_the_loop.py`의 `_hitl_cfg`처럼 에이전트가 `"thread": "$thread:0"`을 쓰면 cli는 `$thread:0`을 그대로 받고, `session.human_send(thread_id="$thread:0", ...)`에 넘기면 **`KeyError: no such thread: $thread:0`** 발생.

**해결안 A (팀 합의, v1.0 필수):** `loop.py` 1줄 변경 — `on_tool_call`에 **resolve된 `args`** 전달:

```python
# loop.py — 기존
self.on_tool_call(self.agent_id, call.name, call.arguments, result)
# 변경
self.on_tool_call(self.agent_id, call.name, args, result)   # resolve된 args
```

- `session.py` lambda는 파라미터명 `args` 그대로 — **시그니처 불변**, 변경 0.
- 파일 도구(read_file 등)는 ref가 없어 영향 없음. `thread`만 실제 id로 일관.
- **회귀 확인 포인트:** `test_wiring.py` / `test_parallel.py` / `test_agent_loop.py`에서 `on_tool_call` args 검증 시 resolve 전/후 차이 확인 필요 (thread만 실제 id로 변경).
- 대안 기각: B안(cli에서 `$thread:N` 파싱 — cli가 에이전트별 `created_threads` 목록을 알 수 없음), C안(최근 활성 스레드 폴백 — 근사치라 부정확).

---

## 3. 요구사항 (수용 기준)

| ID | 요구사항 | 수용 기준 |
|----|----------|-----------|
| R1 | 세션 도중 상시 입력 | 에이전트가 일하는 동안 언제든 입력 가능, 입력 즉시 `human_send`로 주입 |
| R2 | 입력창 고정 | 입력창이 항상 터미널 하단에 고정, 로그 스크롤과 무관 |
| R3 | 선택지 고정 | `ask_user` 질문+options가 로그에 밀려 사라지지 않음 |
| R4 | 선택지 빠른 응답 | 번호(1..N) 또는 키 입력으로 선택지 응답 가능 |
| R5 | 멀티라인/붙여넣기 | 긴 task/멀티라인 텍스트 입력 가능 (빈 줄 또는 Meta+Enter로 제출) |
| R6 | 히스토리 | 이전 입력 recall (↑/↓), 세션 간 유지 가능 |
| R7 | 한글 IME | Windows에서 한글 입력 정상 동작 |
| R8 | 비동기 통합 | 기존 asyncio `Session.run()`과 같은 루프, `run_in_executor` 제거 |
| R9 | 호환성 | `--interactive` 없으면 기존 동작 100% 동일. 비-TTY/파이프 환경에서도 크래시 없음 |
| R10 | 테스트 가능 | `PipeInput` 등으로 human 입력 시뮬레이션, 기존 `test_human_in_the_loop.py`와 병행 |

---

## 4. 라이브러리 조사 및 비교

### 4.1 후보 요약

| 라이브러리 | 종류 | 비동기 | Windows | 선택지 고정 | 기존 rich 로그 재사용 | 의존성 부담 |
|-----------|------|--------|---------|-------------|----------------------|-------------|
| **prompt_toolkit** | 입력/레이아웃 라이브러리 | ✅ `prompt_async`/`run_async` | ✅ 네이티브 (win32+colorama, ConHost 포함) | ✅ toolbar 또는 full-screen Layout | △ 로그 버퍼로 재라우팅 필요 (경량) | 낮음 (순수 Python, wcwidth/pygments) |
| **textual** | full-screen TUI 프레임워크 | ✅ `run_async`/`run_test` | ⚠️ ConPTY 필수 (Win10 1809+) | ✅ 위젯 구조상 자연 | ❌ full-screen 전환 시 인라인 로그 공존 불가 | 중간 (rich 의존, 추가 설치) |
| **rich only** (Layout+Live+Prompt) | 출력/렌더링 | △ | ✅ | ❌ 진짜 편집 입력 불가 | ✅ 그대로 | 0 (이미 설치) |
| urwid | TUI 프레임워크 | △ (이벤트 루프 자체) | △ | ✅ | ❌ | 중간, asyncio 통합 불편 |
| pytermgui | TUI 프레임워크 | △ | △ | ✅ | ❌ | 중간, 성숙도 낮음 |
| ink (Rust) | TUI | — | — | — | — | 다른 언어, 배제 |

> rich 15.0.0은 이미 설치·사용 중. prompt_toolkit / textual은 **미설치** (추가 의존성 결정 필요).

### 4.2 prompt_toolkit 상세 (agent-2 조사 — `LIBRARY_RESEARCH_prompt_toolkit.md`)

- **비동기 first-class**: `PromptSession.prompt_async()` / `Application.run_async()` — 기존 asyncio 루프와 공존. `_human_input_loop`의 `run_in_executor` 제거 가능. (R8 ✅)
- **선택지 고정 3레벨**:
  - A. 인라인 + `patch_stdout` — 최소 변경, 단 "고정" 아님 (스크롤 소실)
  - B. `PromptSession` bottom_toolbar에 선택지 — 항상 하단 고정, 로그는 위로 흐름. **v1.0 권장**
  - C. full-screen `Application`(로그/고정패널/입력 3분할) — **완전한 고정**, v1.1+
- **입력 UX**: `FileHistory` (R6), `multiline=True` + `enable_open_in_editor` (R5), Windows IME 네이티브 (R7), `PipeInput` 테스트 (R10).
- **주의**: prompt_toolkit과 rich는 둘 다 터미널을 직접 다루므로 출력 경쟁 → `patch_stdout` 또는 전용 출력 라우팅 필수. 단 레벨 B의 bottom_toolbar는 프롬프트 재렌더링과 분리되어 갱신되므로 실제 깜빡임은 제한적 (agent-2 검증).
- **의존성**: 순수 Python, wcwidth/pygments 정도 — 추가 부담 낮음. BSD-3-Clause (Apache-2.0과 충돌 없음).

### 4.3 textual 상세 (agent-4 조사 — `TUI_INPUT_BAR_DESIGN.md`)

**핵심 기능**
- `Input`(단일 라인) / `TextArea`(멀티라인): placeholder, `Input.Submitted` 이벤트.
- `RichLog` 위젯: 로그 스트리밍용, `max_lines`로 스크롤백 제한 — 기존 `_output_queue` 출력을 RichLog에 write하면 채팅 로그가 됨.
- `OptionList` 위젯: 선택지 목록, `on_option_list_option_selected` 이벤트 — **ask_user pinned 선택지 패널로 정확히 부합**.
- CSS 스타일링: `dock: bottom`으로 입력창 하단 고정, `width: 1fr` 레이아웃, 반응형 breakpoints.
- reactive: `@watch("...")` 상태 변경 시 자동 UI 갱신. `run_test()` pilot으로 헤드리스 테스트 내장.

**Windows 호환 (중요)**
- **Windows 10 1809+의 ConPTY가 필수.** Windows Terminal / VS Code 터미널에서는 정상, 구형 cmd.exe/ConHost에서는 불안정.
- 한글 IME: Windows Terminal + ConPTY 환경에서 입력 지원 (터미널 의존적).

**trade-off (핵심 결론)**
- textual은 **full-screen(alternate screen buffer)** 모델 → 기존 rich 인라인 로그 출력과 **공존 불가** (전환하면 print 기반 로그가 안 보임). "상시 입력창"은 로그 흐름 유지 + 하단 고정 UI가 목표라 full-screen 전환은 UX 파괴.
- textual은 **"전면 개편"**(채팅 앱 스타일 TUI)에 적합, prompt_toolkit은 **"기존 로그에 입력창/패널 추가"**에 적합.
- textual 채택 = `cli.py` 대부분 + 출력 이벤트 포맷 재작성 (침습 큼). 러닝 커브(CSS/reactive) 중간~높음.

### 4.4 rich-only (의존성 0 옵션)

- rich `Layout` + `Live`로 화면 갱신 가능, `Prompt.ask`도 가능.
- **하지만**: `Prompt.ask`는 단발 질문일 뿐 "상시 편집 가능한 입력창"(커서 이동/히스토리/멀티라인/IME)이 **아님**. R5~R7 미충족.
- 결론: rich-only는 입력창 도입에 부적합. rich는 **로그 영역 렌더링**으로만 유지.

### 4.5 비교 결론 (확정)

| 기준 | prompt_toolkit | textual |
|------|----------------|---------|
| asyncio 통합 | ✅ 자연스러움 (run_async) | ✅ 가능하나 full-screen 재설계 |
| 기존 rich 파이프라인 침습 | △ cli.py 소량 + 신규 모듈 1개 | ❌ 인라인 로그 공존 불가 (전면 개편) |
| 선택지 고정 | ✅ bottom_toolbar (R2 직접 해결) | ✅ OptionList (위젯 구조상 유리) |
| Windows IME | ✅ 성숙 (네이티브, ConHost 포함) | ⚠️ ConPTY 필수, 터미널 의존 |
| 첫 릴리스 리스크 | 낮음 | 중간~높음 |
| 목적 부합도 | ✅ "로그 유지 + 입력창/패널 추가" | △ "전면 개편" 전용 |

> **확정: 1차 도입은 `prompt_toolkit` (agent-1/2/4 공동 동의).**
> textual은 (1) "채팅 앱 스타일 전면 TUI 개편"을 원하는 사용자층이 생기고, (2) Windows Terminal/ConPTY 환경만 지원해도 된다고 판단될 때 **v2.0 전환 옵션**으로 남긴다.
> 전환 경로: 기존 `_log_tool_event`/`_log_step`의 rich 렌더링 결과를 `RichLog.write()`로 재사용 가능 → 마이그레이션 비용이 비교적 낮음.

---

## 5. 아키텍처 설계

### 5.1 전체 구조

```
┌───────────────────────────────────────────────────────────────┐
│                    터미널 (로그 스크롤 유지)                  │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ ① 로그 영역 (스크롤)  — rich 렌더링, 기존 파이프라인 유지 │  │
│  │   · step 요약, tool 이벤트, send_message 브로드캐스트     │  │
│  └─────────────────────────────────────────────────────────┘  │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ ② 선택지 패널 (고정, pin) — ask_user 질문 + options     │  │
│  │   · bottom_toolbar (v1.0) / full-screen 3분할 (v1.1+)   │  │
│  │   · 활성 ask_user가 있으면 표시, 응답 시 해제            │  │
│  └─────────────────────────────────────────────────────────┘  │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ ③ 입력줄 (항상 하단 고정) — PromptSession               │  │
│  │   · 일반 텍스트: human_send로 주입                       │  │
│  │   · "1"~"N" 입력 시: 활성 선택지 응답                    │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
```

### 5.2 컴포넌트 (v1.0 — 최소 침습)

```
src/agent_augury/
  channel/
    human_tui.py      # ★ 신규: HumanTUIAdapter (prompt_toolkit 기반)
                      #   · run_input_loop(): PromptSession.prompt_async() 상시 입력 루프
                      #   · on_ask_user(): tool 이벤트(tool=="ask_user") 수신 → _pending_question 저장 → toolbar 렌더
                      #   · _deliver(): 번호 응답 치환 / 일반 텍스트 human_send
  agent/loop.py       # (1줄, 안 A) on_tool_call에 resolve된 args 전달 — thread ref 블로커 해결 (필수)
  cli.py              # (소) --interactive 분기: human.interface=tui → HumanTUIAdapter 사용
                      #      + ask_user 로그 출력 대체/억제 + [ask-user] prefix send_message 스킵
  config.py           # (소) human.interface 검증에 'tui' 추가 + human.tui 키 검증
  agent/tools.py      # (변경 없음) ask_user는 그대로 — SSOT 불변 (D8)
  server.py           # (변경 없음) human_send 그대로 — SSOT 불변
  session.py          # (변경 없음) on_tool_call lambda 시그니처 동일 (파라미터명 args)
pyproject.toml        # prompt-toolkit 의존성 추가
examples/
  human_tui_demo.yaml # 신규: TUI 데모 (fake 백엔드, ask_user 포함)
tests/
  test_human_tui.py   # 신규: HumanTUIAdapter 단위 테스트 (PipeInput 헤드리스)
```

> v1.1+ full-screen(레벨 C)로 확장 시 `tui/` 패키지(log_buffer, choice_panel, input_bar)로 분리한다.

### 5.3 데이터 흐름 (skip-then-show 순서 보장 — agent-1 검증)

```
[Agent] ask_user(thread, question, options)
   │
   ▼
ToolBox.execute("ask_user") → server.send_message(mentions=["human"])
   │   │
   │   └─► (1) send_message 이벤트 emit (content="[ask-user] ...", human inbox push)
   ▼
AgentLoop.step() → on_tool_call(agent_id, "ask_user", args, result)
   │   ※ 안 A: resolve된 args 전달 (thread는 실제 id) — §2.2
   ▼
(2) tool 이벤트 emit (tool=="ask_user", args={question, options, thread(실제 id)})
   │
   ▼  같은 asyncio 루프 + FIFO → cli 처리 순서: (1) → (2)
Session._output_consumer → cli.on_tool_event
   │
   ├─► (1) send_message([ask-user] prefix) → TUI 모드: 로그 스킵 (먼저 숨김)
   └─► (2) tool(ask_user) → HumanTUIAdapter.on_ask_user() → pinned 패널 갱신 (그다음 고정 표시)
                    │
                    ▼
[User] "2" 입력 ──► PromptSession
   │
   ├─► (활성 선택지 있음) _deliver("2") → options[1] 치환
   │        └─► session.human_send(thread_id=질문 스레드(실제 id), content="코드 가독성 우선",
   │                               mentions=[질문한 agent_id])
   └─► (활성 선택지 없음) session.human_send(thread_id=최근 활성 스레드, content=text)
                    │
                    ▼
        agent inbox push → step() [radio] 흡수 (기존 L3 경로 그대로)
```

> **skip-then-show 순서 (agent-1 검증):** `_execute_tool` 내부의 `server.send_message`가 send_message 이벤트를 먼저 emit하고, 그 후 `on_tool_call`이 tool 이벤트를 emit한다. 같은 이벤트 루프 + FIFO 큐이므로 cli 처리 순서가 구조적으로 보장된다:
> 1. `send_message` 이벤트 (`[ask-user]` prefix) → TUI 모드에서 **스킵**
> 2. `tool(ask_user)` 이벤트 → **pinned 패널 갱신**

### 5.4 핵심 설계 결정

| # | 결정 | 근거 |
|---|------|------|
| D1 | **`PromptSession.prompt_async()` 사용** — 기존 asyncio 루프에서 `Session.run()`과 병렬 태스크로 실행 | R8. `run_in_executor` 제거, 스레드 없음 |
| D2 | **선택지 응답은 `human_send`로 주입** (특수 명령이 아니라 일반 사용자 메시지) | 기존 L3 파이프라인 그대로 — 에이전트가 `[radio] from human: ...`로 흡수 |
| D3 | **선택지 패널은 "활성 ask_user 1개" 모델 + PendingQuestions 큐** — 최신 질문 우선 표시, 이전 것은 로그에 남김 (v1.1에서 큐 순차 노출) | 복잡도 억제. 여러 에이전트가 동시에 질문해도 하나씩 답 가능 |
| D4 | **로그 영역은 기존 rich Console 유지** (v1.0) — `_log_tool_event`/`_log_step` 렌더링 로직 재사용 | 침습 최소화. 서버 이벤트 → output_queue → console.print 그대로 |
| D5 | **비-TTY/파이프 환경 fallback** — TUI 불가 시 기존 rich 로그 + `input()` 모드로 자동 전환 | R9. `check_tty()` 재사용 |
| D6 | **config 확장은 `human.interface: tui`** (신규 값) + `--interactive` 플래그 유지 | 기존 config 호환. `interface: cli`/미지정은 기존 동작 100% |
| D7 | **선택지 응답 포맷: 번호 + 원문 둘 다 수용** — 번호("2")면 options[1] 텍스트로 치환해 주입, 텍스트면 그대로 | 에이전트가 의미 파악 쉬움. config `response_format`으로 전환 가능 |
| D8 | **ask_user 식별: 기존 `tool` 이벤트(`tool=="ask_user"`)를 기본 경로로 사용** — 구조적 데이터(question/options)가 이미 `args`에 존재. **별도 이벤트/스키마 변경 불필요** (tools.py/server.py 변경 0) | 팀 합의 확정 (agent-1/2/4). agent-2 C안 철회. `send_message`의 `[ask-user]` prefix는 로그 중복 억제용 보조 |
| D9 | **`patch_stdout`는 보조 수단** — v1.0 레벨 B는 bottom_toolbar가 프롬프트 재렌더링과 분리되어 갱신되므로 깜빡임 제한적 (agent-2 검증). full-screen(레벨 C)에서는 자체 버퍼 렌더링 | agent-2 조사 반영 |
| D10 | **응답 스레드: 안 A로 resolve된 `args["thread"]`를 사용** (loop.py 1줄) + 실패 시 최근 활성 스레드 fallback. 회신은 **질문한 에이전트에게만** `mentions=[agent_id]` | agent-1/2/4 합의. V3 블로커 해결. 기존 `_human_input_loop`의 "첫 스레드 고정" 한계 개선 |
| D11 | **기존 ask_user 로그 출력 대체** — TUI 모드에서 `_log_tool_event`의 `👤 ... asks:` 로그 출력을 **pinned 패널 표시로 대체** (로그 억제). `pin_options: false`일 때만 기존 로그 유지 | agent-4 보강 반영. 질문이 화면에 1번만 표시되어 중복 방지 |
| D12 | **로그 스킵은 TUI 모드 한정** — `human.tui` 설정 시에만 `[ask-user]` prefix send_message 로그 스킵 + ask_user 로그 대체. 비TUI 모드는 **현행 동작 그대로** (회귀 0) | agent-1 §2.2 반영. 현재 ask_user가 send_message 로그 + tool 로그 **둘 다 출력하는 기존 중복**은 TUI 모드에서 해결, 비TUI는 건드리지 않음 |
| D13 | **세션 종료/정리** — `Session.close()`/`_run` finally에서 PendingQuestions·pinned 상태 초기화 + `ps.app.exit()` 명시 호출 (prompt_async가 루프 종료를 기다리지 않게) | agent-1 §2.4 반영. 기존 `_human_input_loop` cancel 패턴 재사용 |

---

## 6. ask_user 식별 및 선택지 고정 (핵심 설계)

### 6.1 ask_user 식별 경로 — 확정 (D8)

agent-1 검증: **ask_user는 `send_message`로 구현되어 있어** 서버 이벤트로는 `send_message`로만 보인다. 선택지를 고정하려면 ask_user 메시지를 식별 가능한 신호가 필요하다.

**확정안 (D8, 팀 합의):** 별도 이벤트 타입을 새로 만들지 않고, **이미 구현된 `tool` 이벤트(`on_tool_call` 콜백 → `_emit_event({"type":"tool","tool":"ask_user","args":{...}})`)를 기본 식별 경로**로 사용한다.

- `session.py` `from_config`의 `on_tool_call` 람다는 이미 `tool` 이벤트를 발생시키고, `args`에 `question`/`options`/`thread`가 **구조적으로** 담겨 있다 (§2.1, `tools.py` `ask_user` 분기).
- `loop.py step()`의 `on_tool_call` 호출부는 **안 A로 resolve된 `args`를 전달**하도록 1줄 변경 (V3 블로커 해결, §2.2).
- `cli._log_tool_event`도 이미 `tool == "ask_user"` 분기가 존재 → TUI 어댑터는 여기서 구조적 데이터를 받아 `on_ask_user()` 호출.
- `send_message` 이벤트의 `[ask-user]` prefix는 **로그 중복 억제용 보조 신호**로만 사용 (D12 — TUI 모드 한정):
  - TUI 모드: `send_message` 이벤트 중 content가 `[ask-user]`로 시작하면 **로그 출력 스킵** (질문은 툴바에만 표시).
  - `interface: cli`(기존) 모드: 현재처럼 로그에 그대로 출력 (회귀 0).

### 6.2 상태 모델

```python
@dataclass
class PendingQuestion:
    thread_id: str           # ask_user의 thread (안 A로 resolve된 실제 id)
    agent_id: str            # 질문한 에이전트
    question: str
    options: list[str]
    created_at: float
    status: Literal["pending", "answered", "dismissed"]
```

- `on_ask_user()` 호출 시 `_pending_question` 갱신 (v1.0: 최신 1개) 또는 `_pending_queue.append` (v1.1: 큐).
- 응답/타임아웃/세션 종료 시 `status` 변경 → 툴바에서 제거.

### 6.3 표시 형식 (bottom_toolbar)

```
┌─ 로그 (스크롤) ────────────────────────────────────────────┐
│  💭 agent-1: 초기 탐색 완료...                             │
│  💬 [agent-2 → broadcast][thread-1] (FYI) DB 쪽 맡겠습니다 │
│                                                            │
├─ 선택지 패널 (bottom_toolbar, 항상 하단 고정) ─────────────┤
│  ❓ agent-1: 어떤 방향으로 진행할까요?                      │
│     [1] 성능 최적화 우선   [2] 코드 가독성 우선   [3] 문서화 │
├─ 입력줄 ───────────────────────────────────────────────────┤
│  👤 > _                                                   │
└────────────────────────────────────────────────────────────┘
```

### 6.4 응답 흐름

1. 사용자가 입력줄에 `2` + Enter.
2. `HumanTUIAdapter._deliver("2")` — `_pending_question` 존재 확인.
3. `content = options[1]` (원문 치환, D7) → `session.human_send(thread_id=_pending_question.thread_id, content="코드 가독성 우선", mentions=[_pending_question.agent_id])`.
   - `human.tui.response_format: number`면 번호 그대로 `content="2"` 전송.
   - 회신은 **질문한 에이전트에게만** (브로드캐스트보다 정밀 — agent-1 §2.3).
4. 패널 `status="answered"` → 툴바에서 제거. 로그에는 "사용자가 선택지 2번 응답" 기록.

### 6.5 입력 → 전달 규칙

| 입력 | 동작 |
|------|------|
| 일반 텍스트 | `session.human_send(thread_id=최근 활성 스레드, content=text)` → 에이전트 브로드캐스트 |
| `1`, `2`, ... (선택지 표시 중) | 해당 옵션 텍스트로 치환 후, 질문 스레드 + 질문 에이전트에게만 전달 (D7/D10) |
| `/quit` 또는 Ctrl+D | 입력 루프 종료 (세션은 계속 진행 — 패시브 원칙) |
| 빈 줄 | 무시 |

---

## 7. config 스키마 확장

```yaml
human:
  id: human                 # 기존 (예약어)
  display_name: "사용자"     # 기존
  interface: tui             # ★ 신규: cli(기존 input()) | tui(prompt_toolkit) | discord | file
  tui:                       # ★ 신규 (interface: tui일 때만 사용)
    response_format: text    # number | text — 선택지 응답을 번호로 보낼지 원문으로 보낼지 (D7)
    history_file: ~/.agent-augury/human_history.txt   # FileHistory 경로 (R6)
    input_prompt: "👤 > "    # 입력 프롬프트 문자열
    multiline: true          # 멀티라인 입력 (R5)
    full_screen: false       # v1.1+: 레벨 C (true) — 기본 false (레벨 B)
    choice_queue: false      # v1.1+: 다중 ask_user 순차 노출 큐
    pin_options: true        # ask_user 선택지 하단 고정 여부 (false면 기존 로그 출력 유지 — D11)
```

- `config.py` 검증: `interface` 허용값에 `"tui"` 추가. `tui:` 섹션 키 화이트리스트 검증.
- **후방 호환:** `interface: cli`/미지정 → 기존 `input()` 동작 100% 유지. `--interactive` + `interface: tui`일 때만 TUI 활성화.
- CLI: `agent-augury --config session.yaml --interactive` (기존 플래그 그대로, interface 값만 분기).

---

## 8. 구현 로드맵

| 단계 | 범위 | 산출물 | 통과 기준 |
|------|------|--------|-----------|
| **v1.0** | prompt_toolkit 의존성 추가, `channel/human_tui.py` (HumanTUIAdapter: 입력 루프 + bottom_toolbar 선택지 패널 + 번호 응답 + 히스토리), `loop.py` 안 A(1줄), `interface: tui` config, 비-TTY fallback, `[ask-user]` 로그 스킵(D12) + ask_user 로그 출력 대체(D11), 세션 정리(D13) | `channel/human_tui.py`, `agent/loop.py`(1줄), `config.py`, `cli.py`, `pyproject.toml`, `examples/human_tui_demo.yaml`, `tests/test_human_tui.py` | 시나리오 A~F (아래) |
| **v1.1** | PendingQuestions 큐, 응답 타임아웃, full-screen 레벨 C(선택), (필요 시) `Message.kind` 정식화 + aiosqlite ALTER TABLE | `human_tui.py` 개선 (+`tui/` 패키지 분리), `server.py`(선택) | 연속 ask_user 처리, 타임아웃 시 기본 경로, 영속화 ask_user 재생 |
| **v2.0 (선택)** | textual 전면 개편형 채팅 TUI (`[tui-textual]` extras) | `channel/human_tui_textual.py` | RichLog 이식 + OptionList 선택 E2E |

### v1.0 통과 기준 (자동 검증)

```
시나리오 A (상시 입력):
- fake 백엔드로 에이전트가 오래 도는 동안 HumanTUIAdapter에 PipeInput으로 "방향 바꿔줘" 주입.
- session.human_send가 호출되고, 에이전트 다음 step()의 [radio]에 from human: 방향 바꿔줘 포함.
- assert human_send 호출 → 에이전트 inbox push 확인
- assert 다음 step() drained_count >= 1

시나리오 B (선택지 고정):
- 에이전트가 ask_user(question, options=["A","B","C"]) 호출 → tool 이벤트 수신 (D8).
- HumanTUIAdapter._pending_question이 해당 질문으로 갱신 (로그 스크롤과 무관하게 툴바 표시).
- PipeInput으로 "2" 주입 → _deliver("2") → options[1] 텍스트로 치환되어 human_send.
- assert _pending_question["options"] 저장됨
- assert _resolve_option("2") == options[1]
- 에이전트 다음 step()이 해당 응답 흡수.

시나리오 C (ask_user 식별 — D8 + 이벤트 순서):
- tools.py ask_user 호출 시 기존 tool 이벤트(tool=="ask_user")에 question/options가
  구조적 필드로 포함되어 발생 (별도 이벤트 추가 없음).
- assert on_tool_call 이벤트의 args.question == "..." / args.options == [...]
- assert send_message([ask-user]) 이벤트 → tool(ask_user) 이벤트 FIFO 순서 (skip-then-show)

시나리오 D (thread ref — 안 A, V3 블로커):
- ask_user의 thread가 "$thread:0" ref인 경우 on_tool_call에 resolve된 실제 thread id가 전달됨.
- assert tool 이벤트의 args.thread == "thread-1" (또는 실제 id, "$thread:0" 아님)
- human_send(thread_id=실제 id, ...) 성공 — KeyError 없음

시나리오 E (중복 표시 방지 — D11/D12):
- TUI 모드: ask_user 수신 시 기존 👤 asks: 로그 출력 억제, [ask-user] prefix
  send_message 로그 스킵 → 질문이 화면에 1번만 표시.
- assert cli 로그 출력에 ask_user 질문 1회만 등장
- 비TUI 모드: 기존 중복 출력(💬 + 👤) 그대로 — 회귀 없음

시나리오 F (응답 라우팅 + 후방 호환 + 세션 정리):
- human_send(thread_id=질문 스레드, mentions=[질문 에이전트]) → 해당 에이전트만 [radio] 흡수.
- assert human_send mentions == [질문한 agent_id]
- interface 미지정/cli → 기존 input() 경로, TUI 코드 미활성, 크래시 없음.
- 비-TTY 환경 (stdin 파이프)에서 --interactive → 기존 input() 모드로 자동 전환.
- 세션 종료 시 PendingQuestions/pinned 초기화 + ps.app.exit() 호출 (D13).
```

---

## 9. 리스크와 대응

| 리스크 | 대응 |
|--------|------|
| prompt_toolkit과 rich 출력 경쟁 | v1.0 레벨 B: bottom_toolbar는 프롬프트 재렌더링과 분리되어 갱신 → 깜빡임 제한적 (agent-2 검증). 심하면 레벨 C(자체 버퍼) |
| 선택지가 로그에도 남아 중복 노출 | TUI 모드에서 `[ask-user]` prefix send_message 로그 스킵 + 기존 ask_user 로그 출력 대체 (D11/D12) — 질문이 화면에 1번만 |
| **thread ref 블로커 (V3)** | **안 A (loop.py 1줄)** — v1.0 필수. cli에서 ref 파싱 불필요 (agent-1/2/4 합의) |
| 동시 질문 다수 | 최신 1개 우선 (D3), v1.1에서 PendingQuestions 큐 + "대기 질문 N개" 표시 |
| textual이 더 나은 선택일 가능성 | agent-4 조사 반영 — full-screen 전제가 목적(로그 유지)과 상충 → **배제 확정**. 전환 시 RichLog 위젯으로 로그 렌더러 재사용 가능 (v2.0 경로) |
| 비-TTY/CI에서 TUI 크래시 | `check_tty()` fallback (D5), CI 테스트는 PipeInput으로 TUI 로직 검증, 실제 TTY는 수동 |
| Windows 터미널 호환 | prompt_toolkit win32 네이티브 지원. **Windows Terminal(ConPTY) 사용 안내** (agent-4: 구형 cmd.exe는 textual 한정, prompt_toolkit은 ConHost도 동작) |
| prompt_async가 종료되지 않아 세션 종료 지연 | 세션 종료 시 `ps.app.exit()` 명시 호출 + 태스크 cancel (D13, agent-1 §2.4) |
| max_steps 예산이 응답 대기 중 소진 | 기존 정책 유지 — 선택지 응답도 fire-and-forget HITL. 타임아웃/기본 경로는 v1.1 |
| `on_tool_call` args 변경 회귀 (test_wiring/test_parallel) | 안 A 채택 시 thread만 resolve 후 값으로 변경 — 해당 테스트 예상값 갱신 필요 (agent-1/2 확인) |
| 의존성 추가 부담 | prompt_toolkit은 경량 순수 Python. `dependencies`에 추가 (T-D3 논의) |

---

## 10. 열린 결정 (Open Decisions)

| # | 항목 | 상태 |
|---|------|------|
| T-D1 | 1차 라이브러리: prompt_toolkit vs textual | ✅ **prompt_toolkit 확정** (agent-1/2/4 동의). textual은 v2.0 전면 개편 옵션 |
| T-D2 | 선택지 응답 포맷: 번호 vs 원문 (D7) | ✅ 기본값 **text(원문)** 확정 — 에이전트가 의미 파악 쉬움. config `response_format`으로 전환 |
| T-D3 | prompt_toolkit 의존성 위치: 핵심 `dependencies` vs `[tui]` extras | ⏳ agent-2/agent-1: extras 제안(`[tui]`), agent-4: 핵심 dependencies 제안. **첫 릴리스 단순성 vs 최소 설치 트레이드오프** (OSS_STRATEGY §2.2 연계) — 사용자/메인테이너 결정 필요 |
| T-D4 | 선택지 패널: 최신 1개 vs 큐 | ✅ 최신 1개 (v1.0), PendingQuestions 큐 (v1.1) |
| T-D5 | 로그 영역: 기존 rich Console 유지 vs prompt_toolkit 버퍼 전환 | ✅ v1.0은 rich Console 유지 (침습 최소), full-screen 레벨 C에서 버퍼 전환 |
| T-D6 | 대상 스레드 결정: 첫 스레드 vs 최근 활성 스레드 추적 | ✅ **안 A (loop.py 1줄: resolve된 args)** 로 질문 스레드 정확 획득 + 최근 활성 스레드 fallback (D10, v1.0 포함) |
| T-D7 | ask_user 식별: 별도 이벤트(C안) vs 기존 tool 이벤트(D8) | ✅ **D8 (기존 tool 이벤트) 확정** — 팀 합의. C안 철회 (agent-2). v1.1에서 `Message.kind` 필요 시 재검토 |

---

## 11. 변경 파일 요약 (팀 최종 합의 — 5개 + 신규)

```
src/agent_augury/
  channel/human_tui.py      # ★ 신규: HumanTUIAdapter (입력 루프 + pinned 선택지 패널)
  agent/loop.py             # (1줄, 안 A) on_tool_call에 resolve된 args 전달 — V3 블로커 해결 (필수)
  cli.py                    # (소) --interactive 분기: interface=tui → HumanTUIAdapter 사용,
                            #      ask_user 로그 출력 대체/억제(D11), [ask-user] prefix send_message 로그 스킵(D12)
  config.py                 # (소) human.interface 검증에 'tui' 추가 + human.tui 키 검증
  agent/tools.py            # (변경 없음) ask_user는 그대로 — SSOT 불변 (D8)
  server.py                 # (변경 없음) human_send 그대로 — SSOT 불변 (D8)
  session.py                # (변경 없음) on_tool_call lambda 시그니처 동일 (파라미터명 args)
  agent/system_prompt.py    # (변경 없음)
pyproject.toml              # prompt-toolkit 의존성 추가 (T-D3: dependencies 또는 [tui] extras)
examples/
  human_tui_demo.yaml       # 신규: TUI 데모 (fake 백엔드, ask_user 포함)
tests/
  test_human_tui.py         # 신규: HumanTUIAdapter 단위 테스트 (PipeInput 헤드리스)
                           # + 기존 test_human_in_the_loop.py / test_reserved_names.py 전체 통과 유지
                           # + test_wiring/test_parallel의 on_tool_call args 예상값 갱신 (thread resolve 후)
```

---

## 12. 참고 자료

- `USER_INTERVENTION_DESIGN.md` v1.1 — 기존 HITL 설계 (이 문서의 상위 호환)
- `LIBRARY_RESEARCH_prompt_toolkit.md` — agent-2 조사 (prompt_toolkit API/3레벨/리스크)
- `TUI_INPUT_BAR_DESIGN.md` — agent-4 조사 (textual 상세 + 구현 방향 + 테스트 전략)
- `TUI_INPUT_DESIGN.md` — agent-2 통합 초안 (D8 합의로 정정)
- `VERIFICATION_TUI_INTEGRATION.md` — agent-1 통합/검증 리포트 (V1~V7, thread ref 블로커 상세)
- `IdeaProjects/agent-augury/src/agent_augury/{server,cli,session,config}.py` — 현행 구현
- `IdeaProjects/agent-augury/src/agent_augury/agent/{loop,tools,system_prompt}.py` — ask_user/프롬프트
- `IdeaProjects/agent-augury/tests/test_human_in_the_loop.py`, `test_reserved_names.py`, `test_wiring.py`, `test_parallel.py`
- prompt_toolkit 공식 문서 — PromptSession / Application / Layout / patch_stdout / PipeInput
- textual 공식 문서 — RichLog / OptionList / Input / run_test / ConPTY
