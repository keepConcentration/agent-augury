# agent-augury 세션 TUI 전면 재설계 — 통합 설계 (v2.5)

> **Status:** 설계 확정본 (구현 전 — 사용자 UX 결정 반영 완료) · **Date:** 2026-09 · **Author:** agent-3 (통합)
> **팀 합의:** agent-1(파이프라인/테스트/상태바) · agent-2(prompt_toolkit Layout/버퍼/종료/hermes 근거/입력 구현 검증) ·
> agent-4(입력 라우팅/슬래시/선택지 UX/키바인딩)
> **Rev:** v2.5 — **사용자 UX 결정 확정 반영**:
> ① **Enter 제출 확정** (D-A14-1A, 사용자 확인) — v2.3부터 반영된 `accept_handler` 방식 그대로 확정
> ② **`--repl`을 기본값으로 전환** (D-A14-2B, 사용자 확인) — REPL(세션 재사용 루프)이 **유일한 실행 모드**가 됨.
>    `--repl` 플래그 자체 제거, 반대 옵션(1회 실행 `_run`) 코드 **제거 대상 확정**
> ③ **구현 범위 = 설계만** (D-A18, 사용자 확인) — 본 문서 포함 설계 산출물만 작성, 코드 변경 없음
> ④ **빈 입력 처리 통일 (agent-4 지적 반영, D-A20)** — 기존 `_run_repl`은 "빈 입력=quit"(루프 종료)이었으나,
>    TUI router 규칙은 "빈 줄=ignored"(무시). REPL이 유일 모드가 되므로 **빈 줄 Enter = 무시(ignored)** 로 통일.
>    종료는 `/quit`/`/exit` 또는 Ctrl+D로만. (실수로 빈 Enter를 눌러 세션이 끝나는 사고 방지)
> v2.4 변경사항: agent-2 전체 검증 승인 반영. v2.3 변경사항: ① **TextArea `accept_handler` 기반 Enter 제출
> 구현 확정** (agent-2 함정 검증) ② **StatusBar `control()`/`invalidate()` 최종 원고 반영** ③ Ctrl+C="드래프트
> 클리어"(v1.0 포함) ④ §3.5 키바인딩 표 구현 컬럼 추가.
> **이 문서는 기존 초안 v1을 대체합니다.** hermes-agent UX(상태바·슬래시 명령·키 시퀀스 별칭·
> 전체 화면 전환)를 적극 반영한 **전면 재작성**본입니다.
> **문서 체계:**
> - **본 문서 (SESSION_TUI_REDESIGN.md v2.5)** — 통합 상위 문서 (권위)
> - **하위 문서 ① `INPUT_ROUTING_SLASH_UX_DESIGN.md` (agent-4, v0.5)** — router/commands/choice_panel/key_aliases 상세
>   (REPL 기본화 + 빈 입력 통일 반영)
> - **하위 문서 ② `TUI_ALWAYS_ON_INPUT_DESIGN.md` (v0.6)** — **구식 (부분 outdated)** — PromptSession 기반 B레벨 설계로
>   full-screen Application 전환(레벨 C) 후에는 **참고용으로만** 유지
> - **하위 문서 ③ `VERIFICATION_TUI_INTEGRATION.md` (agent-1)** — **부분 outdated** — "안 A(loop.py 1줄)"는
>   이미 적용됨(검증만). 파이프라인/응답 라우팅 검증 내용은 유효
> - `LIBRARY_RESEARCH_prompt_toolkit.md`(agent-2) / `TUI_INPUT_BAR_DESIGN.md`(agent-4) — 라이브러리 조사 (유효)
> - `TUI_DISPLAY_INPUT_LAYER.md`(agent-2, v1.1) — 표시/입력 계층 상세 (v2.5 본문과 정합, 구현 레퍼런스)
> - hermes-agent `hermes_cli/`(Python) · `ui-tui/`(React+Ink, UX 참고)

---

## 0. 한 줄 결론

> **rich `Console.print()`를 터미널 stdout에서 제거하고, prompt_toolkit `Application`
> (full-screen, alternate screen) 하나가 "로그 영역 + ask_user 패널 + 상태바 + 입력줄"을
> 단일 레이아웃으로 렌더링한다.** 로그는 rich를 **in-memory(`Console(record=True)`)** 렌더러로만
> 사용해 **이벤트 → ANSI 문자열 1개** 순수 함수(`render_event`)로 변환한 뒤 `LogBuffer`
> (dirty 캐시)에 넣고 `FormattedTextControl`이 렌더링한다.
> → 두 렌더러가 화면을 경쟁하는 구조가 **구조적으로 소멸**한다 (rich print가 alternate screen에
> 섞일 수 없음). 비-TTY(파이프/리다이렉트/CI)에서는 `render_event` 결과를 그대로 `print()` —
> 기존 rich 출력과 동일 → fallback 1줄로 통일.
>
> **v2.5 추가:** 실행 모드는 **REPL(세션 재사용 루프) 단일 모드**로 통일한다. `--repl` 플래그와
> 1회 실행(`_run`) 경로는 **설계상 제거**한다. "같은 Application + 세션 재사용 루프"(D-A4)가
> 곧 기본 UX가 된다. 빈 줄 Enter는 **무시**(종료 아님) — 종료는 `/quit`/Ctrl+D로만.

```
Before:  rich print ──► stdout ──┐  (독립 제어, 서로 밟음)
         PromptSession ──────────┘
After:   Session 이벤트 ─► render_event(rich record=True) ─► ANSI ─► LogBuffer ─► Application (단일 화면, full-screen)
         실행 모드: REPL 단일 (세션 재사용 루프) — --repl 플래그/1회 실행 경로 제거
         빈 줄 Enter = ignored (종료는 /quit, /exit, Ctrl+D)
```

---

## 1. 문제 정의 (왜 지금 깨지는가)

### 1.1 코드로 확인된 근본 원인

| # | 원인 | 위치 (코드 근거) |
|---|------|------------------|
| C1 | **두 개의 터미널 렌더러가 stdout 경쟁** — rich `Console.print()`(로그)와 `PromptSession.prompt_async()`(입력)가 각자 화면 갱신. `human_tui.py`의 `run_input_loop`에는 `patch_stdout` **없음** | `cli.py::_log_step/_log_tool_event`, `channel/human_tui.py::run_input_loop` |
| C2 | `_run()`에서 입력 태스크(`human_task`)와 `session.run()`을 병렬로 돌리지만, **로그 출력이 별도 경로**라 렌더링 동기화 수단이 없음 | `cli.py::_run` |
| C3 | 로그가 stdout에 직접 쓰이므로 **스크롤백/이력 관리 불가** — 사용자가 지난 출력을 되돌아볼 수 없음 | `cli.py` rich print |
| C4 | `_log_step`/`_log_tool_event`가 **2회 print** (헤더 + Markdown) — record 모드 이관 시 단일 블록으로 조립 필요 | `cli.py` |
| C5 | **"전송됨" 피드백 부재** — 입력이 실제로 human_send 됐는지 화면에서 확인 불가 → "입력이 안 먹힌다"는 인식 | `channel/human_tui.py::_deliver` (출력 없음) |
| C6 | **실행 모드 2개 분기 (v2.5 추가)** — `--repl` 유무에 따라 `_run_repl`(세션 재사용)과 `_run`(1회)이 갈라짐. TUI 설계(상시 입력)와 1회 실행 모드는 UX가 어긋남 (1회 실행 후 종료되면 상시 입력 의미가 반감) | `cli.py::main`, `_run_wizard_flow(repl=...)` |
| C7 | **빈 입력 의미 불일치 (v2.5 추가, agent-4 지적)** — 기존 `_run_repl`은 빈 입력=`quit`(루프 종료)이지만, v2.4 TUI router는 빈 줄=`ignored`(무시). REPL이 유일 모드가 되면 반드시 통일 필요 | `cli.py::_run_repl` while 루프 vs `tui/router.py::route` |

> 기존 문서들이 "v1.0 블로커"로 지목한 **thread ref(안 A)는 현재 코드에 이미 적용됨**
> (agent-1/agent-4 공동 확인): `loop.py::step()`이 `self.on_tool_call(self.agent_id, call.name, args, result)`로
> **resolve된 `args`** 를 전달. → 설계 범위에서 **변경 불필요(검증만)**.
> `prompt-toolkit>=3.0.53`도 `pyproject.toml` dependencies에 **이미 포함** → 의존성 추가 불필요.

### 1.2 관찰된 증상

- "--- Initial Task ---" 아래 화면이 흰 바탕으로 스크롤, 입력 불가처럼 보임
- TUI 입력줄(`👤 >`)이 렌더링되지만 **타이핑한 메시지가 전송되지 않는 것처럼** 보임
  (실제로는 입력되나 로그가 입력줄을 덮어 화면 갱신이 밀림)
- ask_user 선택지가 로그 스크롤에 밀려 사라짐
- (v2.5) 위저드 종료 후 `--repl` 없이 시작하면 **세션 1회 후 종료** — 연속 질문하려면 `--repl`을
  켜야 한다는 인지 부담 (사용자 요청의 배경)
- (v2.5) 기존 REPL에서 빈 Enter를 누르면 루프가 종료되어버림 — "Enter=제출" UX와 결합 시
  실수로 세션을 죽이는 사고 가능 (agent-4 지적)

### 1.3 이 설계가 풀어야 할 요구

| ID | 요구 | v2.5 해법 |
|----|------|-----------|
| R1 | 로그와 입력줄이 **절대 안 밟힘** | 단일 `Application` full-screen 렌더링 (구조적 해결) |
| R2 | 세션 도중 **언제든** 입력 가능 | 입력줄은 레이아웃 하단 고정 (상시) |
| R3 | ask_user 선택지 **고정 표시 + 번호 응답** | 중간 패널 (`ConditionalContainer`) |
| R4 | 에이전트 로그 실시간 스트리밍 | LogBuffer(dirty 캐시) + `app.invalidate()` |
| R5 | 히스토리 / 멀티라인 / 붙여넣기 / 한글 IME | `TextArea(multiline=True, accept_handler=...)` + `FileHistory` + 키 별칭 |
| R6 | 기존 로그 **포맷(마크다운/아이콘/마스킹) 재사용** | `render_event` 순수 함수 (rich record=True → ANSI) |
| R7 | 비-TTY 회귀 0 | `isatty()` 분기 — `render_event` 결과를 그대로 print (fallback 1줄) |
| R8 | 세션 상태 가시성 (gate/phase/steps) | **상태바 v1.0 포함** (4분할 Layout) — 매 렌더 스냅샷 + 1초 타이머 트리거 |
| R9 | 사용자 제어 명령 | 슬래시 명령 5종+quit (hermes 패턴) |
| R10 | "전송됨" 확인 | human_send 성공 시 로그에 `👤 → {thread}` 피드백 1줄 (v1.0 필수) |
| R11 | 테스트 가능 | `PipeInput` + 순수 함수(router/render_event/LogBuffer/commands) 분리 |
| R12 | **REPL 단일 모드 (v2.5 추가)** | 위저드/--config 모두 같은 세션 재사용 루프. `--repl` 플래그·1회 실행 경로 제거 |
| R13 | **빈 입력 안전 (v2.5 추가)** | 빈 줄 Enter = 무시(ignored). 종료는 `/quit`/`/exit`/Ctrl+D로만 — 실수 종료 방지 |

---

## 2. 목표 아키텍처 (hermes 패턴 채택)

### 2.1 구조 원칙

1. **화면은 하나의 렌더러만 제어한다.** → prompt_toolkit `Application`(full-screen)이 유일한
   stdout 소유자. alternate screen buffer라 외부 print가 섞일 수 없음 (agent-2 검증).
2. **rich는 "렌더링 엔진"으로만 남는다.** → `Console(record=True, file=io.StringIO())`로
   렌더링하고 `export_text(styles=True)`로 ANSI를 얻는다. 기존 포맷 코드를 **순수 함수
   `render_event(event) -> str | None`** 로 재구성 (2회 print → 1개 ANSI 블록, agent-1 지적 해결).
3. **에이전트 출력 경로는 불변.** → `Session._output_queue → cli.on_tool_event` 유지.
   소비자만 "rich print" → `render_event` → `LogBuffer.append`로 변경.
4. **입력·선택지·상태는 앱 레이아웃의 일부.** → ask_user 패널, 상태바, 입력줄이 로그와
   같은 렌더 사이클에서 갱신 → 서로 절대 안 밟음.
5. **SSOT/프로토콜 불변.** → `server.py` / `session.py` / `tools.py` / `system_prompt.py` 변경 없음.
6. **TUI는 순수 표시 계층.** → `channel/human_tui.py`는 얇은 어댑터로 남기고 내부는 `tui/` 위임.
   (agent-2 구조 제안 수용 — 테스트 재작성 범위가 `tui/` 단위로 명확해짐)
7. **실행 모드 단일화 (v2.5 추가).** → REPL(세션 재사용 루프)이 **유일한 실행 모드**.
   `--repl` 플래그 제거, 1회 실행(`_run`) 경로 제거. `main()`은 항상 세션 재사용 루프로 진입.
8. **빈 입력 = 무시 (v2.5 추가).** → router의 `ignored` 규칙을 REPL 루프 전체에 적용.
   종료는 `/quit`/`/exit`/Ctrl+D만.

### 2.2 전체 구조 — **Layout 4분할 확정 (agent-1/2 합의)** + **REPL 단일 루프**

```
┌───────────────────────────────────────────────────────────────┐
│              SessionTUIApplication (prompt_toolkit)           │
│  Layout = HSplit (full_screen=True — alternate screen)        │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ ① 로그 영역  Window(FormattedTextControl ← LogBuffer)    │  │  ← 1fr (스크롤)
│  │    · step 요약 / tool 이벤트 / send_message / read_...    │  │
│  │    · render_event(rich record=True) → ANSI → 버퍼         │  │
│  │    · v1.0: tail view 자동 (Window wrap_lines 기본)        │  │
│  └─────────────────────────────────────────────────────────┘  │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ ② ask_user 패널  ConditionalContainer(활성 질문 있을 때) │  │  ← 높이 3 (질문 없으면 숨김)
│  │    · ❓ agent-N: 질문 / [1] opt [2] opt ... / (대기 N개)  │  │
│  └─────────────────────────────────────────────────────────┘  │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ ③ 상태바  Window(FormattedTextControl ← StatusBar)       │  │  ← 높이 1 (상시)
│  │    · threads=N · msgs=M · gate=OPEN/CLOSED · phase=P1~P5  │  │
│  └─────────────────────────────────────────────────────────┘  │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │ ④ 입력줄  Window(TextArea, multiline=True, height=3)     │  │  ← 높이 3 (하단 고정)
│  │    · Enter 제출(accept_handler) · Shift/Esc+Enter 개행    │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────┬───────────────────────────────────┘
                            │ run_async()
              ┌─────────────▼─────────────┐
              │ cli._run_repl (asyncio)   │ ← ★ v2.5: 유일한 실행 경로 (1회 _run 제거)
              │  REPL 루프:                │
              │  while 사용자가 계속:       │
              │    steps = await session.run(prompt)
              │    (같은 Application, 세션 재사용)
              │  └─ app.run_async()        │
              │      └─ 이벤트 → render_event → LogBuffer │
              └─────────────┬─────────────┘
                            ▼
              ┌─────────────────────────┐
              │ MessageServer (SSOT)    │ ← 변경 없음
              │ human_send / ask_user   │
              └─────────────┬───────────┘
                            ▼
              ┌─────────────────────────┐
              │ AgentLoop step()        │ ← 이미 resolve된 args 전달 (안 A 적용됨, 검증만)
              └─────────────────────────┘
```

### 2.3 비-TTY / fallback

```
isatty(stdin) and isatty(stdout)?   (wizard.check_tty() + AttachConsole 재사용)
  ├─ YES → SessionTUIApplication (본 설계)
  └─ NO  → render_event(event) 결과를 그대로 print()  ← 기존 rich 출력과 동일 (fallback 1줄)
```

- **비-TTY에서도 REPL 루프는 동일하게 동작** — 입력은 `input()` 폴백, 출력은 `print(render_event(...))`.
  (v2.5: `--repl` 분기가 사라져도 비-TTY fallback 경로는 REPL 루프 안에서 일관 유지)
- **비-TTY 빈 입력:** `input()` 폴백에서도 **빈 줄 = 무시** (기존 quit 의미 제거).
- **종료 수단 (TUI·비TTY 통일, v0.5.1):** `/quit`·`/exit`·EOF(Ctrl+D/Z)·KeyboardInterrupt.
  평문 `quit`/`exit`는 **일반 메시지/다음 턴 프롬프트** (종료 아님 — router plain과 동일).

---

## 3. 컴포넌트 설계

### 3.1 신규 패키지 `src/agent_augury/tui/`

대규모 변경이므로 `channel/human_tui.py` 내부를 **`tui/` 패키지**로 분리한다
(agent-2 구조 제안 수용 — `channel/human_tui.py`는 얇은 어댑터로 남김).

```
src/agent_augury/tui/
  __init__.py
  renderer.py      # render_event(event) -> str | None — rich record=True → ANSI 1블록 (순수 함수)
  log_buffer.py    # LogBuffer — deque(maxlen) + dirty 캐시 + control() -> FormattedTextControl
  app.py           # SessionTUIApplication — Layout 조립 + run_async + shutdown/복원
  choice_panel.py  # ChoicePanel — PendingQuestion 큐(FIFO) + 패널 렌더러   [상세: INPUT_ROUTING_...§4]
  input_bar.py     # InputBar — TextArea(multiline=True, accept_handler) + 키바인딩 + FileHistory + 전송 피드백
  router.py        # 입력 라우팅 (슬래시/질문 응답/일반) — 순수 함수          [상세: INPUT_ROUTING_...§2]
  commands.py      # 슬래시 명령 레지스트리 5종+quit                        [상세: INPUT_ROUTING_...§3]
  status_bar.py    # StatusBar — control()/invalidate() (매 렌더 스냅샷 + 1초 타이머)
  key_aliases.py   # install_tui_key_aliases() — hermes pt_input_extras 이식 [상세: INPUT_ROUTING_...§5]
  __main__.py      # (테스트/데모) PipeInput 헤드리스 실행
src/agent_augury/channel/human_tui.py   # 얇은 어댑터로 축소 — tui/ 위임 (호환 shim 유지)
```

> **상세 설계 위임:** router/commands/choice_panel/key_aliases의 코드 골격·테스트는
> **하위 문서 `INPUT_ROUTING_SLASH_UX_DESIGN.md`(agent-4, v0.5)** 에 확정돼 있음.
> 본 문서는 통합 관점의 인터페이스/흐름만 정의한다.

### 3.2 LogBuffer (로그 버퍼 — dirty 캐시, agent-2 원고 반영)

```python
# tui/log_buffer.py
from collections import deque
from prompt_toolkit.formatted_text import ANSI, FormattedText
from prompt_toolkit.layout.controls import FormattedTextControl

class LogBuffer:
    """로그 라인 버퍼 — FormattedTextControl의 데이터 소스.

    - maxlen=1000 기본 (설정 가능)
    - dirty 캐시: append() 시 _cache=None → 다음 렌더에서만 ANSI 재구성 (성능)
    - tail view: Window(wrap_lines=True)가 높이 초과 시 자동으로 최신(하단) 표시
    - v1.1: 스크롤백 (Window scroll_offset/allow_scroll_beyond_bottom 확장)
    """

    def __init__(self, maxlen: int = 1000) -> None:
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._cache: FormattedText | None = None
        self._invalidate = lambda: None      # app.invalidate() 바인딩 (주입)

    def append(self, ansi_block: str) -> None:
        """ANSI 블록 추가 (여러 줄은 splitlines로 개별 라인 저장)."""
        self._lines.extend(ansi_block.split("\n"))
        self._cache = None                      # dirty
        self._invalidate()                      # app.invalidate() 호출

    def render(self) -> FormattedText:
        if self._cache is None:
            self._cache = ANSI("\n".join(self._lines))
        return self._cache

    def control(self) -> FormattedTextControl:
        return FormattedTextControl(text=lambda: self.render())

    def append_separator(self) -> None: ...
    def clear(self) -> None: ...
    def export_tail(self, n: int = 200) -> str: ...  # 종료 시 일반 화면 덤프 (ANSI strip)
```

- **성능:** `FormattedTextControl(text=lambda: render())`는 매 렌더 콜백 실행.
  dirty 캐시로 로그 1000줄 ANSI 재구성을 append 시 1회로 제한 (agent-2).
- **배치 flush:** 고빈도 이벤트는 50ms 모아 1회 `invalidate()` (선택).
- **app 참조 획득 (agent-2 v1.1 반영):** `LogBuffer(invalidate=콜백)` 주입 + `set_app(app)` 바인딩.
  app 생성 전 단위 테스트 가능, 생성 순서 의존성 제거.
- **REPL 루프 간 로그 유지 (v2.5):** 세션 재사용 루프 동안 LogBuffer는 **초기화하지 않고 유지** —
  이전 턴의 로그가 이어져 보임 (연속 대화 맥락). `/clear`로 명시 초기화만 가능.

### 3.3 render_event (기존 rich 로직 재사용 — 순수 함수, agent-1+agent-2 합의)

```python
# tui/renderer.py — 표시 전용. session/server/loop 변경 0.
import io
from rich.console import Console
from rich.markdown import Markdown

_style_console = Console(record=True, file=io.StringIO(), force_terminal=True)

def render_event(event: dict) -> str | None:
    """이벤트 → ANSI 문자열 1개. 표시할 게 없으면 None.

    기존 _log_step/_log_tool_event의 포맷 코드를 그대로 옮김 (동작 보존).
    ⚠️ 2회 print 구조(헤더+Markdown)를 1개 ANSI 블록으로 조립 (agent-1 지적 반영).
    """
    t = event["type"]
    if t == "step":
        if not event["result"].text:
            return None
        _style_console.print(f"💭 {event['agent_id']}:")
        _style_console.print(Markdown(event["result"].text))
    elif t == "send_message":
        if event["content"].startswith("[ask-user]"):
            return None                                   # D12 스킵
        targets = ", ".join(event["delivered_to"]) or "broadcast"
        _style_console.print(f"💬 [{event['author']} → {targets}][{event['thread_id']}]")
        _style_console.print(Markdown(_mask_sensitive(event["content"])))
    elif t == "tool":
        if event["tool"] in ("send_message", "create_thread", "read_resource"):
            return None                                   # D2-dedup
        if event["tool"] == "ask_user":
            return None                                   # pinned 패널이 대신 표시 (D11)
        # ... 기존 아이콘/path 축약 포맷 그대로
    elif t == "create_thread":
        ...
    elif t == "read_resource":
        ...
    return _style_console.export_text(styles=True).rstrip("\n")   # ANSI 1덩어리
```

- **마이그레이션:** `cli._log_step`/`_log_tool_event` 바디를 `tui/renderer.py::render_event`로 이동.
  기존 포맷(이모지/마크다운/마스킹) **100% 보존**.
- **비-TTY fallback 1줄:** `print(render_event(event))` — 기존 rich 출력과 동일 (agent-1 요구 충족).
- **보안:** `_mask_sensitive`는 renderer 경로에 유지 (시나리오 T6).

### 3.4 SessionTUIApplication (Application + lifecycle, agent-2 원고 반영)

```python
# tui/app.py — Layout 최종 (agent-2)
from prompt_toolkit.application import Application
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

class SessionTUIApplication:
    def __init__(self, session, renderer, router, *,
                 history_file, key_aliases=True) -> None:
        if key_aliases:
            install_tui_key_aliases()          # hermes pt_input_extras 이식 (1회)
        self.log_buffer = LogBuffer()
        self.log_buffer._invalidate = self._invalidate   # app.invalidate() 바인딩
        self.choice_panel = ChoicePanel()       # FIFO 큐 (agent-4 설계)
        self.status_bar = StatusBar(session)    # 매 렌더 스냅샷 + 1초 타이머
        self.input_area = self._build_input_area(history_file)   # accept_handler (아래 §3.5)
        self.app = Application(
            layout=self._build_layout(),
            full_screen=True,                   # ★ alternate screen (팀 합의)
            paste_mode=True,
            refresh_interval=0.1,
        )

    def _invalidate(self) -> None:
        self.app.invalidate()

    def _build_layout(self) -> Layout:
        # ★ 4분할 확정: [로그(1fr) / 선택지(h3, 조건부) / 상태바(h1) / 입력(h3)]
        return Layout(HSplit([
            Window(self.log_buffer.control(), wrap_lines=True),
            ConditionalContainer(
                Window(self.choice_panel.control(), height=3, style="bg:#333333"),
                filter=self.choice_panel.has_pending,   # 대기 질문 없으면 영역 소멸
            ),
            Window(self.status_bar.control(), height=1, style="bg:#222222"),
            Window(self.input_area, height=3),
        ]))

    async def run(self) -> None:
        status_task = asyncio.create_task(self.status_bar.run(self.app))
        try:
            await self.app.run_async()          # 세션 루프와 병렬 태스크
        finally:
            status_task.cancel()
            self.app.exit()                     # alternate screen 자동 복원

    def shutdown(self) -> None:
        """세션 종료 시 cli._run_repl finally에서 호출 — prompt_async hang 방지 (D13)."""
        self.app.exit()
        self.choice_panel.reset()
        self.input_area.buffer.reset()
```

- **full_screen=True (팀 합의):** alternate screen buffer라 rich print 경쟁이 구조적으로 불가능.
  종료 시 `app.exit()` 1회로 원래 화면 자동 복원.
- **Ctrl+C:** Application이 KeyboardInterrupt로 exit → finally에서 동일 정리. **세션이 도는 동안엔
  Ctrl+C가 입력줄에만 전달**되어 에이전트 루프를 죽이지 않음 (full-screen Application 키 스코프).
- **종료 로그 보존:** `--preserve-log-on-exit`(기본 true) → `LogBuffer.export_tail()`을 일반 화면에 덤프.
- **REPL 루프 (v2.5):** Application은 **루프 전체 동안 1개 인스턴스**로 유지.
  `session.run(prompt)`을 반복 호출할 때마다 앱을 재생성하지 않는다 (상태/레이아웃/로그 유지).
  루프 탈출(quit) 시에만 `shutdown()`.

### 3.5 InputBar (입력줄 — Enter=제출, agent-2 `accept_handler` 검증 반영)

> **⚠️ 함정 (agent-2 검증):** `TextArea(multiline=True)`에 커스텀 `key_bindings`만 넘기면
> Enter가 TextArea 내부의 기본 newline 처리와 **우선순위가 겹쳐** 예측 불가 동작 가능.
> → **`accept_handler`로 Enter 제출을 명시** (multiline과 무관하게 Enter=제출 보장).
>
> **v2.5 — 사용자 확정:** "나는 Enter 제출" — 아래 `accept_handler` 방식이 그대로 **최종 확정**이다.
>
> **구현 정정 (Enter submit fix):** `multiline=True`만으로는 Enter가 기본 newline에 가로채인다.
> 실제 코드는 `enter`/`c-j` eager → `validate_and_handle()`, `_accept`는 reset 없이 `return False`,
> 개행은 `(escape, enter)` + `(escape, c-j)`(win32 Ctrl+Enter). 위저드도 동일 키맵.
> 상세 SSOT: `TUI_ENTER_SUBMIT_FIX_DESIGN.md`.

```python
# tui/input_bar.py — 구현 확정 (agent-2 2안 중 안 ① 채택, 사용자 Enter 제출 확정)
def _make_input_area(app, history_file) -> TextArea:
    kb = KeyBindings()

    @kb.add("escape", "enter")                    # ② Esc+Enter = 개행 (기존 호환)
    def _newline(event): event.current_buffer.insert_text("\n")
    @kb.add("shift", "enter")                     # ③ Shift+Enter = 개행 (hermes 표준)
    def _newline2(event): event.current_buffer.insert_text("\n")
    @kb.add("c-d")                                # ④ Ctrl+D = 입력 루프 종료 (세션은 계속)
    def _quit(event): event.app.exit()
    @kb.add("c-c")                                # ⑤ Ctrl+C = 드래프트 클리어 (세션 계속, v1.0 포함)
    def _clear(event): event.current_buffer.reset()

    def _accept(buff: Buffer) -> bool:
        """Enter = 제출 — TextArea accept_handler (동기)."""
        text = buff.text
        buff.reset()                              # 입력줄 비움
        app.create_task(router.route(text))       # 비동기 전달 (fire-and-forget)
        return True                               # True = accept (Enter가 버퍼에 남지 않음)

    return TextArea(
        multiline=True,
        accept_handler=_accept,                   # Enter = 제출 (multiline과 무관)
        key_bindings=kb,                          # 개행/종료/클리어만 커스텀
        history=FileHistory(str(history_file)),
        prompt="👤 > ",
        height=3,
    )
```

- **왜 `accept_handler`인가 (agent-2):** `TextArea` 위젯은 내부적으로 자체 키바인딩을 구성하고
  `accept_handler`로 Enter를 처리한다. `multiline=True`면 기본 Enter=newline이므로, 커스텀 kb만으로
  재정의하면 충돌. `accept_handler`를 명시하면 Enter=제출이 구조적으로 보장됨.
  (대안: `Buffer` + `Window(BufferControl)` 직접 구성 — TextArea 스타일/프롬프트를 잃어서 비권장)
- **비동기 라우팅:** `accept_handler`는 동기 함수 → `app.create_task(...)` 또는 내부 큐로
  `router.route()`를 비동기 실행 (fire-and-forget). **`app` 참조는 `InputBar` 생성 시 주입**
  (agent-2 v1.1 반영) — 단위 테스트 시 `asyncio.create_task` 폴백.

- **제출 규칙 (agent-4 라우터, 팀 합의 + v2.5 빈 입력 통일 — 상세: `INPUT_ROUTING_SLASH_UX_DESIGN.md` §2):**
  | 입력 | 동작 |
  |------|------|
  | 빈 줄 | **무시 (ignored)** — 기존 `_run_repl`의 "빈 입력=quit" 폐기 (D-A20, agent-4 지적). 실수 종료 방지 |
  | `/`로 시작 | 슬래시 명령 디스패치 (§3.8) |
  | 활성 PendingQuestion + 숫자 `1`~`N` | 옵션 텍스트 치환 → `human_send(thread=질문 스레드, mentions=[질문 에이전트])` |
  | **활성 PendingQuestion + 그 외 텍스트** | **질문 스레드+질문 에이전트로 회신** (브로드캐스트 아님). 로그에 "(질문에 응답하는 대신 일반 메시지로 보냄)" 안내 1줄 |
  | 일반 텍스트 | `human_send(thread=최근 활성 스레드, mentions=[])` (+ REPL: 다음 턴 트리거, §4.4) |
- **전송 피드백 (R10, v1.0 필수):** `human_send` 성공 후 로그 버퍼에
  `👤 → {thread_id} (broadcast 또는 mentions=[...])` 1줄 append. → "입력이 안 먹힌다"는
  인식 해소 (agent-2 UX 1번).
- **히스토리:** `FileHistory(~/.agent-augury/human_history.txt)` — 기존 유지.
- **멀티라인:** `multiline=True`, 제출 Enter(accept_handler), 개행 Esc+Enter/Shift+Enter (Windows IME 안전).

### 3.6 ChoicePanel (ask_user 고정 패널 — agent-4 UX, FIFO 확정)

> 코드 import 메모: `from prompt_toolkit.formatted_text import FormattedText` (또는 HTML) 필요 —
> `INPUT_ROUTING_SLASH_UX_DESIGN.md` §4 코드 블록에 import 줄 포함. (agent-2 검증 메모)

- 상세: `INPUT_ROUTING_SLASH_UX_DESIGN.md` §4 (queue 구조/`push`/`pop`/`skip`/`control`).
- `on_ask_user(agent_id, tool, args, result)` 시그니처 그대로 (D8 경로 재사용).
- **다중 질문 큐: `deque[PendingQuestion]` — FIFO(도착 순서) 확정 (P3, D-A15).** 최신 1개 표시,
  응답/스킵 시 popleft, "대기 N개" 배지.
- 렌더: `❓ agent-N: 질문` / `[1] opt  [2] opt ...` / `(대기 N개)`.
- `ConditionalContainer(filter=choice_panel.has_pending)` — 질문 없으면 영역 자체가 사라져
  로그 공간 확보 (agent-1 동의).
- **활성 질문 회신 정책:** §3.5 표에 따라 질문이 있는 동안엔 모든 입력이 질문 스레드로
  라우팅 → 응답 오라우팅 방지 (agent-4 ①, 팀 합의). `/` 슬래시 명령은 항상 앱 레벨 처리.
- 탈출구: `/skip`(질문 dismiss), v2.0 `/broadcast <text>`.
- **"기타 입력" = 숫자가 아닌 원문 회신 규칙이 이미 자유 응답 역할** (agent-2 확인 — 추가 작업 0).
- **REPL 루프 간 초기화 (v2.5):** 새 `session.run(prompt)` 시작 시 ChoicePanel 큐는 유지하되,
  이전 턴의 미응답 질문은 `/skip`으로 정리 가능 (세션 재사용이므로 컨텍스트 연속성 유지).

### 3.7 StatusBar (상태바 — v1.0 포함 확정, agent-1/2 원고 반영)

```
threads=3 · msgs=41 · gate=OPEN(thread-3) · phase=P3_EXECUTE · steps=12 · agents=2 · 👤 입력 대기
```

```python
# tui/status_bar.py — 최종 (agent-2 원고)
class StatusBar:
    def __init__(self, session, *, refresh_interval=1.0):
        self._session = session
        self._refresh_interval = refresh_interval

    def _snapshot_line(self) -> str:
        snap = self._session.server.snapshot()
        gate = self._session.gate
        phase = self._session.protocol.phase if self._session.protocol else "n/a"
        return (
            f"threads={len(snap['threads'])} · msgs={len(snap['messages'])} · "
            f"gate={'OPEN' if gate and gate.is_open else ('CLOSED' if gate else 'n/a')} · "
            f"phase={phase} · agents={len(snap['agents'])}"
        )

    def control(self) -> FormattedTextControl:
        # 매 렌더마다 최신 스냅샷 — 이벤트 기반 갱신 + 1초 주기 보정이 1곳으로 수렴
        return FormattedTextControl(text=lambda: self._snapshot_line())

    async def run(self, app) -> None:
        """1초 주기 보정 태스크 — session.run과 병렬. 렌더 트리거만 담당."""
        while True:
            await asyncio.sleep(self._refresh_interval)
            app.invalidate()
```

- **갱신 방식 (agent-1 제안 → agent-2 수렴):** `FormattedTextControl(text=lambda: snapshot())`이
  **매 렌더마다 최신 상태를 읽으므로** 별도 상태 캐시 불필요. 이벤트(`log_buffer.append`/
  `on_ask_user`/`on_tool_event`)로 `app.invalidate()` → 즉시 반영, 1초 타이머로 보정 트리거.
- **성능:** snapshot()은 O(스레드+메시지) — 1초에 1번이면 부담 없음. 메시지 수천 건 넘어가면
  `len()`만 O(1)인 별도 카운터 노출로 최적화 (v2.0).
- **색상 테마:** v1.0은 **기존 rich 기본 테마 유지** (P9, D-A13 — agent-2 제안, 렌더러가 기존
  포맷 그대로라 변경 리스크 0). 커스텀 색상은 v2.0 config로.
- **REPL 턴 표시 (v2.5):** 상태바에 현재 REPL 턴(예: `turn=2/∞`) 또는 "세션 재사용 중" 표시를
  추가할 수 있음 (선택 — v2.0 확정 사항으로 분리).

### 3.8 슬래시 명령 (hermes slash 레지스트리 축소판 — v1.0 5종+quit, P4 확정)

상세: `INPUT_ROUTING_SLASH_UX_DESIGN.md` §3 (레지스트리 패턴/핸들러 순수 함수).

| 명령 | 동작 |
|------|------|
| `/quit` `/exit` | **REPL 루프 종료 → 세션 정리(shutdown) → 프로그램 종료** (v2.5 재정의 — 아래 참고) |
| `/help` | 키맵/명령 안내 — **LogBuffer.append로 출력** (출력 파이프라인 재사용) |
| `/status` | 게이트/페이즈/스레드/메시지 스냅샷을 로그에 출력 (`session.server.snapshot()`) |
| `/threads` | 현재 스레드 목록 + 최근 활성 스레드 표시 |
| `/clear` | 로그 버퍼 초기화 |
| `/skip` | 현재 PendingQuestion dismiss → 다음 대기 질문 노출 |
| 미등록 `/...` | "unknown command" + 명령 목록 제안 |

- 레지스트리 패턴(hermes `slash_exec.py` 축소판): `commands.py`에 `{name: (usage, handler)}` 맵.
- **v1.0은 읽기 전용 명령 + `/skip`만.** mutation 명령(예: gate open, `/broadcast`)은 v2.0
  (tools.py 스키마 확장과 함께).
- 슬래시 명령은 **에이전트에게 전달되지 않는다** (사용자 메시지 아님).
- **REPL 기본화 영향 (v2.5):** `/quit`은 **REPL 루프 전체 종료**로 의미가 단순해짐
  (1회 실행 모드가 없으므로 "입력 루프만 종료 + 세션은 계속"이라는 구분이 불필요).
  `/quit` → Application exit + `_run_repl` 루프 break + `session.close()`.
  (백그라운드 세션 유지/재개는 v2.0 범위 — `/quit`은 프로그램 종료가 기본 의미)

### 3.9 router (입력 라우팅 — 순수 함수, agent-4 설계 + agent-2 보강)

상세: `INPUT_ROUTING_SLASH_UX_DESIGN.md` §2 (`route()/RouteResult/_resolve_option` 코드 골격).

```python
# tui/router.py — TUI와 분리된 순수 함수 → 단위 테스트 용이
def route(text: str, ctx: RouterContext) -> RouteResult:
    """입력 → 라우팅 결정 (kind: command/choice/question_reply/plain/ignored/quit).

    - 빈 줄 → ignored (v2.5: REPL 기본화로 통일 — 종료 아님)
    - "/..." → command 또는 quit
    - 활성 질문 + 숫자 → choice (옵션 치환, thread/mentions=질문 에이전트)
    - 활성 질문 + 그 외 → question_reply (질문 thread/mentions, notice)
    - 일반 → plain (최근 스레드, mentions=None)
    """
    ...
```

- 순수 함수 → `test_tui_router.py`로 모든 분기 단위 테스트 (TTY 불필요).
- `route()`는 **결정만** 하고 실행(await human_send)은 호출부(TUI)에서.
- `_deliver`(기존)는 이 라우터의 결정을 실행하는 얇은 async 래퍼로 축소 (호환 shim 유지).
- **REPL 기본화 영향 (v2.5):** `plain` 라우팅이 "다음 세션 턴의 initial prompt"로도 이어짐 —
  REPL 루프에서 사용자 일반 입력 → `human_send` 주입 + 동시에 **다음 턴 진행 트리거**로
  해석 (턴 수명 주기 §4.4).

### 3.10 key_aliases (hermes `pt_input_extras.py` 이식 — agent-4 제안, 팀 합의)

상세: `INPUT_ROUTING_SLASH_UX_DESIGN.md` §5 (`install_tui_key_aliases()` 골격).

- `install_shift_enter_alias()` — Shift+Enter → (Escape, ControlM) = 개행
- `install_ctrl_enter_alias()` — Ctrl+Enter → 개행
- `install_modify_other_keys_aliases()` — Kitty CSI-u / xterm modifyOtherKeys에서
  Ctrl+letter/Alt+letter/Shift+letter 정상화 (한글 IME + 키 조합 안정성)
- `install_ignored_terminal_sequences()` — 포커스 이벤트(`ESC[I`/`ESC[O`) → Keys.Ignore

`install_tui_key_aliases()`를 `app.py`/`__init__`에서 import 시 1회 호출 (agent-2: `key_aliases=True` 기본).
각 함수는 `ANSI_SEQUENCES`에 `setdefault`로 매핑 추가 (기존 매핑 보존). 단위 테스트 포함.

---

## 4. 데이터 흐름

### 4.1 로그 (에이전트 → 화면) — agent-1 확정 경로

```
Session._output_queue
  → _output_consumer (session.py, 불변)
  → cli.on_step / cli.on_tool_event (콜백, 불변)
  → TuiRenderer.render_event(event) → ANSI 문자열 1개 (또는 None)
  → LogBuffer.append(ansi) → app.invalidate() (+ 상태바는 매 렌더 자동 갱신)
  → FormattedTextControl(text=lambda: ANSI("\n".join(buffer)))
```

- `session.py`/`server.py`/`loop.py`/`tools.py` **변경 0** — TUI는 순수 표시 계층.
- 비-TTY fallback: `print(render_event(event))` — 기존 출력과 동일.

### 4.2 ask_user (질문 → 고정 패널 → 응답)

```
agent ask_user(thread, question, options)
  → send_message 이벤트([ask-user] prefix) → render_event에서 None (D12)
  → tool(ask_user) 이벤트 → ChoicePanel.on_ask_user() → 패널 표시 (D11)
  → 사용자 "2" 입력 → TextArea.accept_handler → router.route → ("choice", 1, options[1])
       → human_send(thread_id=질문 스레드(resolve된 id), mentions=[질문 에이전트])
       → 로그에 "👤 → {thread}" 전송 피드백 (R10)
       → [radio] 흡수 (기존 경로 그대로)
```

### 4.3 실행 흐름 (v2.5 — REPL 단일 모드)

```
main(argv)
  ├─ (--config 있음) → _run_repl(cfg_path, ...)          ← --repl 분기 없음 (항상 REPL)
  └─ (--config 없음) → _run_wizard_flow(...)             ← repl 파라미터 없음 (항상 REPL)
       └─ 위저드 → _save_config → 초기 태스크 수집
            → _run_repl(cfg_path, initial_prompt=task)

_run_repl(cfg_path, initial_prompt=None, *, quiet=False, allow_fake=False)  # ★ 유일한 실행 함수
  cfg = load_config(...)
  session = Session.from_config(cfg, on_step=on_step, on_tool_event=on_tool_event)
  tui = SessionTUIApplication(session, renderer, router, history_file=...)
  try:
      await tui.run()                       # app.run_async (세션 루프와 병렬)
      steps = await session.run(initial_prompt=initial_prompt)   # 1턴
      print("--- session finished: ... ---")
      while True:                           # REPL 루프 (세션 재사용)
          question = await tui.prompt_next()   # 다음 질문 입력 (Enter 제출)
          if question is None or question in ("quit", "exit"):   # 빈 줄은 ignored — 여기 도달 안 함
              break
          steps = await session.run(initial_prompt=question)     # 같은 세션
          print("--- session finished: ... ---")
      return 0
  finally:
      tui.shutdown()                        # app.exit() → alternate screen 복원
      await session.close()
```

- **`_run`(1회 실행)은 제거** — `--repl`의 반대 옵션 코드가 더 이상 존재하지 않음.
- `--repl` argparse 인자, `_run_wizard_flow(repl=...)` 파라미터, `main()`의 `args.repl` 분기 전부 제거.
- REPL 루프는 TUI Application과 같은 asyncio 루프에서 동작 (D-A4 그대로).
- **빈 줄 처리 (v2.5):** router가 `ignored`를 반환하므로 `question`이 빈 문자열인 경우가
  구조적으로 없음. 종료는 `/quit`/`/exit`(router kind="quit") 또는 Ctrl+D(`app.exit()`)로만.

### 4.4 REPL 턴 수명 주기 (v2.5 신규 — 사용자 결정에 따른 설계 보강)

> `--repl`이 기본이 되면서 "언제 다음 턴이 시작되는가"가 명시적이어야 한다.

```
상태: IDLE(입력 대기) ⇄ RUNNING(session.run 실행 중)

[IDLE]
  ├─ 사용자 일반 입력(Enter) → router(plain) → human_send
  │     └─ 동시에 RUNNING으로 전환 → session.run(initial_prompt=입력)  ← "Enter로 제출=다음 턴 시작"
  ├─ 빈 줄 Enter → router(ignored) → 아무 일도 없음 (상태 유지)  ← v2.5 (D-A20)
  ├─ ask_user 응답(숫자/원문) → router(choice/question_reply) → human_send
  │     └─ RUNNING 상태면 에이전트가 흡수 (현재 턴 안에서 처리)
  ├─ 슬래시 명령 → 앱 레벨 처리 (턴 전환 없음)
  └─ /quit, /exit, Ctrl+D → 루프 종료

[RUNNING]
  ├─ 에이전트가 ask_user → 패널 표시 → 사용자 응답 → human_send (같은 턴)
  ├─ 에이전트 step 완료 → session.run 반환 → IDLE로 복귀 (상태바에 "👤 입력 대기")
  └─ 에이전트가 오래 도는 동안에도 입력줄은 항상 활성 (R2) — 입력은 큐/즉시 전송
```

- **정책:** IDLE에서 일반 텍스트 제출 시 = "다음 턴 프롬프트"로 `session.run` 시작.
  RUNNING 중 입력은 `human_send`로 주입되어 현재 턴에 반영 (패시브 어웨어니스 유지).
- 이 수명 주기는 `cli._run_repl`의 while 루프 + `SessionTUIApplication` 상태로 표현된다.
- 세부 결정은 agent-4/agent-2와 정합 필요 (설계 확정 항목으로 추가).

---

## 5. 변경 파일

| 파일 | 변경 | 비고 |
|------|------|------|
| `src/agent_augury/tui/*` (신규 10개) | 신규 패키지 | renderer/log_buffer/app/choice_panel/input_bar/router/commands/status_bar/key_aliases |
| `src/agent_augury/channel/human_tui.py` | **얇은 어댑터로 축소** — 내부는 `tui/` 위임 + 호환 shim(`_deliver`/`_resolve_option`/`_pending_question`) 유지 | agent-2 구조 제안 수용. 기존 API 결합 테스트는 shim으로 통과 + `tui/` 단위 재작성 |
| `src/agent_augury/cli.py` | **표시 계층 재작성 + 실행 모드 단일화**: `_run` 제거, `_run_repl`이 유일한 실행 함수로 승격. `--repl` argparse 인자 제거. `_run_wizard_flow(repl=...)` 파라미터 제거. REPL 루프 빈 입력=quit 로직 제거(ignored로 통일). `_log_step`/`_log_tool_event` → `render_event` 이관. 비-TTY fallback 1줄 | v2.5 핵심 |
| `src/agent_augury/agent/loop.py` | **변경 없음** — 이미 resolve된 args 전달 (검증 완료) | 안 A 적용됨 |
| `src/agent_augury/config.py` | `human.tui` 키 검증 (모드: `app`/`prompt_session`/`none`) | 옵트인 호환 |
| `pyproject.toml` | **변경 없음** (prompt-toolkit/rich 이미 포함) | — |
| `src/agent_augury/server.py` / `session.py` / `tools.py` / `system_prompt.py` | **변경 없음** | SSOT·L3 원칙 |
| `tests/test_human_tui.py` | **재작성/보강**: tui/ 단위(router/render_event/LogBuffer/ChoicePanel) + shim 호환 검증 | |
| `tests/test_repl.py` | **재작성**: REPL = 유일한 실행 모드 — 위저드/--config 모두 REPL 진입 검증, `_run` 제거 반영, 빈 입력=ignored 검증 | v2.5 |
| `tests/test_cli_entry.py` (또는 test_wiring.py 내) | **갱신**: `--repl` 플래그 존재 검증 제거, `--repl` 없이도 REPL 동작 검증 | v2.5 |
| `tests/test_tui_router.py` / `test_tui_commands.py` / `test_tui_key_aliases.py` | 신규 (agent-4 하위 문서 §7) | 순수 함수 |
| `tests/test_human_in_the_loop.py` / `test_reserved_names.py` / `test_server.py` | **무변경 통과 예상** | 서버/세션 불변 |
| `tests/test_cli_output_path.py` / `test_wizard.py` | 무영향 | 순수 함수 |
| `tests/test_wiring.py` | `test_cli_runs_fake_session_and_prints_log`는 **어댑터 미생성(비-TTY) → rich print fallback** 전제 명문화 + REPL 단일 모드 반영 | agent-1 지적 |

---

## 6. 키 바인딩 상세 (팀 합의 + hermes 공식 근거 + agent-2 구현 확정 + 사용자 Enter 확정)

| 키 | 동작 | 구현 |
|----|------|------|
| `Enter` / `C-j` | **제출** (ask_user 활성 시 번호 선택도 여기서) — hermes 표준, IME 안전, **사용자 확정** | 컨트롤 kb `eager` → `validate_and_handle()` → `accept_handler` (return False; history 순서 보존). 상세: `TUI_ENTER_SUBMIT_FIX_DESIGN.md` |
| `Shift+Enter` / `Esc+Enter` | 개행 | kb `(escape, enter)` → `insert_text("\n")` (`key_aliases`가 Shift+Enter 정규화; `shift` 단독 키 바인딩 불가) |
| `Ctrl+Enter` (Windows) | 개행 | kb `(escape, c-j)` — win32는 Ctrl+Enter를 `[Escape, ControlJ]`로 전달 |
| `Ctrl+D` | 입력 루프 종료 (세션은 계속) — v2.5: REPL 단일 모드이므로 **프로그램 종료**로 이어짐 | kb → `on_quit()` + `app.exit()` |
| `Ctrl+C` | 드래프트 클리어 (세션 계속) — v1.0 포함 | kb → `buff.reset()` |
| `↑` / `↓` | 히스토리 탐색 | `FileHistory` 기본 (TextArea에 history 연결) |
| `Ctrl+R` | 히스토리 역방향 검색 | prompt_toolkit 기본 |
| `Ctrl+L` | 화면/로그 갱신 (redraw) | kb → `app.invalidate()` |
| `/...` | 슬래시 명령 (5종+quit) | router → commands.dispatch |
| `Tab` | (v2.0) 슬래시 명령 자동완성 | Completer |
| `PgUp` / `PgDn` | 로그 스크롤백 (v1.1) | Window scroll_offset |

---

## 7. Windows 대응 (사용자 환경: `C:\Users\test`)

- prompt_toolkit은 **win32 네이티브(ConHost 포함)** 지원. full-screen alternate screen도
  Windows Terminal(ConPTY)에서 최상의 품질 → **README에 Windows Terminal 권장 안내**.
- `check_tty()`/`_try_attach_parent_console()`(wizard.py)을 **TUI 시작 전에 호출** —
  console_scripts 래퍼에서 TTY 탐지가 안 되는 문제 해결 (기존 로직 재사용).
- 한글 IME: `TextArea(multiline=True)` + **Enter=제출(accept_handler)** — IME 조합 확정과 제출의
  충돌 방지 (win32 IME가 조합 중 Enter를 가로채므로). **Windows Terminal에서 IME 실측 테스트를
  테스트 항목으로 포함** (agent-2).
- 위저드 종료 → 세션 시작 전환 지점에서 터미널 상태가 깨지지 않도록 `check_tty()` + 화면
  클리어 경계 처리 (agent-2 지적 — 현재 깨짐의 한 요인).
- **REPL 기본화 (v2.5):** 위저드 종료 후 항상 REPL 루프로 진입하므로, 위저드→TUI 전환 경계가
  **단 한 번**만 발생 — 화면 전환이 잦아 깨지는 문제 자체가 줄어든다.

---

## 8. 테스트 전략

### 8.1 순수 함수 단위 (TTY 불필요)

- `router.route` — 슬래시/선택지/질문 회신/일반/빈 줄 전 분기 (`test_tui_router.py`)
- `render_event` — ANSI 출력에 마스킹/아이콘/[ask-user] 스킵/D2-dedup 반영
- `LogBuffer.append/render/export_tail` — maxlen 슬라이딩, dirty 캐시, 종료 덤프
- `ChoicePanel` — 질문 큐(FIFO)/번호 치환/상태 전이
- `commands.dispatch` — 슬래시 파싱/디스패치/예외 안전 (`test_tui_commands.py`)
- `key_aliases` — ANSI_SEQUENCES 주입 수/중복 안전 (`test_tui_key_aliases.py`)
- **`accept_handler` 단위** — `_accept` 호출 → 버퍼 리셋 + router.route 예약 (PipeInput으로 Enter 전송)

### 8.2 헤드리스 E2E (`PipeInput`)

```
시나리오 T1: 세션 도중 "방향 바꿔줘" 주입 → human_send → [radio] 흡수 + "👤 → thread" 피드백 로그
시나리오 T2: ask_user(thread="$thread:0") → 패널 표시 → "2" → options[1] 치환 전송 (KeyError 없음)
시나리오 T3: 활성 질문 중 일반 텍스트 → 질문 스레드+질문 에이전트로 회신 (오라우팅 방지) + 안내 로그
시나리오 T4: 비-TTY(파이프) → render_event 결과 print (기존 rich 출력 동일) — 회귀 0
시나리오 T5: Ctrl+D → TUI 종료, 세션은 계속 → 종료 로그 덤프 + 정리 완료
시나리오 T6: 마스킹 — 토큰/API키가 로그/패널에 노출되지 않음
시나리오 T7: (수동) Windows Terminal에서 한글 IME 조합 + Enter 제출 실측
시나리오 T8: 상태바 — 1초 폴링으로 threads/gate/phase 갱신
시나리오 T9: Ctrl+C → 드래프트만 클리어, 세션/입력루프 계속 (v1.0)
시나리오 T10 (v2.5): REPL 기본값 — `--repl` 없이 위저드/--config로 시작해도 세션 재사용 루프 동작
시나리오 T11 (v2.5): 1회 실행 경로 제거 — `_run`/`--repl` 코드·인자 존재하지 않음 (import/grep 검증)
시나리오 T12 (v2.5): IDLE 일반 입력 → 다음 턴 session.run(initial_prompt=입력) 시작 (턴 수명 주기)
시나리오 T13 (v2.5): REPL 2턴 연속 — 같은 세션 인스턴스 재사용, 로그 버퍼 유지, 상태바 갱신
시나리오 T14 (v2.5, agent-4): 빈 줄 Enter → ignored — 세션 종료되지 않고 계속 (기존 _run_repl의 quit 폐기 검증)
시나리오 T15 (v2.5, agent-4): /quit → REPL 루프 종료 → session.close() → 프로그램 종료 (정리 순서 검증)
```

### 8.3 기존 테스트 영향 (agent-1 보고 반영)

- `test_human_in_the_loop.py` / `test_reserved_names.py` / `test_server.py` — 서버/세션 불변 → **그대로 통과**
- `test_wiring.py` / `test_parallel.py` — `on_tool_call`이 이미 resolve된 args 전달 → **이미 반영, 예상값 확인만**
- `test_wiring.py::test_cli_runs_fake_session_and_prints_log` — **"어댑터 미생성(비-TTY) → rich print fallback" 전제 명문화** (TUI 어댑터 None mock 유지 가능)
- `test_human_tui.py` / `test_repl.py` — **재작성 필요** (`tui/` 단위 + shim 호환 + REPL 단일 모드)
- `test_cli_output_path.py` / `test_wizard.py` — 무영향
- **`--repl` 인자/`_run` 관련 테스트 — 제거/갱신 (v2.5)**
- **기존 REPL "빈 입력=quit" 테스트 — "빈 입력=ignored"로 기대값 변경 (v2.5)**

---

## 9. 구현 로드맵

| 단계 | 범위 | 통과 기준 |
|------|------|-----------|
| **P1 골격** | `tui/` 패키지 + LogBuffer + render_event + Application(full-screen) 최소 동작. Enter 제출(accept_handler) → human_send + 전송 피드백, 로그 append → 화면 갱신 | T1 |
| **P2 이벤트 배선** | `cli._run`을 App 기반으로 전환. `_log_step`/`_log_tool_event` → render_event. 비-TTY fallback 확인 | T4 |
| **P3 ask_user + 상태바** | ChoicePanel(FIFO) + router(질문 회신 정책) + StatusBar(1초 폴링) | T2, T3, T8 |
| **P4 REPL 단일 모드 (v2.5 확대)** | `_run_repl` 승격 + `_run` 제거 + `--repl` 인자 제거 + `_run_wizard_flow(repl=...)` 제거 + 빈 입력=ignored 통일 + REPL 턴 수명 주기 + 종료 로그 덤프 + 정리 | T5, T9, T10~T15 |
| **P5 마이그레이션/테스트** | `channel/human_tui.py` 얇은 어댑터화(+shim), config 키, key_aliases 이식, 테스트 재작성/갱신 | 전체 회귀 0 |

---

## 10. 열린 결정 (Open Decisions — 팀 합의 + 사용자 확정 반영)

| # | 항목 | 합의/제안 | 상태 |
|---|------|-----------|------|
| D-A1 | full-screen 전환 | **✅ True 확정** (agent-2/4 합의) — alternate screen으로 rich 경쟁 구조 소멸. 종료 시 로그 덤프로 보완 | ✅ |
| D-A2 | 활성 질문 회신 라우팅 | **✅ 질문 스레드+질문 에이전트 우선** + 일반 메시지 안내 로그 (agent-2 보강) | ✅ |
| D-A3 | Enter 바인딩 | **✅ Enter=제출(accept_handler), Esc+Enter/Shift+Enter=개행** — hermes 공식 근거 + agent-2 함정 검증 + **사용자 확정** | ✅ 확정 |
| D-A4 | 실행 모드 (REPL 기본) | **✅ REPL 단일 모드 확정** — "같은 Application + 세션 재사용 루프". `--repl` 플래그/1회 실행(`_run`) 제거 (**사용자 확정**) | ✅ 확정 |
| D-A5 | 위저드 통합 | **⏳ v2.4는 input() 유지** + 단일 라인 프롬프트만 prompt_toolkit 통일. 전환 경계 check_tty/클리어 처리. 전체 TUI 통합은 v2.0 후속 | ⏳ |
| D-A6 | 로그 렌더링 | **✅ render_event 순수 함수** (rich record=True → ANSI 1블록) — 2회 print 리팩토링 포함 | ✅ |
| D-A7 | 로그 스크롤백(PgUp/PgDn) | v1.1 (tail view 자동 — Window wrap_lines 기본) | ⏳ |
| D-A8 | key_aliases 이식 | **✅ `tui/key_aliases.py`** — `install_tui_key_aliases()` 1회 호출 (기본 on) | ✅ |
| D-A9 | `channel/human_tui.py` | **✅ 얇은 어댑터 유지** (tui/ 위임 + 호환 shim) — 삭제 대신 이관 | ✅ |
| D-A10 | 상태바 | **✅ v1.0 포함, Layout 4분할 확정** — 매 렌더 스냅샷 + 1초 타이머 트리거 (agent-2 수렴) | ✅ |
| D-A11 | 종료 로그 보존 | `--preserve-log-on-exit` 기본 true (full-screen 복원 후 덤프) | ⏳ |
| D-A12 | busy 중 입력 큐(hermes queue) | v1.1 옵션 (현행은 즉시 전송 + 전송 피드백 — 패시브 원칙 유지) | ⏳ |
| D-A13 | 로그 색상 테마 | **✅ 기존 rich 기본 테마 유지** (P9 — 변경 리스크 0). 커스텀은 v2.0 config | ✅ 확정 |
| D-A14 | **사용자 UX 확정** | ① **Enter=제출 확정** (1A) ② **--repl 기본값=REPL 단일 모드 확정** (2B) — **사용자 확인 완료** | ✅ 확정 |
| D-A15 | 다중 질문 순서 (P3) | **✅ FIFO(도착 순서) 확정** (agent-4) — HITL에선 밀리면 안 됨 | ✅ 확정 |
| D-A16 | 슬래시 명령 v1.0 (P4) | **✅ 5종+quit 확정** (/help /status /threads /clear /skip) | ✅ 확정 |
| D-A17 | Ctrl+C 동작 | **✅ 드래프트 클리어 (v1.0 포함)** — hermes 3단계 중 2단계. 세션/입력루프 계속 | ✅ |
| D-A18 | 구현 범위 | **✅ "설계만" 확정** (사용자 확인) — 설계 산출물만 작성, 코드 변경 없음 | ✅ 확정 |
| D-A19 | **REPL 턴 수명 주기 (v2.5 신규)** | IDLE 일반 입력 = 다음 턴 시작 / RUNNING 중 입력 = 현재 턴 주입. 세부는 agent-2/4와 정합 | ⏳ 정합 필요 |
| D-A20 | **빈 입력 처리 통일 (v2.5, agent-4 지적)** | **✅ 빈 줄 Enter = ignored (무시)** — 기존 `_run_repl`의 "빈 입력=quit" 폐기. 종료는 `/quit`/`/exit`/Ctrl+D로만 | ✅ 확정 |

---

## 11. 리스크와 완화

| 리스크 | 완화 |
|--------|------|
| `Application.run_async()` 루프와 `session.run()` 태스크 간 간섭 | 같은 asyncio 루프에서 create_task 병렬. `app.invalidate()`는 이벤트 루프 내 호출 보장 |
| 로그 이벤트 폭주로 렌더링 지연 | LogBuffer dirty 캐시 + 50ms 배치 flush + refresh_interval |
| rich ANSI와 prompt_toolkit `ANSI` 파서 불일치 | `FormattedTextControl(text=ANSI(...))` 네이티브 파싱. 색 불일치는 renderer 콘솔 옵션 조정 |
| full-screen 종료 시 로그 소실 | `--preserve-log-on-exit` 덤프 (D-A11) |
| Windows console_scripts TTY 탐지 실패 | `check_tty()` 재사용 (AttachConsole) |
| 마스킹 회귀 | `_mask_sensitive`를 renderer 경로에 유지 + 시나리오 T6 |
| `on_tool_call` args 변경 회귀 | 이미 resolve 후 전달 — 테스트 예상값 확인만 (agent-1) |
| 세션 종료 지연 | `app.shutdown()` 명시 호출 (D13) — prompt_async hang 방지 |
| IME 조합 중 Enter 오동작 | Enter=제출(accept_handler) + Esc/Shift+Enter 개행 + key_aliases + Windows Terminal 실측(T7) |
| `TextArea` Enter 제출 충돌 | **accept_handler 사용 확정** (agent-2 함정 검증) — 커스텀 kb만으로 재정의 금지 |
| `test_wiring`의 어댑터 None mock | "어댑터 미생성 → rich print fallback" 전제 명문화 (agent-1) |
| "전송 안 됨" 인식 (기존 화면 깨짐 증상) | 전송 피드백 로그(R10) + 상태바 — 사용자가 입력 결과를 항상 확인 |
| **REPL 기본화 회귀 (v2.5)** | `--repl` 인자/`_run` 참조를 테스트에서 grep 검증(T11). 기존 `--repl` 없이 1회 실행하던 사용자 흐름은 REPL이 기본이므로 **동작 확장** (1회 실행이 필요하면 `/quit`로 즉시 종료) |
| **REPL 턴 수명 주기 혼란 (v2.5)** | 상태바에 IDLE/RUNNING 표시 + 전송 피드백 로그. D-A19 정합 후 확정 |
| **빈 입력 의미 변경 혼란 (v2.5)** | `/help`에 "빈 Enter=무시, 종료는 /quit 또는 Ctrl+D" 안내 명시. 기존 quit-by-blank에 익숙한 사용자 대상 안내 (T14 검증) |

---

## 12. 참고 (hermes-agent 패턴 요약)

- **한 렌더러 독점:** hermes는 Python gateway가 소유권을 갖고, UI가 단일 화면으로 렌더링.
  출력 경쟁 자체가 없음. → agent-augury는 prompt_toolkit 단일 Application으로 축소 적용.
- **상태바/입력/로그가 하나의 앱 트리:** `ui-tui/src/components/appChrome.tsx`, `appLayout.tsx`.
- **입력 UX (공식 키맵):** `Enter=Submit` / `Shift+Enter/Alt+Enter=newline` / `Ctrl+C=인터럽트`
  / `Ctrl+D=Exit` / 슬래시 명령 / busy 중 큐 / 히스토리(`~/.hermes/.hermes_history`).
- **ask_user 선택지 UX:** hermes clarify prompt와 동형 — `Up/Down+Enter`, 숫자 1-9 퀵픽,
  "Other"=자유 입력 (우리 "숫자 아니면 원문 회신" 규칙이 동일 역할).
- **키보드 시퀀스 보강:** `hermes_cli/pt_input_extras.py` — ANSI_SEQUENCES alias로
  Kitty/xterm modifyOtherKeys 대응 → `tui/key_aliases.py`로 이식.
- **Python CLI 쪽:** `hermes_cli/curses_ui.py`(전체 화면 메뉴 — ask_user 선택지 UX 참고) /
  `console_engine.py`(라인 명령 엔진 — 슬래시 명령 레지스트리 참고).
- **REPL 단일 모드 (v2.5):** hermes는 대화 세션이 기본적으로 연속(채팅) — agent-augury도
  "위저드 → REPL 루프"로 기본 UX를 통일. `--repl` 반대 개념(1회 실행)은 hermes에도 없음.
