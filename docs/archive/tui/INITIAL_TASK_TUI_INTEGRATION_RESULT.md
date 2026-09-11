# agent-augury — "Initial Task가 TUI가 아니다" 원인·해결 설계 + 사용자 결정 확정

> **Status:** 결과 문서 **v1.0.1 (구현 확정)** · **Date:** 2026-09
> **작성:** agent-2 (원인 분석/검증/결과 통합) · 협업: agent-1 (설계 초안/통합/테스트), agent-3 (소스 검증), agent-4 (원인 확정/선택지/로그 스크롤 제안)
> **Rev:**
> - **v1.0.1 (agent-3 디테일 반영)** — `_initial_task_mode`는 **1회성 플래그**: 첫 task 입력 접수 시 `False`로 해제해야
>   첫 턴 **RUNNING 중 개입 입력**이 `human_send`로 현재 턴에 주입된다 (설계 v2.5 §4.4 규칙 위반 방지). §2.1 코드에
>   `self._initial_task_mode = False` 1줄 반영 + §3/§4 갱신.
> - **v1.0 (사용자 결정 확정)** — ① 로그 스크롤 키 **1-A** (↑/↓=입력 history 유지 + PgUp/PgDn·마우스 휠·Alt+↑/↓=로그 스크롤) ② Initial Task UX **2-A** (위저드 직후 TUI 바로 시작 → 입력줄로 task 입력) ③ 선택지 패널 **3-A** (옵션 세로 목록 + 동적 높이(최대 8줄) + wrap). D-1~D-5 전부 확정.
> - v0.13 — **agent-3 함정 반영**: 전역 kb 바인딩에서 `event.app`은 prompt_toolkit `Application`이지 `SessionTUIApplication`이 아님 → **`self` 클로저 방식으로 구성** (`event.app.log_window` 금지).
> - v0.12 — **agent-3 단일 소스 방식 반영**: ① S2 커서 동기화는 `get_cursor_position`이 `log_window.vertical_scroll`을 **직접 읽는 단일 소스**로 확정 ② follow 플래그는 앱 레벨 관리, LogBuffer는 순수 버퍼 유지 ③ `initial_task_mode` 파라미터 + 전역 kb 동의 반영.
> - v0.11 — agent-3 S2 정밀화: wrap_lines에서 `vertical_scroll ± N` 직접 조작은 max_scroll(=cursor.y) 클램프로 **무효** → 콜백 기반 get_cursor_position + follow 플래그 채택. 회귀 분석: TTY일 때만 TUI 분기 → pytest 영향 0.
> **관련 문서:** `SESSION_TUI_REDESIGN.md` v2.5 (D-A5 — 본 문서로 확정), `TUI_ENTER_SUBMIT_FIX_DESIGN.md` (Enter=제출 — 이미 구현), `INITIAL_TASK_TUI_INTEGRATION_DESIGN.md` v0.9 (agent-1 설계 초안)
> **사용자 요청:** ① "InitialTask가 여전히 TUI가 아니다" 원인/해결/설계 ② "선택지가 3줄밖에 안 보여서 선택을 못 해" ③ "선택지는 결과 문서(MD)에 함께 작성" ④ "TUI 로그가 스크롤이 안 된다 (↑/↓가 입력 history로 동작)"
> **사용자 결정 (v1.0 확정):** **1-A / 2-A / 3-A** — 모두 기본 권장안 채택.

---

## ★★★ 사용자 결정 확정 (1-A / 2-A / 3-A) ★★★

> ✅ **결정 완료 (2026-09):**
> - **결정 ① 로그 스크롤 키 정책: 1-A** — ↑/↓ = 입력 history 유지 + **PgUp/PgDn·마우스 휠·Alt+↑/↓** = 로그 스크롤
> - **결정 ② Initial Task UX: 2-A** — 위저드 직후 **TUI 바로 시작** → 로그 안내 + 입력줄로 task 입력 (Enter 제출)
> - **결정 ③ 선택지 패널 형태: 3-A** — 옵션 **세로 목록** + 패널 **동적 높이**(최대 8줄) + wrap
> - D-4 패널 최대 높이 **8줄** · D-5 로그 스크롤 단위 **10줄** — 기본안 유지
>
> 이 문서를 **구현 확정본 v1.0.1** 으로 승격합니다. 아래 §2 해결 설계 3건을 코드로 구현합니다.

---

## 0. 한 줄 결론

> **사용자 보고가 정확합니다.** 현재 `Initial Task` 입력은 full-screen TUI(로그/선택지/상태바/입력줄 4분할)가 아니라
> **별도 인라인 `PromptSession`**(`cli.py::_prompt_multiline`)으로 받고 있습니다 — 설계 문서 v2.5의 D-A5가
> "위저드 TUI 통합은 v2.0 후속"으로 미뤄둔 항목이 그대로 남아 있는 것입니다.
>
> **해결 (사용자 결정 1-A/2-A/3-A 반영):**
> 1. **Initial Task를 full-screen TUI 첫 입력으로 통합** (2-A) — 위저드 직후 TUI 먼저 시작 → 입력줄에서 task 입력(Enter 제출) → `session.run(task)`. 기존 router/입력 라우팅 재사용, 비-TTY는 기존 fallback 유지.
> 2. **ask_user 선택지 패널 3줄 잘림 해결** (3-A) — 옵션 **세로 목록** + 패널 **동적 높이**(최대 8줄) + **wrap_lines=True**.
> 3. **로그 스크롤백** (1-A) — `mouse_support=True` + **단일 소스 S2**(get_cursor_position이 `log_window.vertical_scroll` 직접 읽기) + **PgUp/PgDn·Alt+↑/↓** 키 바인딩. ↑/↓는 입력 history 유지.

---

## 1. 원인 분석 (코드 근거)

### 1.1 "Initial Task가 TUI가 아니다" — 왜 그런가

| # | 항목 | 근거 코드 | 판정 |
|---|------|-----------|------|
| P1 | Initial Task 입력이 **인라인 PromptSession** | `cli.py::_prompt_multiline` — `PromptSession(multiline=True, key_bindings=kb)` + `session.prompt()` | full-screen `Application`(SessionTUIApplication)이 **아님** |
| P2 | 위저드 → Initial Task → TUI 순서 | `cli.py::_run_wizard_flow` — `_prompt_multiline` 호출 후 `_run_repl(cfg, initial_prompt=task)` | Initial Task가 TUI **밖**에서 처리됨 |
| P3 | 설계상 미완 항목 | `SESSION_TUI_REDESIGN.md` v2.5 **D-A5**: "위저드 통합 ⏳ — input() 유지, 전체 TUI 통합은 v2.0 후속" | **설계가 구현보다 앞서 있었음** (사용자 관찰 정확) |
| P4 | 키맵 자체는 정상 | `_prompt_multiline`에 `enter`/`c-j` eager → `validate_and_handle()`, `escape+enter`/`escape+c-j` 개행 구현됨 | Enter=제출 설계는 **이미 반영** — 문제는 "위치/형태" |

> **정리:** Enter=제출 키맵은 위저드에서도 이미 동작합니다. 사용자가 "TUI가 아니다"라고 느끼는 실체는
> **Initial Task 단계가 full-screen TUI(4분할 레이아웃)가 아니라 일반 터미널 인라인 프롬프트**라는 것 — 입력 줄이
> 화면 하단 고정이 아니고, 로그/상태바/선택지 패널이 없는 화면입니다.

### 1.2 "선택지가 3줄밖에 안 보여서 선택을 못 해" — 왜 그런가

| # | 항목 | 근거 코드 |
|---|------|-----------|
| P5 | 선택지 패널 **height=3 고정** | `tui/app.py::_build_layout` — `Window(self.choice_panel.control(), height=3, style="class:choice")` |
| P6 | 옵션 **가로 한 줄 join** | `tui/choice_panel.py::render` — `"   ".join(f"[{i+1}] {opt}" ...)` |
| P7 | Window 기본 **wrap_lines=False** → 폭 초과 시 잘림 | prompt_toolkit `Window` 기본 동작 |

> 질문 1줄 + 옵션 1줄(가로) + (queued N) 1줄 = **최대 3줄 구조** — 옵션이 길거나 많으면 옵션 줄이 화면 폭을 넘어 잘리고,
> 패널 높이 3으로 추가 표시가 불가능합니다. 이것이 "선택하려고 하면 3줄밖에 안 보인다"의 정확한 원인입니다.

### 1.3 "TUI 로그가 스크롤이 안 된다" — 왜 그런가

| # | 항목 | 근거 코드 |
|---|------|-----------|
| P8 | 로그 Window에 **scroll/focusable 설정 없음** | `tui/app.py::_build_layout` — `Window(self.log_buffer.control(), wrap_lines=True)` |
| P9 | `Application(mouse_support=True)` **미설정** → 마우스 휠도 안 됨 | `tui/app.py::__init__` — app_kwargs에 mouse_support 없음 |
| P10 | ↑/↓ = 입력 TextArea의 **FileHistory 탐색** (기본) | `tui/input_bar.py` — `history=FileHistory(...)` 연결 |

> ⚠️ **핵심 함정 (prompt_toolkit 소스 검증 — agent-2/3 수렴):** `wrap_lines=True`인 로그 Window는 커서 위치 기준으로
> `vertical_scroll`을 **매 렌더마다 재계산**합니다. `FormattedTextControl`은 커서가 기본 `(0,0)`이라
> `vertical_scroll=0`(맨 위 고정)으로 강제 → **스크롤 불가**가 구조적으로 발생. 단순히 `allow_scroll_beyond_bottom=True`를
> 붙이거나 `vertical_scroll`을 직접 조작해도 **max_scroll(=cursor.y)에 클램프**되어 되돌아갑니다 (해법은 §2.3 **S2**).

---

## 2. 해결 설계 (v1.0.1 — 사용자 결정 확정 반영)

### 2.1 Initial Task → full-screen TUI 첫 입력 통합 (결정 ② 2-A)

```
Before: 위저드(input) → "--- Initial Task ---" 인라인 PromptSession → full-screen TUI → session.run(task)
After:  위저드(input) → full-screen TUI 시작 → 로그에 "--- Initial Task ---" 안내
        → TUI 입력줄에서 task 입력(Enter 제출) → session.run(initial_prompt=task)
        → 이후 동일 TUI에서 대화 계속 (기존 REPL 루프 그대로)
```

- **TUI는 1개 인스턴스로 위저드 직후 바로 시작** — 화면 전환이 "위저드 → TUI" 단 1회만 발생.
- Initial Task 입력은 **기존 router/input_bar 라우팅을 그대로 재사용** (별도 프롬프트 경로 제거).
- **Initial Task 대기 모드**: `_run_repl_tui`에 `initial_prompt=None` 분기 추가. 첫 입력(plain) 시
  `_send`(**스레드가 아직 없어 실패**)를 생략하고 `on_next_turn(task)`만 호출 → `session.run(initial_prompt=task)`.
  이후 턴부터는 기존 plain 경로(`_send` + `on_next_turn`) 그대로.
- **⚠️ v1.0.1 — `_initial_task_mode`는 1회성 플래그**: 첫 task가 접수된 순간 `False`로 해제해야,
  첫 턴 **RUNNING 중 개입 입력**("방향 바꿔줘")이 `human_send`로 **현재 턴에 주입**된다 (설계 v2.5 §4.4
  "RUNNING 중 입력 = 현재 턴 주입" 규칙). 해제하지 않으면 개입 입력이 `_send` 없이 다음 턴 프롬프트로만 전달되는 버그 발생.
- **비-TTY fallback (회귀 분석 — agent-3):** `_run_wizard_flow`의 변경은 **"TTY(=TUI 사용)일 때만"**
  `_prompt_multiline`을 건너뛰고 `_run_repl(initial_prompt=None)`을 호출. pytest 환경은
  `_want_fullscreen_tui()`가 `PYTEST_CURRENT_TEST`로 먼저 False → plain REPL 경로 → **기존 테스트 전부 통과**.

```python
# cli.py::_run_wizard_flow — TTY 분기 (핵심)
if _want_fullscreen_tui():
    return asyncio.run(_run_repl(str(output_path), initial_prompt=None, quiet=quiet))  # TUI 첫 입력 = Initial Task
# 비-TTY: 기존 인라인 _prompt_multiline 유지 (회귀 0)
task = _prompt_multiline("What would you like to do? [Multi-agent collaboration] ")
return asyncio.run(_run_repl(str(output_path), initial_prompt=task, quiet=quiet))
```

```python
# cli.py::_run_repl_tui — Initial Task 대기 모드
waiting_initial = initial_prompt is None
...
tui = SessionTUIApplication(
    session,
    on_quit=on_quit,
    on_next_turn=on_next_turn,
    initial_task_mode=waiting_initial,     # ★ 신규 파라미터 (agent-4)
    preserve_log_on_exit=True,
)
if waiting_initial:
    tui.append_text("--- Initial Task ---")
    tui.append_text("What would you like to do? [Multi-agent collaboration]")
    tui.append_text("(Enter to submit, Shift+Enter for newline)")

async def session_loop():
    tui.set_running(True)
    try:
        if waiting_initial:
            task = await next_turn.get()          # TUI 입력줄에서 첫 입력 대기
            if task is None:
                return 0
            steps = await session.run(initial_prompt=task)
        else:
            steps = await session.run(initial_prompt=initial_prompt)
    finally:
        tui.set_running(False)
    ...  # 이후 기존 REPL 루프 동일
```

```python
# tui/app.py — 생성자 + handle_input plain 분기 (v1.0.1: 1회성 해제)
class SessionTUIApplication:
    def __init__(self, session, *, ..., initial_task_mode: bool = False, ...):
        self._initial_task_mode = initial_task_mode
        ...

    async def handle_input(self, text: str) -> None:
        ...
        if result.kind == "plain":
            if self._initial_task_mode:
                self._initial_task_mode = False      # ★ v1.0.1: 1회성 — 첫 task에서만 동작, 즉시 해제
                if self._on_next_turn is not None:
                    self._on_next_turn(result.content)   # session.run 트리거만 (스레드 없음 → _send 생략)
                return
            await self._send(result.thread_id, result.content, mentions=None)
            ...
```

> **핵심 이유:** plain 경로가 `_send(recent_thread=None)`을 먼저 호출하면
> `✗ send failed: no active thread`가 뜨는데, Initial Task는 **아직 스레드가 없으므로**
> `_send`를 생략하고 `on_next_turn`으로 첫 `session.run` 트리거만 해야 합니다.
> **v1.0.1:** 첫 task 접수 후 `_initial_task_mode=False` — 이후 RUNNING 중 개입 입력은 `_send`(human_send 주입)로 현재 턴에 반영.

### 2.2 ask_user 선택지 패널 — 세로 목록 + 동적 높이 + wrap (결정 ③ 3-A)

- **옵션 세로 목록**: 옵션당 1줄 (`   [1] opt` / `   [2] opt` ...) — 가로 join 제거. (**agent-4 구현 완료 ✅**)
- **동적 높이**: `Window(height=callable → Dimension(min=1, max=8, preferred=n))` — prompt_toolkit이 **callable 높이를 지원** (소스 검증 완료).
  - `ChoicePanel.line_count()` = 질문 1 + 옵션 수 + (queued 1) (최대 8). (**agent-4 구현 완료 ✅**)
  - 옵션이 긴 경우 `wrap_lines=True`로 줄바꿈 → 잘림 없음.
  - 초과 시 마지막 줄 `(queued N — /skip)` 안내 (v1.0은 최대 8줄까지만 표시).
- **응답 UX는 기존 그대로**: 번호(`1`~`N`) → 옵션 텍스트 치환 → 질문 스레드+질문 에이전트 회신. 숫자 외 → 원문 회신. `/skip` → dismiss.

```python
# tui/app.py::_build_layout (agent-2 구현 대상)
def _choice_height() -> Dimension:
    return Dimension(min=1, max=8, preferred=self.choice_panel.line_count())

ConditionalContainer(
    Window(self.choice_panel.control(), height=_choice_height, wrap_lines=True, style="class:choice"),
    filter=self.choice_panel.has_pending,
)
```

### 2.3 로그 스크롤백 — 단일 소스 S2 + follow (결정 ① 1-A)

> **agent-3 소스 검증 정밀화:** wrap_lines=True에서 `_scroll_when_linewrapping`은
> cursor.y=C일 때 `vertical_scroll ∈ [C-height+1, C]` 범위를 **유지**합니다.
> - **직접 `vertical_scroll ± N` 조작은 무효**: V를 S+N으로 올리면 max_scroll(=C=S)에 **클램프**되어 되돌아감.
> - **단일 소스 S2 (v0.12 확정)**: `get_cursor_position`이 **`log_window.vertical_scroll`을 직접 읽음** —
>   별도 상태(`_scroll_y`) 불필요, 동기화 문제 원천 제거. 렌더마다 create_content가 현재 vertical_scroll을 읽고
>   `_scroll`이 클램프/유지 → 다음 렌더에서 갱신된 값을 읽음 (1렌더 수렴, agent-2 검증).
>   PgUp/PgDn/Alt+↑/↓ 핸들러는 `log_window.vertical_scroll = max(0, ...±N)` + `invalidate()`만 하면 됨.
>   마우스 휠(Window 내장 `_scroll_up/down` ±1)도 다음 렌더에서 콜백이 그 값을 읽어 유지 ✅.
> - **follow 플래그 (앱 레벨 관리)**: follow=True(기본) → append 시 tail 복귀. 사용자가 PgUp/휠 업으로
>   과거 탐색 → follow=False → 고정 유지. PgDn으로 맨 끝 도달 → follow=True 복귀.
>   LogBuffer는 **순수 버퍼 유지** (`line_count()`만 추가 — agent-4 구현 완료 ✅, 스크롤 상태는 SessionTUIApplication 소유).
> - ⚠️ **v0.13 함정**: 전역 kb 바인딩에서 `event.app`은 prompt_toolkit의 `Application`이지
>   **`SessionTUIApplication`이 아니다** (`self.app = Application(...)`으로 내부 보유). → `event.app.log_window`는
>   AttributeError. **반드시 `self` 클로저 방식으로 구성** (아래 `_build_app_kb` 참조).
> - ✅ **v1.0 (1-A 확정)**: ↑/↓는 입력 history 유지(변경 없음). 로그 스크롤은 **PgUp/PgDn·마우스 휠·Alt+↑/↓**
>   (`escape+up`/`escape+down`)로 제공. `/help`에 키 안내 추가.

```python
# tui/app.py — 단일 소스 S2 + self 클로저 전역 kb (agent-2 구현 대상, v1.0.1)
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import FormattedTextControl

# ★ _build_layout() 호출 전에 정의 (agent-4 초기화 순서 제안)
self.log_window = Window(
    FormattedTextControl(
        text=lambda: self.log_buffer.render(),
        get_cursor_position=lambda: Point(x=0, y=self.log_window.vertical_scroll),  # ★ 단일 소스
    ),
    wrap_lines=True,
    always_hide_cursor=True,        # 커서 숨김 (focusable=False 유지 — 입력줄 포커스 불변)
    allow_scroll_beyond_bottom=True,
)
self._log_follow = True             # ★ tail follow (앱 레벨 상태)

# ★ 전역 kb는 self 클로저 방식 — event.app 사용 금지 (v0.13 함정)
def _build_app_kb(self) -> KeyBindings:
    kb = KeyBindings()
    @kb.add("pageup")
    def _pgup(event):
        self.log_window.vertical_scroll = max(0, self.log_window.vertical_scroll - 10)
        self._log_follow = False
        self.app.invalidate()
    @kb.add("pagedown")
    def _pgdn(event):
        max_scroll = max(0, self.log_buffer.line_count() - 1)
        self.log_window.vertical_scroll = min(max_scroll, self.log_window.vertical_scroll + 10)
        if self.log_window.vertical_scroll >= max_scroll:
            self._log_follow = True       # 끝 도달 시 follow 복귀
        self.app.invalidate()
    # 결정 ① 1-A: Alt+↑/↓ = 로그 스크롤 (history와 충돌 없음)
    @kb.add("escape", "up")
    def _alt_up(event):
        self.log_window.vertical_scroll = max(0, self.log_window.vertical_scroll - 3)
        self._log_follow = False
        self.app.invalidate()
    @kb.add("escape", "down")
    def _alt_down(event):
        max_scroll = max(0, self.log_buffer.line_count() - 1)
        self.log_window.vertical_scroll = min(max_scroll, self.log_window.vertical_scroll + 3)
        if self.log_window.vertical_scroll >= max_scroll:
            self._log_follow = True
        self.app.invalidate()
    return kb

# append 시 follow 처리 (LogBuffer.on_append 콜백 또는 app.append_text/append_event에서):
#   if self._log_follow:
#       self.log_window.vertical_scroll = max(0, self.log_buffer.line_count() - 1)

app_kwargs = {
    "layout": ...,
    "full_screen": True,
    "paste_mode": True,
    "mouse_support": True,             # ★ 신규: 마우스 휠 (Window 내장 _scroll_up/down)
    "key_bindings": self._build_app_kb(),   # ★ 신규: 전역 kb (self 클로저)
    ...
}
```

- **tail follow**: 기본은 최신(하단) 자동 follow. PgUp/Alt+↑/휠 업으로 과거 탐색 시 follow 해제(`_log_follow=False`),
  PgDn/Alt+↓/휠 다운으로 끝 도달 시 follow 복귀. 새 append 시 follow=True면 tail로 자연 복귀.
- **마우스 휠**: `Window._mouse_handler` 내장 `SCROLL_UP/DOWN → _scroll_up/_scroll_down` (focusable 불필요).
  S2 단일 소스 상태에서 휠 업=과거 유지 / 휠 다운=tail 클램프.
- **주의**: full-screen alternate screen에서는 마우스 드래그 텍스트 선택이 기본 비활성 — Windows Terminal에서
  복사는 **Shift+드래그** 사용 (터미널 자체 기능). `/help`에 안내.

---

## 3. prompt_toolkit 검증 결과 (agent-2 + agent-3 소스 검증 수렴 + v0.12/0.13/v1.0.1)

| 검증 항목 | 결과 | 근거 |
|-----------|------|------|
| `Window(height=callable)` 동적 높이 | ✅ 지원 | `dimension.py::to_dimension`이 callable 호출. HSplit `_divide_heights`가 min→preferred→max 배분 |
| wrap_lines=True + `vertical_scroll` 직접 조작 | ❌ **클램프됨 (함정)** | `_scroll_when_linewrapping`이 cursor.y 기준 재계산. cursor 기본 (0,0) → vertical_scroll=0 강제. S+N으로 올려도 max_scroll=S에 클램프 |
| **S2 단일 소스**: `get_cursor_position`이 `log_window.vertical_scroll` 직접 읽기 | ✅ 스크롤 유지 | cursor.y=vertical_scroll → `[C-height+1, C]` 범위 유지. 휠 업/다운 모두 자연 동작. 동기화 상태 불필요 (v0.12) |
| follow 플래그 (tail follow / 과거 탐색 고정) | ✅ | 앱 레벨 `_log_follow`. True → append 시 tail 복귀, False → 고정 유지. LogBuffer는 순수 버퍼 |
| 마우스 휠 | ✅ (S2 + mouse_support 시) | `Window._mouse_handler` 내장 `SCROLL_UP/DOWN → _scroll_up/down` (focusable 불필요) |
| `mouse_support=True` Windows | ✅ 지원 | win32 네이티브(ConHost) + Windows Terminal(ConPTY) 모두. **실측(V10) 필수** |
| PgUp/PgDn 전역 바인딩 + 입력 포커스 유지 | ✅ | `Application(key_bindings=kb)` 전역. 입력줄 TextArea에 pageup/pagedown 기본 바인딩 없음 → 충돌 없음 |
| **전역 kb에서 `event.app` 사용** | ❌ **AttributeError (함정)** | `event.app`은 prompt_toolkit `Application`. `log_window`/`_log_follow`는 `SessionTUIApplication` 속성 → **`self` 클로저로 구성 필수** (v0.13) |
| ↑/↓를 로그 스크롤로 재매핑 | ✅ 가능 (1-A는 미채택) | 컨트롤 kb가 기본 history 탐색보다 우선. 단 history 대체 키 필요 — **1-A 확정으로 미적용** (↑/↓=history 유지) |
| **`_initial_task_mode` 1회성 해제 (v1.0.1)** | ✅ 필수 | 해제 안 하면 첫 턴 RUNNING 중 개입 입력이 `_send` 없이 다음 턴 프롬프트로만 전달 → v2.5 §4.4 위반. 첫 task 접수 시 `False`로 전환 |
| **회귀 분석 (Initial Task TUI 통합)** | ✅ 영향 0 | TTY일 때만 분기 → pytest(비-TTY)는 `_want_fullscreen_tui()`가 PYTEST_CURRENT_TEST로 False → 기존 경로 그대로 (agent-3) |

---

## 4. 변경 파일 요약 (v1.0.1)

| 파일 | 변경 | 담당 | 비고 |
|------|------|------|------|
| `src/agent_augury/tui/choice_panel.py` | 옵션 **세로 목록** render + `line_count(max_lines=8)` | ✅ **agent-4 완료** | 선택지 패널 (3-A) |
| `src/agent_augury/tui/log_buffer.py` | **순수 버퍼 유지** + `line_count()` 추가 | ✅ **agent-4 완료** | 로그 버퍼 (1-A) |
| `src/agent_augury/tui/app.py` | `initial_task_mode: bool = False` 생성자 + `handle_input` plain 분기(**1회성 해제**) + `mouse_support=True` + `key_bindings=self._build_app_kb()`(self 클로저) + `self.log_window`(단일 소스 S2) + `_log_follow` + 선택지 패널 동적 높이 + append follow 처리 | ⏳ agent-2 | 핵심 |
| `src/agent_augury/cli.py` | `_run_wizard_flow` TTY 분기(2-A) + `_run_repl_tui` Initial Task 대기 모드 + 안내 로그 | ⏳ agent-3/agent-2 | 핵심 |
| `src/agent_augury/tui/commands.py` | `/help`에 로그 스크롤 키 안내 (1-A: PgUp/PgDn·Alt+↑/↓·휠) | ⏳ agent-2 | 문서 정합 |
| `tests/` | `test_tui_choice_panel.py`(세로 목록 — ✅ agent-1 작성), `test_tui_app.py`(동적 높이/스크롤/follow/initial_task_mode), `test_initial_task_tui.py`(대기 모드) | ⏳ agent-1 | 회귀 방지 |
| `docs/tui/SESSION_TUI_REDESIGN.md` | D-A5 상태 갱신 ("v2.0 후속" → "본 설계로 확정") | ⏳ agent-2 | 상위 문서 정합 |

> **SSOT 불변:** `server.py` / `session.py` / `agent/loop.py` / `tools.py` / `system_prompt.py` / `config.py` — 변경 없음.
>
> **구현 주의 (팀 수렴):**
> ① S2 커서 동기화는 **단일 소스** — `get_cursor_position`이 `window.vertical_scroll`을 직접 읽음. 별도 스크롤 상태 변수 금지.
> ② `initial_task_mode`는 **생성자 파라미터**로 cli에서 전달 (handle_input이 참조).
> ③ PgUp/PgDn/Alt+↑/↓는 **Application 전역 kb** (input_bar 컨트롤 kb 아님) — **반드시 `self` 클로저로 구성, `event.app` 사용 금지** (v0.13).
> ④ LogBuffer는 순수 버퍼 — 스크롤/follow 상태는 app.py 소유.
> ⑤ 회귀: TTY일 때만 TUI 분기 → pytest(비-TTY)는 기존 경로 그대로 (test_repl/test_wizard 패치 유효).
> ⑥ **`_initial_task_mode`는 1회성 — 첫 task 접수 시 `False` 해제** (v1.0.1 — RUNNING 중 개입 입력의 `human_send` 주입 보장).

---

## 5. 검증 방법 (구현 후)

### 5.1 헤드리스 (PipeInput)

| # | 시나리오 | 기대 |
|---|----------|------|
| V1 | TUI 시작 + 첫 입력 "hello" → `_initial_task_mode`에서 `_send` 생략 + `on_next_turn("hello")` + **`_initial_task_mode=False` 해제** → `session.run(initial_prompt="hello")` | Initial Task 통합 + 1회성 |
| V2 | 첫 입력 전 빈 줄 Enter → ignored (대기 유지) | 빈 입력 안전 |
| V3 | ask_user 옵션 6개 → 패널 동적 높이 + 세로 목록 → "3" 입력 → 옵션[2] 치환 전송 | 선택지 패널 |
| V4 | PgUp/PgDn → `log_window.vertical_scroll` 증감 + `_log_follow` 전환 + 렌더 유지 (리셋 없음) | 로그 스크롤 (단일 소스 S2) |
| V4b | Alt+↑/↓ (`escape+up`/`escape+down`) → 로그 스크롤 증감 + follow 전환 | 1-A 확정 바인딩 |
| V5 | 마우스 휠(모의) → 로그 스크롤 (S2) | mouse_support |
| V6 | Initial Task 통합 후 `/quit` → 세션 정리 + 종료 (회귀 없음) | 종료 정합 |
| V7 | 비-TTY(파이프) → 기존 `_prompt_multiline` fallback (회귀 0) | fallback |
| V8 | follow=True에서 새 로그 append → tail 유지. PgUp 후 follow=False → append에도 고정 | follow 플래그 |
| V9 | **(v1.0.1)** 첫 task 접수 후 RUNNING 중 개입 입력 "방향 바꿔줘" → `human_send`로 현재 턴 주입 (다음 턴 프롬프트가 아님) | 1회성 해제 검증 |

### 5.2 실측 (Windows Terminal + cmd — 사용자 환경)

| # | 시나리오 |
|---|----------|
| V10 | **(필수)** 위저드 → TUI 진입 → Initial Task 입력(한글 IME) → Enter 제출 → 첫 턴 실행 |
| V11 | 로그 화면 초과 시 마우스 휠 / PgUp / PgDn / Alt+↑/↓ 스크롤 실측 (Windows Terminal + ConHost) |
| V12 | ask_user 옵션 8개 → 패널 표시/응답 실측 (잘림 없음 확인) |
| V13 | ↑/↓ = 입력 history 유지 확인 + Alt+↑/↓ 로그 스크롤 동작 실측 (1-A) |

---

## 6. ★ 사용자 결정 확정 기록 (v1.0)

> ✅ **2026-09 사용자 결정: 1-A / 2-A / 3-A** — 아래 상세표에서 채택된 항목입니다.

### 선택지 3-① 로그 스크롤 키 정책 (↑/↓ 방향키) — ✅ **1-A 채택**

| 선택지 | 동작 | 장점 | 단점 | 결정 |
|--------|------|------|------|------|
| **1-A** | ↑/↓ = **입력 history 유지** + PgUp/PgDn·마우스 휠·Alt+↑/↓ = 로그 스크롤 | history 편의 유지, 혼동 최소, 표준 패턴 | 로그 스크롤은 추가 키 사용 | ✅ **채택** |
| 1-B | 입력창이 **비어 있을 때만** ↑/↓ = 로그 스크롤 (내용 있으면 history) | 직관적, 키 1벌로 양쪽 | 상태 의존 동작 — 예측성 약간 저하 | — |
| 1-C | ↑/↓ = **로그 스크롤 전용** (history는 Ctrl+↑/↓ 또는 Alt+↑/↓로 이동) | 로그 탐색 최우선 | 입력 history UX가 비표준으로 바뀜 | — |

### 선택지 3-② Initial Task UX — ✅ **2-A 채택**

| 선택지 | 동작 | 장점 | 단점 | 결정 |
|--------|------|------|------|------|
| **2-A** | 위저드 직후 **TUI 바로 시작** → 로그 안내 + 입력줄로 task 입력 (Enter 제출) | 전체 흐름이 단일 TUI, 위저드→TUI 전환 1회, 일관 UX | 첫 턴 시작 전에 TUI 진입 (약간의 화면 전환) | ✅ **채택** |
| 2-B | 현행 유지 — 인라인 `_prompt_multiline` (Initial Task는 TUI 밖) | 변경 최소 | 사용자 불만(Initial Task가 TUI가 아님) 지속 | — |
| 2-C | 위저드 안에서 Initial Task를 **위저드 단계로 흡수** (마지막 위저드 질문으로) | TUI 시작 전 task 확정, 위저드 일관성 | 위저드가 길어지고 TUI 로그에 task 히스토리 없음 | — |

### 선택지 3-③ 선택지 패널 형태 (ask_user) — ✅ **3-A 채택**

| 선택지 | 동작 | 장점 | 단점 | 결정 |
|--------|------|------|------|------|
| **3-A** | 옵션 **세로 목록** + 패널 **동적 높이**(최대 8줄) + wrap | 모든 옵션 가독성, 잘림 없음, 화면 효율 | 옵션 많으면 패널이 커짐 (최대 제한) | ✅ **채택** |
| 3-B | 세로 목록 + **높이 고정 5줄** + 초과 시 "(+N more)" + `/skip` 안내 | 화면 과점유 없음 | 5줄 초과 옵션은 바로 안 보임 | — |
| 3-C | 현행 유지 (가로 1줄) + 패널 높이만 확대 | 변경 최소 | 옵션 많은 경우 여전히 잘림 | — |

---

## 7. 열린 결정 (v1.0 — 전부 확정)

| # | 항목 | 확정값 | 상태 |
|---|------|--------|------|
| D-1 | ↑/↓ 키 정책 | **1-A** (history 유지 + PgUp/PgDn·휠·Alt+↑/↓) | ✅ 확정 |
| D-2 | Initial Task UX | **2-A** (TUI 바로 시작 + 입력줄로 task) | ✅ 확정 |
| D-3 | 선택지 패널 형태 | **3-A** (세로 목록 + 동적 높이 + wrap) | ✅ 확정 |
| D-4 | 패널 최대 높이 | **8줄** | ✅ 확정 |
| D-5 | 로그 스크롤 단위 | **10줄** (PgUp/PgDn), 3줄 (Alt+↑/↓) | ✅ 확정 |
| D-6 | `_initial_task_mode` 1회성 해제 | 첫 task 접수 시 `False` | ✅ 확정 (v1.0.1) |

---

## 8. 구현 현황 (v1.0.1)

| 담당 | 파일 | 상태 |
|------|------|------|
| **agent-4** | `tui/choice_panel.py` (세로 목록 + line_count) | ✅ 구현 완료 + 리뷰 통과 (agent-3) |
| **agent-4** | `tui/log_buffer.py` (line_count 추가) | ✅ 구현 완료 + 리뷰 통과 (agent-3) |
| **agent-1** | `tests/test_tui_choice_panel.py` (세로 목록/line_count) | ✅ 작성 완료 |
| **agent-1** | `tests/test_tui_app.py` 확장 + `tests/test_initial_task_tui.py` | ⏳ 작성 중 |
| **agent-2** | `tui/app.py` (initial_task_mode/전역 kb/S2/동적 높이) | ⏳ 구현 중 |
| **agent-3/2** | `cli.py` (TTY 분기 + Initial Task 대기 모드) | ⏳ 구현 중 |
| **agent-2** | `tui/commands.py` (/help 키 안내) + `SESSION_TUI_REDESIGN.md` D-A5 | ⏳ 진행 예정 |

**구현 순서 (의존성):** choice_panel/log_buffer ✅ → app.py (S2/전역 kb/동적 높이) → cli.py (대기 모드) → commands.py /help → 테스트 통합 → 문서 정합
