# agent-augury 상시 입력창(TUI) — prompt_toolkit 조사 문서

> 작성: agent-2 (2026-09) · 담당: prompt_toolkit 상세 조사
> 상위 문서: `TUI_ALWAYS_ON_INPUT_DESIGN.md` (agent-3 통합본, v0.4)
> 목적: 사용자 요청 — ① 세션 도중 언제든 inbox에 메시지 주입, ② `ask_user` 선택지가
> 에이전트 로그에 밀려 사라지지 않도록 고정(pin)하는 상시 입력창 도입을 위한
> prompt_toolkit 적합성 조사.
> Rev: v2.2 — **D8 합의 + agent-4 보강 2건 + agent-1 thread ref 블로커 검증(안 A) 반영**

---

## 1. 결론 요약 (TL;DR)

| 항목 | 판단 |
|------|------|
| 비동기 통합 | ✅ `prompt_async()` / `Application.run_async()` — asyncio first-class |
| 기존 `_human_input_loop` 대체 | ✅ `run_in_executor` 제거 가능 |
| 선택지 고정(pin) | ✅ `bottom_toolbar` 고정 패널 (레벨 B, v1.0) / full-screen (레벨 C, v1.1+) |
| 기존 rich 출력 파이프라인 | ✅ patch_stdout으로 공존 (레벨 B) — full-screen 선택 시 개편 필요 |
| ask_user 식별 | ✅ **기존 `tool` 이벤트(`tool=="ask_user"`) 사용 — 변경 0 (D8)** |
| thread ref 해석 | ✅ **안 A: `loop.py` 1줄** (on_tool_call에 resolve된 args 전달) — v1.0 필수 |
| 대상 스레드 | `args["thread"]`(resolve 후)로 정확 회신 + 최근 활성 스레드 폴백(C안) |
| Windows + 한글 IME | ✅ 네이티브 콘솔 API + IME 조합 지원 |
| 의존성 부담 | ✅ 순수 Python, 경량 (wcwidth/pygments) |
| **최종 판정** | **prompt_toolkit 1안 (v1.0), textual 2안 (v2.0 전면 개편 옵션)** |

**핵심 판단:** agent-augury의 요구(로그 흐름 유지 + 하단 상시 입력창 + 선택지
고정)는 **기존 rich 인라인 로그 파이프라인을 유지한 채 입력층만 교체**하는
prompt_toolkit 방식이 가장 적합하다. textual은 full-screen 전제 때문에 기존
로그 출력과 공존이 불가능해 "전면 개편" 시나리오로 분류한다.

---

## 2. prompt_toolkit 개요

- **제작:** Jonathan Slenders (IPython/PTPython 계열, `ptpython`의 기반).
- **성격:** 터미널 UI 프레임워크 중 "입력/편집기 중심" 라이브러리. 텍스트 입력,
  멀티라인 편집, 히스토리, 자동완성, 검증, 키바인딩, 레이아웃을 제공.
- **Python:** 3.9+ (3.11~3.13 호환) — agent-augury의 `requires-python >=3.11` 충족.
- **Windows:** win32 콘솔 API + colorama 기반. Windows Terminal / ConHost 모두 동작.
- **라이선스:** BSD-3-Clause — Apache-2.0 프로젝트와 충돌 없음.

### 2.1 왜 이 프로젝트에 어울리는가

agent-augury의 구조는 DESIGN.md §3.5.4에 따라 **단일 asyncio 이벤트 루프 + 병렬
asyncio.Task**다. 현재 HITL 입력은 `cli._human_input_loop`가
`loop.run_in_executor(None, input)`로 블로킹 `input()`을 스레드로 돌리는
우회 방식이다. prompt_toolkit은:

- `await session.prompt_async(...)` — **이벤트 루프 위에서 직접** 입력을 기다림
  (별도 스레드/executor 불필요).
- `Application.run_async()` — full-screen 앱도 루프 위에서 동작.

즉, 기존의 "블로킹 I/O를 스레드로 밀어내는" 우회가 **아키텍처 레벨에서 사라진다.**

### 2.2 prompt_toolkit vs textual (agent-4 조사 결과 반영)

| 차원 | prompt_toolkit | textual |
|------|----------------|---------|
| 모델 | 인라인 프롬프트 + 레이아웃 | **full-screen** (alternate screen buffer) |
| 기존 rich 로그 공존 | ✅ patch_stdout / bottom_toolbar | ❌ full-screen 전환 시 print 기반 로그 안 보임 |
| 선택지 패널 | `bottom_toolbar` (하단 고정) | `OptionList` 위젯 (강력) |
| asyncio | `prompt_async()` / `run_async()` | `App.run_async()` / `@work` 워커 |
| 입력 위젯 | `PromptSession` / `TextArea` | `Input` / `TextArea` (멀티라인) |
| 히스토리 | `FileHistory` | `TextArea` 내장 히스토리 |
| Windows | win32 네이티브 (ConHost 포함) | **ConPTY 필수** (Windows 10 1809+, Windows Terminal/VS Code) |
| 한글 IME | 입력 이벤트 기반 조합 | 터미널 의존적 (ConPTY 환경) |
| CSS/reactive | 없음 (코드 레이아웃) | CSS 스타일링 + reactive (`@watch`) |
| 러닝 커브 | 낮음 (기존 rich 사용자) | 중간~높음 (CSS/reactive 패러다임) |
| 적합 시나리오 | **로그 유지 + 입력창/패널 추가** | **전면 개편 (채팅 앱 스타일 TUI)** |
| 의존성 | 경량 (wcwidth/pygments) | rich 내장 (이미 rich 의존성과 겹침) |

> textual 상세는 agent-4 문서(`TUI_INPUT_BAR_DESIGN.md`) 참조.

---

## 3. 핵심 API — 상시 입력창 관점

### 3.1 `PromptSession` (세션 유지형 프롬프트)

```python
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory

session = PromptSession(
    history=FileHistory(str(Path.home() / ".agent-augury" / "history.txt")),
    auto_suggest=AutoSuggestFromHistory(),
    enable_open_in_editor=True,      # 긴 입력: $EDITOR 열기
    multiline=True,                  # 멀티라인 입력 (빈 줄 = 제출 설정 가능)
)

# 비동기 — run_in_executor 불필요
text = await session.prompt_async("❯ ")
```

- 기존 `_prompt_multiline`("빈 줄까지 읽기")은 `multiline=True` +
  `PromptSession` 키바인딩으로 대체 가능.
- 히스토리 → 세션 간 입력 재사용. agent-augury의 `~/.agent-augury/` 디렉토리에 저장.

### 3.2 `patch_stdout` — 로그와 프롬프트 공존 (레벨 B 최소 변경 경로)

```python
from prompt_toolkit import patch_stdout

with patch_stdout():
    # 이 블록 안의 print()/rich Console 출력은 프롬프트를 침범하지 않고
    # 위쪽 로그 영역으로 흘러간다.
    ...
```

- **원리:** stdout을 패치해 출력 발생 시 프롬프트를 잠시 숨기고, 출력 후 복원.
- rich의 `Console`(현재 `cli.py`의 `_console`)과 함께 사용 가능.
- **한계:** 고빈도 로그 스트리밍 시 매 출력마다 프롬프트 재렌더링 → 깜빡임(flicker)
  가능. 단, 레벨 B는 `bottom_toolbar`가 프롬프트 재렌더링과 분리되어 갱신되므로
  실제 깜빡임은 제한적.

### 3.3 `Application` + `Layout` — full-screen (레벨 C, v1.1+)

```python
from prompt_toolkit.application import Application
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

log_window = Window(FormattedTextControl(log_content), wrap_lines=True)  # 위: 로그
pinned = Window(FormattedTextControl(pinned_content), height=3)          # 중간: 고정 선택지
input_box = TextArea(multiline=True, focusable=True)                     # 아래: 입력

root = HSplit([log_window, pinned, input_box])
app = Application(layout=Layout(root), full_screen=True)
await app.run_async()
```

- `full_screen=True` → alternative screen buffer로 전환, 자체 화면 구성.
- `FormattedTextControl`은 rich 스타일 토큰/HTML/ANSI 문자열을 지원 → 기존
  `_log_step`/`_log_tool_event`의 rich 렌더링 결과를 재사용 가능.
- 로그 갱신: `app.invalidate()` + 버퍼 내용 교체.
- **대가:** rich `Console` 직접 출력을 버퍼 렌더링으로 전환해야 함 → `cli.py`와
  `session._output_consumer`의 표시 경로 개편 필요 (agent-1 검토 영역).

### 3.4 `PipeInput` — 테스트/자동화

```python
from prompt_toolkit.input import PipeInput

pipe = PipeInput()
app.input = pipe
pipe.send_text("postgres로 결정\n")   # 프로그램적으로 입력 주입
```

- E2E 테스트에서 human 입력 시뮬레이션의 핵심. 기존
  `test_human_in_the_loop.py`의 `session.human_send()` 주입과 병행 가능.
- non-TTY/CI 환경에서도 입력 파이프라인 테스트 가능.

### 3.5 키바인딩/이벤트

- `KeyBindings` 객체로 커스텀 키 추가 (예: `Ctrl+P` = 선택지 접기/펼치기,
  `Ctrl+O` = 에디터 열기).
- `prompt_async`의 `bottom_toolbar` 파라미터에 `FormattedTextControl`을 넘기면
  **프롬프트 하단 고정 영역**에 선택지를 표시할 수 있다 (§5 레벨 B).

---

## 4. 기존 코드와의 통합 지점 (코드 근거)

| 현재 코드 | 역할 | prompt_toolkit 도입 시 |
|-----------|------|------------------------|
| `cli.py::_human_input_loop` | `run_in_executor`로 `input()` → `session.human_send()` | `PromptSession.prompt_async()`로 대체 (스레드 제거) |
| `cli.py::_prompt_multiline` | 빈 줄까지 멀티라인 읽기 | `multiline=True` + 키바인딩 |
| `cli.py::_log_tool_event` (ask_user 분기) | 질문/선택지를 콘솔에 출력 | TUI 모드: pinned 패널 표시로 **대체** (로그 출력 억제) |
| `cli.py::_log_step` | agent 텍스트 rich 렌더링 | full-screen 시 `FormattedTextControl` 버퍼로 |
| `session._output_queue → _output_consumer` | 표시 이벤트 집중화 | 유지 (표시 대상만 TUI 위젯으로 교체) |
| `server.ask_user` → `send_message(mentions=["human"])` | 질문을 human inbox로 push | 변경 없음 — **SSOT 불변** |
| **`loop.py::step()` (안 A)** | `on_tool_call`에 **resolve 전** `call.arguments` 전달 | **resolve된 `args` 전달 (1줄)** — thread ref 해석 |

> 핵심 원칙: TUI는 **표시/입력 계층**만 바꾼다. `MessageServer`(SSOT),
> `ask_user` 도구, `human_send` 경로는 손대지 않는다. (DESIGN.md §3.3 "채널은 뷰")

### 4.1 ✅ ask_user 식별 — D8 확정 (기존 tool 이벤트 사용, 변경 0)

**배경:** `tools.py`의 ask_user는 `server.send_message(mentions=["human"])`으로
구현되어 서버 이벤트로는 `send_message`로만 보인다. 선택지 고정을 위해선
ask_user를 식별할 신호가 필요하다.

**초기 제안(C안: 별도 ask_user 이벤트) — 이후 철회 (agent-3 정정 반영):**
실제 코드를 보면 이미 구조적 경로가 존재한다:

```python
# session.py from_config — on_tool_call 콜백 (이미 구현됨, 변경 0)
on_tool_call=lambda agent_id, tool, args, result, _server=server: (
    _server._emit_event({
        "type": "tool",
        "agent_id": agent_id,
        "tool": tool,          # ← "ask_user"
        "args": args,          # ← {question, options, thread} 구조적 데이터
        "timestamp": __import__("time").time(),
    })
)
```

- `tools.py`의 ask_user는 `args`에 `question`/`options`/`thread`를 그대로 담고,
  `on_tool_call`이 이를 `type:"tool"` 이벤트로 이미 emit.
- `cli._log_tool_event`에 `if tool == "ask_user":` 분기가 이미 존재.
- **즉, 별도 이벤트 추가 없이** `cli.on_tool_event`에서 `tool=="ask_user"` 분기만으로
  구조적 데이터 수신 가능.

**D8 확정 합의 (agent-1/2/3/4):**

| 항목 | 합의 |
|------|------|
| ask_user 식별 기본 경로 | **기존 `tool` 이벤트(`tool=="ask_user"`) 사용** — 구조적 데이터 |
| send_message 로그 중복 방지 | cli `_log_tool_event`에서 send_message 이벤트의 content가 `[ask-user]`로 시작하면 **로그 출력 스킵** (TUI 모드 한정) — prefix는 보조 신호 |
| ask_user tool 이벤트 로그 | TUI 모드: `👤 asks:` 로그 대신 **pinned 패널 표시** (로그 억제, agent-4 보강 ②) |
| v1.1 정식화(선택) | 영속화 재생 시 식별이 필요해지면 `Message.kind` 필드 + aiosqlite ALTER TABLE — 그때까지 보류 |

**이벤트 순서 (skip-then-show, agent-1 검증):**
`loop.py step()`에서 `_execute_tool` 내부의 `server.send_message`가 **먼저**
`send_message` 이벤트를 emit하고, 그 후 `on_tool_call`이 tool 이벤트를 emit한다.
같은 asyncio 루프 + FIFO 큐이므로 cli 처리 순서는:
1. `send_message` 이벤트 (content=`[ask-user] ...`) → TUI 모드에서 **스킵** (prefix 감지)
2. `tool(ask_user)` 이벤트 → **pinned 패널 갱신**
→ "먼저 숨기고, 그다음 고정 표시" 순서가 구조적으로 보장된다.

### 4.2 ✅ thread ref 해석 — 안 A (agent-1 블로커 검증, v1.0 필수)

**블로커:** `loop.py step()`의 `on_tool_call`은 **resolve 전** `call.arguments`를 전달한다:

```python
# loop.py step() — 현재 구현
args = self._resolve_refs(call.arguments)          # ① resolve된 args
result = await self._execute_tool(call.name, args) # ② 실행은 resolve된 값으로
...
self.on_tool_call(self.agent_id, call.name, call.arguments, result)  # ③ ★ resolve 전 원본!
```

- ask_user의 `thread`가 `"$thread:0"`이면 cli는 `$thread:0`을 받는다.
- 그대로 `session.human_send(thread_id="$thread:0", ...)` → **`KeyError: no such thread`** — v1.0 E2E 실패.

**해결안 3종 비교:**

| 안 | 방식 | 변경 범위 | 정확성 | 판정 |
|----|------|-----------|--------|------|
| **A** | `loop.py`의 `on_tool_call` 호출부를 `call.arguments` → **resolve된 `args`** 로 변경 (1줄) | loop.py 1줄 | ✅ 항상 실제 thread id | **채택 (v1.0 필수)** |
| B | cli에서 `$thread:N` 파싱 + 에이전트별 created_threads 추적 | cli.py 추가 로직 | △ 에이전트별 목록을 cli가 모름 → 부정확 | 비권장 |
| C | cli에서 "최근 활성 스레드" 폴백 | cli.py | △ 대부분 동작하나 근사 | v1.0 폴백 |

**안 A 상세:**

```python
# loop.py — 변경
self.on_tool_call(self.agent_id, call.name, args, result)   # resolve된 args
```

- `args`는 이미 `_resolve_refs`를 거친 값 — `$thread:N` → 실제 `thread-<n>`.
- session.py lambda 파라미터명 `args` 그대로 — **시그니처 불변**.
- cli `_log_tool_event`의 기존 tool 로그(path 등)도 resolve 후 값으로 일관.
- **회귀 확인 포인트:** `test_wiring.py`/`test_parallel.py` 등에서 `on_tool_call`의
  args를 검증하는 테스트가 있다면 resolve 전/후 차이(파일 도구는 동일, thread만 실제 id) 확인.

**응답 라우팅:** `session.human_send(thread_id=args["thread"](resolve 후),
content=..., mentions=[질문한 agent_id])` — ask_user를 보낸 에이전트에게만 회신
(브로드캐스트보다 정밀). thread ref가 없는 경우는 최근 활성 스레드 폴백(C안).

### 4.3 ✅ ask_user 로그 출력 대체 (agent-4 보강 ②)

| 모드 | ask_user 표시 |
|------|---------------|
| `interface: tui` (TUI) | **pinned 패널 표시로 대체** — 로그 출력 억제 (`pin_options: true` 기본) |
| `pin_options: false` | 기존처럼 로그 출력 유지 |
| `interface: cli` (기존) | 현재 동작 그대로 (로그 출력) — 호환, 회귀 0 |

- send_message 쪽 `[ask-user]` prefix 스킵(D8) + `👤 asks:` 로그 억제를 함께 적용하면
  **질문이 화면에 1번만** 나타난다.

---

## 5. 선택지(ask_user) 고정 — 3레벨 설계

| 레벨 | 방식 | 선택지 고정? | 변경 범위 | 권장 |
|------|------|--------------|-----------|------|
| A | 로그 스트림에 질문 재출력 (+patch_stdout) | ❌ (스크롤로 사라짐) | 최소 | — |
| B | `PromptSession` **bottom_toolbar**에 선택지 표시 | ✅ (프롬프트가 항상 하단 고정) | 중간 | **v1.0 권장** |
| C | full-screen `Application` (로그/고정패널/입력 3분할) | ✅✅ (완전 고정, 반응형) | 큼 (표시 경로 개편) | v1.1+ |

### 레벨 B 상세 (권장, v1.0)

```python
from prompt_toolkit.formatted_text import HTML

pinned_question: dict | None = None   # ask_user tool 이벤트(§4.1 D8)에서 갱신

async def input_loop(session):
    ps = PromptSession(history=..., multiline=True)
    while True:
        text = await ps.prompt_async(
            "❯ ",
            bottom_toolbar=HTML(_render_pinned(pinned_question)),
        )
        if text.strip():
            await session.human_send(thread_id, content=text)  # thread_id = ask_user의 resolve된 thread (§4.2)
```

- `tool=="ask_user"` 이벤트(§4.1 D8)가 오면 `pinned_question`/`pinned_options`를
  갱신 → 다음 `prompt_async` 호출의 bottom_toolbar에 반영. **질문이 항상 하단에
  고정**되고 로그는 위로 흘러간다.
- 선택지(`options`)도 같은 방식으로 `(1) postgres  (2) mysql` 형태로 표시.
- 응답 후 `pinned_question = None`으로 해제 (또는 "답변 완료" 표시).
- 다중 질문 동시 발생 시: **큐(PendingQuestions)** 로 관리해 순차 노출 —
  질문이 로그에 묻히지 않고, 사용자가 하나씩 답할 수 있게 한다.

### 레벨 C 상세 (v1.1+)

- `Session._output_consumer`가 rich `Console` 대신 TUI `TextArea`/`Window` 버퍼에
  append. 로그는 스크롤 가능, 선택지 패널은 고정 높이(예: 4줄), 입력창은 하단.
- full-screen 전환 시 기존 `--config` 비인터랙티브 모드와 **분기**:
  `--interactive` + `human.tui: prompt_toolkit` + `tui.full_screen: true` (레벨 C)
  vs 기본 (레벨 B).
- textual(전면 개편 옵션)과의 최종 선택은 v2.0에서 재평가.

---

## 6. Windows / 한글 IME / 붙여넣기

| 항목 | prompt_toolkit | textual (비교) | 비고 |
|------|----------------|----------------|------|
| Windows 콘솔 | win32 API 네이티브 (ANSI 불필요) | **ConPTY 필수** (Win10 1809+) | `wizard.check_tty()`의 `AttachConsole` 경로와 호환 |
| 한글 IME 조합 | 입력 이벤트 기반 조합 지원 | 터미널 의존적 (ConPTY) | `input()`보다 안정적 |
| 멀티라인 붙여넣기 | `multiline=True` + bracketed paste | `TextArea` 지원 | 긴 task/코드 붙여넣기 가능 |
| 브래킷 페이스트 | 기본 지원 (bracketed paste) | 지원 | |
| 히스토리 | `FileHistory` | `TextArea` 내장 | 세션 간 유지 |

---

## 7. 의존성 및 설치

```toml
# pyproject.toml (제안 — T-D3 열린 결정)
[project.optional-dependencies]
tui = ["prompt_toolkit>=3.0"]
# 또는 핵심 dependencies에 추가 (agent-4 제안) — 첫 릴리스 단순성 vs 최소 설치 트레이드오프
```

- 순수 Python, 전이 의존성 경량 (wcwidth, pygments).
- rich(15.0)는 이미 필수 의존성 — patch_stdout 연동은 추가 패키지 불필요.
- **T-D3 (열린 결정):** extras(`[tui]`) vs 핵심 `dependencies` — 사용자/메인테이너
  결정 필요 (OSS_STRATEGY §2.2 연계).

---

## 8. 리스크

| 리스크 | 완화 |
|--------|------|
| patch_stdout 깜빡임 (고빈도 로그) | 레벨 B에서 bottom_toolbar는 매 프롬프트 재렌더링이 아니라 toolbar만 갱신 → 실제 깜빡임 적음. 심하면 레벨 C로 |
| full-screen 전환 시 rich 로그 경로 개편 | v1.0은 레벨 B(최소 변경), 레벨 C는 v1.1+ 별도 마일스톤 |
| prompt_async가 종료되지 않아 세션 종료 지연 | 세션 종료 시 `ps.app.exit()` 명시 호출 + `PendingQuestions` 큐·pinned 상태 초기화 (agent-1 보강) |
| 다중 질문 동시 발생 | PendingQuestions 큐로 순차 처리 + 사용자에게 "대기 질문 N개" 표시 |
| 한글 IME에서 선택지 번호 입력 충돌 | 숫자+Enter 입력은 IME 조합과 무관 (조합 중 Enter는 확정) — 기본 동작으로 충분 |
| ask_user 질문이 로그에도 남아 중복 표시 | TUI 모드: `[ask-user]` prefix send_message 로그 스킵 + `👤 asks:` 로그 억제 (§4.3) — pinned 패널이 유일한 표시 |
| **`$thread:0` ref (미해석) → KeyError** | **안 A: `loop.py` 1줄** (on_tool_call에 resolve된 args) — v1.0 필수 (§4.2). 폴백: 최근 활성 스레드 |
| on_tool_call args 변경 회귀 | `test_wiring.py`/`test_parallel.py`에서 resolve 전/후 차이 확인 (파일 도구 동일, thread만 실제 id) |
| 영속화 재생 시 ask_user 식별 불가 | v1.1에서 `Message.kind` + ALTER TABLE 정식화 (보류) |

---

## 9. 최종 권장안 (팀 합의 반영 후 확정)

### 9.1 라이브러리 선택

| 안 | 라이브러리 | 시나리오 | 판정 |
|----|-----------|----------|------|
| **1안** | **prompt_toolkit** | 로그 유지 + 하단 입력창 + 선택지 고정 (레벨 B) | ✅ **v1.0 채택** |
| 2안 | textual | 전면 개편 (채팅 앱 스타일 full-screen TUI) | v2.0 재평가 |
| 3안 | rich-only | 의존성 0이지만 진짜 입력창 불가 | ❌ (입력/히스토리/멀티라인 부족) |

### 9.2 변경 파일 (최종 합의, 4개 + 신규 2개)

```text
src/agent_augury/
  agent/loop.py        # 1줄: on_tool_call(self.agent_id, call.name, args, result) — resolve된 args (안 A)
  cli.py               # PromptSession 입력 + pinned 패널 + [ask-user] 로그 스킵 + ask_user 로그 억제
  config.py            # human.tui / pin_options 검증
  channel/human_tui.py # 신규: HumanTUIAdapter (입력 루프 + bottom_toolbar 선택지 패널)
  server.py            # (변경 없음) human_send 그대로 — SSOT 불변
  session.py           # (변경 없음) — on_tool_call lambda 시그니처 불변
  tools.py             # (변경 없음) ask_user 그대로 — D8
  system_prompt.py     # (변경 없음)
pyproject.toml         # prompt-toolkit 의존성 추가
examples/human_tui_demo.yaml  # 신규
tests/test_human_tui.py       # 신규 (PipeInput 헤드리스)
```

### 9.3 v1.0 통과 기준 (자동 검증, agent-1 단언 포함)

```python
# 1) tool 이벤트 경로 (D8 기본) + thread ref 해석 (안 A)
assert event["type"] == "tool" and event["tool"] == "ask_user"
assert event["args"]["question"] == "DB는 뭘 쓸까?"
assert event["args"]["options"] == ["postgres", "mysql"]
assert event["args"]["thread"].startswith("thread-")   # ★ resolve 후 실제 id (안 A)

# 2) TUI 모드 중복 스킵
# send_message 이벤트(content 시작이 "[ask-user]") → _log_tool_event에서 출력 생략
# tool(ask_user) 이벤트 → pinned 패널 갱신 (로그 출력 억제)

# 3) 응답 라우팅: ask_user의 thread로 human_send → 해당 에이전트만 [radio] 흡수
assert session.human_send(thread_id=event["args"]["thread"], mentions=[agent_id]) 정상
assert agent 대화에 "from human" 포함

# 4) 비TUI 회귀: human.tui 미설정 시 기존 input() 경로 + 기존 출력 그대로
#    (test_human_in_the_loop.py 등 전체 통과 유지)

# 5) $thread:0 ref E2E: fake 백엔드가 ask_user(thread="$thread:0") 호출 시
#    cli가 resolve된 thread id로 human_send → KeyError 없음
```

### 9.4 아키텍처 결정 요약 (팀 합의 D1~D10 + 안 A)

1. **입력층 (D1):** `PromptSession.prompt_async()` — `_human_input_loop`의
   `run_in_executor` 제거, asyncio 네이티브.
2. **로그층 (D4/D5):** 기존 rich 파이프라인 유지 + `patch_stdout()` 공존 (보조).
   비-TTY 환경은 `check_tty()`로 기존 input() 모드 자동 전환.
3. **선택지 고정 (D3/D7):** `bottom_toolbar` 고정 패널 (레벨 B). 최신 1개 우선 +
   v1.1 PendingQuestions 큐. 번호/원문 응답 치환 (`response_format: text` 기본).
4. **ask_user 식별 (D8):** 기존 `tool` 이벤트(`tool=="ask_user"`) — tools.py 변경 0.
   `[ask-user]` prefix는 TUI 모드에서 send_message 로그 스킵용 보조 신호.
   **ask_user tool 이벤트 로그 출력도 pinned 패널로 대체 (§4.3).**
5. **thread ref 해석 (안 A):** `loop.py` 1줄 — on_tool_call에 resolve된 args 전달.
   응답은 `human_send(thread_id=args["thread"], mentions=[질문 에이전트])` (§4.2).
6. **config (D6):** `human.interface: tui` + `human.tui:` 옵트인 — 없으면 기존
   `input()` 100% 호환.
7. **테스트 (R10):** `PipeInput` 기반 입력 주입 + `test_human_in_the_loop.py`
   기존 통과 유지 (loop.py 1줄 외 서버/세션 변경 없음).

### 9.5 근거 요약

1. **요구사항 정합:** "로그 흐름 유지 + 상시 입력 + 선택지 고정"은 prompt_toolkit의
   인라인 프롬프트 + bottom_toolbar 모델과 정확히 일치.
2. **기존 파이프라인 보존:** rich `Console` 출력(server 이벤트 → `_log_tool_event`)을
   그대로 두고 입력층만 교체 → 회귀 리스크 최소.
3. **asyncio 정합:** `prompt_async()`가 루프 위에서 동작 → `run_in_executor` 제거.
4. **Windows 안정성:** win32 네이티브라 ConPTY 의존(textual)보다 구형 환경에서 안전.
5. **텍스트/한글:** 멀티라인 붙여넣기 + IME 조합 + 히스토리 — 기존 `input()`의
   한계를 모두 해소.
6. **ask_user 식별 변경 0 + thread ref는 loop.py 1줄로 해결:** tools.py/server.py
   변경 없음, 변경 파일 최소화 (cli.py/config.py/loop.py/pyproject + 신규 2개).

---

> (agent-3) 통합 문서 `TUI_ALWAYS_ON_INPUT_DESIGN.md` v0.4에 이 조사 내용을
> 라이브러리 비교 섹션으로 흡수해주세요. D8 + agent-4 보강 2건 + **agent-1
> thread ref 블로커 검증(안 A: loop.py 1줄)** 이 반영된 최종본입니다.
