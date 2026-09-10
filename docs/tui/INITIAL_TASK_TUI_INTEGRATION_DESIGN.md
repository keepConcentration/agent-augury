# agent-augury — Initial Task TUI 통합 + 선택지 패널 개선 + 로그 스크롤백 설계

> **Status:** 설계 초안 v0.9 (팀 검토 중) · **Date:** 2026-09
> **Author:** agent-1 (통합/검증) · 협업: agent-4 (원인 확정/선택지 패널/로그 스크롤 제안), agent-2 (prompt_toolkit 검증 대기), agent-3
> **상위 문서:** `SESSION_TUI_REDESIGN.md` v2.5 (D-A5 "위저드 TUI 통합은 v2.0 후속" — **본 문서가 이 항목을 앞당겨 확정**)
> **관련 문서:** `TUI_ENTER_SUBMIT_FIX_DESIGN.md` (Enter=제출 — 이미 구현됨), `INPUT_ROUTING_SLASH_UX_DESIGN.md` v0.5.1, `TUI_DISPLAY_INPUT_LAYER.md` v1.6
> **사용자 요청:** ① "InitialTask가 여전히 TUI가 아니다" 확인/원인/해결 ② "선택지가 3줄밖에 안 보여서 선택을 못 해" ③ **"선택지는 결과 문서(MD)에 함께 작성"** ④ (후속) "TUI 로그 스크롤이 안 된다 — ↑/↓가 입력 history로 동작"

---

## 0. 한 줄 결론

> **현재 `Initial Task` 입력은 full-screen TUI가 아니라 별도 인라인 `PromptSession`으로 받고 있다.**
> (`cli.py::_prompt_multiline`) — 사용자 보고가 정확하며, 설계 문서 v2.5 D-A5가 "위저드 통합은 v2.0 후속"으로
> 미뤄둔 항목이 그대로 남아 있는 것이다. 이번 설계에서 **Initial Task를 full-screen TUI 첫 입력으로 통합**한다.
> 동시에 사용자가 추가 보고한 **선택지 3줄 잘림**(패널 고정 height=3 + 옵션 가로 한 줄)과
> **로그 스크롤 불가**(로그 Window scroll 설정 없음 + ↑/↓가 입력 history로 동작)를 함께 해결한다.

```
Before: 위저드(input) → "--- Initial Task ---" 인라인 PromptSession → full-screen TUI → session.run(task)
After:  위저드(input) → full-screen TUI 시작 → 로그에 "--- Initial Task ---" 안내
        → TUI 입력줄에서 task 입력(Enter 제출) → session.run(initial_prompt=task)
        → 이후 동일 TUI에서 대화 계속 (기존 REPL 루프 그대로)
```

---

## 1. 문제 정의

### 1.1 Initial Task가 TUI가 아닌 이유 (코드 근거 — 4인 수렴)

| # | 항목 | 근거 코드 | 판정 |
|---|------|-----------|------|
| P1 | Initial Task 입력이 **인라인 PromptSession** | `cli.py::_prompt_multiline` — `PromptSession(multiline=True, key_bindings=kb)` + `session.prompt()` | full-screen `Application`(SessionTUIApplication)이 **아님** |
| P2 | 위저드 → Initial Task → TUI 순서 | `cli.py::_run_wizard_flow` — `_prompt_multiline` 호출 후 `_run_repl(cfg, initial_prompt=task)` | Initial Task가 TUI **밖**에서 처리됨 |
| P3 | 설계상 미완 항목 | `SESSION_TUI_REDESIGN.md` v2.5 **D-A5**: "위저드 통합 ⏳ — input() 유지, 전체 TUI 통합은 v2.0 후속" | 사용자 관찰 정확 — **설계가 구현보다 앞서 있었음** |
| P4 | 키맵 자체는 정상 | `_prompt_multiline`에 `enter`/`c-j` eager → `validate_and_handle()`, `escape+enter`/`escape+c-j` 개행 구현됨 | TUI_ENTER_SUBMIT_FIX 설계는 **이미 반영됨** — 문제는 "위치/형태" |

> **정리:** Enter=제출 키맵은 위저드에서도 이미 동작한다. 사용자가 "TUI가 아니다"라고 느끼는 실체는
> **Initial Task 단계가 full-screen TUI(로그/패널/상태바/입력줄 4분할)가 아니라 일반 터미널 인라인 프롬프트**라는 것.

### 1.2 선택지 3줄 잘림 (agent-4 발견 + 사용자 보고)

| # | 항목 | 근거 코드 |
|---|------|-----------|
| P5 | 선택지 패널 **height=3 고정** | `tui/app.py::_build_layout` — `Window(self.choice_panel.control(), height=3, style="class:choice")` |
| P6 | 옵션 **가로 한 줄 join** | `tui/choice_panel.py::render` — `"   ".join(f"[{i+1}] {opt}" ...)` |
| P7 | Window 기본 **wrap_lines=False** → 폭 초과 시 잘림 | prompt_toolkit `Window` 기본 동작 |

> 사용자 "선택하려고 하면 3줄밖에 안 보여서 선택을 못 해"의 정확한 원인:
> 질문 1줄 + 옵션 1줄(가로) + (queued N) 1줄 = **최대 3줄 구조** — 옵션이 길거나 많으면 옵션 줄이 화면 폭을
> 넘어 잘리고, 패널 높이 3으로 추가 표시 불가.

### 1.3 로그 스크롤 불가 (사용자 보고)

| # | 항목 | 근거 코드 |
|---|------|-----------|
| P8 | 로그 Window에 **scroll/focusable 설정 없음** | `tui/app.py::_build_layout` — `Window(self.log_buffer.control(), wrap_lines=True)` |
| P9 | `Application(mouse_support=True)` **미설정** → 마우스 휠도 안 됨 | `tui/app.py::__init__` — `app_kwargs`에 mouse_support 없음 |
| P10 | ↑/↓ = 입력 TextArea의 **FileHistory 탐색** (기본) | `tui/input_bar.py` — `history=FileHistory(...)` 연결 |

> 사용자 "위/아래 방향키 누르면 입력창에서 이전 프롬프트 불러옴" — 정확한 동작 설명.
> 설계 v2.5 §6/§11은 "PgUp/PgDn 로그 스크롤백 (v1.1)"로 **연기**했으나, 사용자가 지금 요구하므로 본 설계에 포함.

---

## 2. 해결 설계

### 2.1 Initial Task → full-screen TUI 첫 입력 통합 (핵심)

#### 2.1.1 원칙

1. **TUI는 1개 인스턴스로 위저드 직후 바로 시작** — "위저드 → Initial Task → TUI"가 아니라
   **"위저드 → TUI(첫 입력=Initial Task) → session.run"** 구조.
2. Initial Task 입력은 **기존 router/input_bar 라우팅을 그대로 재사용** (별도 프롬프트 경로 제거).
3. 비-TTY 환경에서는 **기존 인라인 `_prompt_multiline` fallback 유지** (회귀 0).

#### 2.1.2 데이터 흐름 (After)

```
위저드 종료 (config 저장)
   │
   ▼
_run_wizard_flow:
   tui_task = create_task(tui.run())            # ★ full-screen TUI 먼저 시작
   session_loop:                                  # initial_prompt=None
   ├─ ① TUI 로그에 안내 append: "--- Initial Task ---"
   │     "What would you like to do? [Multi-agent collaboration]"
   │     "(Enter to submit, Shift+Enter for newline)"
   ├─ ② 사용자가 TUI 입력줄에 task 입력 → Enter 제출
   │     └─ InputBar.accept_handler → router.route → kind="plain"
   │          └─ ★ initial-task 대기 모드: _send 생략 + on_next_turn(task)만 호출
   ├─ ③ session.run(initial_prompt=task)          # 첫 턴 실행 (기존과 동일)
   └─ ④ 이후 기존 REPL 루프 그대로 (plain → _send + on_next_turn)
```

#### 2.1.3 구현 요점 (tentative — agent-2 검증 후 확정)

```python
# cli.py::_run_repl_tui — initial_prompt=None인 경우 "Initial Task 대기 모드"로 진입
async def _run_repl_tui(session, *, initial_prompt, quiet):
    next_turn: asyncio.Queue[str | None] = asyncio.Queue()
    waiting_initial = initial_prompt is None      # ★ 신규 플래그
    ...
    tui = SessionTUIApplication(session, on_quit=..., on_next_turn=on_next_turn, ...)

    # Initial Task 안내 로그 (TUI 로그 버퍼에 출력)
    if waiting_initial:
        tui.append_text("--- Initial Task ---")
        tui.append_text("What would you like to do? [Multi-agent collaboration]")
        tui.append_text("(Enter to submit, Shift+Enter for newline)")

    async def session_loop():
        tui.set_running(True)
        try:
            # ★ 첫 턴: initial_prompt가 없으면 사용자 입력을 기다린 뒤 시작
            if waiting_initial:
                task = await next_turn.get()       # 사용자 입력 대기 (TUI 입력줄)
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
# tui/app.py::handle_input — plain 경로 분기
if result.kind == "plain":
    if self._initial_task_mode:      # ★ 신규: Initial Task 대기 중이면
        if self._on_next_turn is not None:
            self._on_next_turn(result.content)   # session.run 트리거만
        return
    await self._send(result.thread_id, result.content, mentions=None)
    if not self._running and self._on_next_turn is not None:
        self._on_next_turn(result.content)
    return
```

> **핵심 이유:** plain 경로가 `_send(recent_thread=None)`을 먼저 호출하면
> `✗ send failed: no active thread`가 뜨는데, Initial Task는 **아직 스레드가 없으므로**
> `_send`를 생략하고 `on_next_turn`으로 첫 `session.run` 트리거만 해야 한다.
> (설계 v2.5 §4.4 "IDLE 일반 입력 = 다음 턴 시작" 규칙과 동일 — Initial Task는 첫 턴의 special case)

#### 2.1.4 비-TTY fallback

```python
# cli.py — 비-TTY(파이프/CI)에서는 기존 _prompt_multiline 유지
if not _want_fullscreen_tui():
    task = _prompt_multiline("What would you like to do? [Multi-agent collaboration] ")
    return await _run_repl(str(output_path), initial_prompt=task, quiet=quiet)
```

- `_prompt_multiline` 자체는 **유지** (비-TTY/`--config` 없이 CI 실행 시 fallback).
- TTY에서는 위저드 후 바로 TUI 시작 → Initial Task는 TUI 입력줄로.

### 2.2 선택지 패널 개선 (3줄 잘림 해결)

#### 2.2.1 원칙

1. **옵션 세로 목록** (가로 join 제거) — 옵션당 1줄.
2. **패널 높이 동적 계산** — `height=Dimension(...)` (prompt_toolkit `Dimension`은 `min/mathematical` 등으로 가변 높이 가능).
3. **wrap_lines=True** — 긴 옵션 텍스트도 줄바꿈되어 잘리지 않음.
4. **최대 높이 제한** (예: 8줄) + 초과 시 "(+N more — /skip or scroll)" 안내 — 화면 과점유 방지.

#### 2.2.2 구현 요점 (tentative — agent-2 검증 후 확정)

```python
# tui/choice_panel.py::render — 옵션 세로 목록으로 변경
def render(self) -> FormattedText:
    pq = self.active
    if pq is None:
        return FormattedText([("", "")])
    lines = [f"❓ {pq.agent_id}: {pq.question}"]
    for i, opt in enumerate(pq.options, 1):          # ★ 세로 1줄씩
        lines.append(f"   [{i}] {opt}")
    if len(self.queue) > 1:
        lines.append(f"   (queued {len(self.queue) - 1})")
    return FormattedText([("bold", "\n".join(lines))])
```

```python
# tui/app.py::_build_layout — 동적 높이 + wrap
from prompt_toolkit.layout import Dimension

max_choice_lines = 8
def _choice_height() -> Dimension:
    n = choice_panel.line_count()          # 질문 1 + 옵션 수 + queued 1 (최대 max_choice_lines)
    return Dimension(min=1, max=max_choice_lines, preferred=n)

ConditionalContainer(
    Window(
        self.choice_panel.control(),
        height=_choice_height,             # ★ 동적
        wrap_lines=True,                   # ★ 긴 옵션 줄바꿈
        style="class:choice",
    ),
    filter=self.choice_panel.has_pending,
)
```

- `ChoicePanel.line_count()` 신규 메서드: `1 + len(options) + (1 if queue>1 else 0)` (최대 제한).
- **높이 초과 시:** 마지막 줄에 `(queued N — /skip or scroll)` + 옵션 스크롤(v2.0) — v1.0은 최대 높이까지만 표시.

#### 2.2.3 선택지 응답 UX (기존 유지)

- 번호(`1`~`N`) 입력 → 옵션 텍스트 치환 → 질문 스레드 + 질문 에이전트에게 회신 (기존 router 그대로).
- 숫자가 아닌 텍스트 → 원문 그대로 질문 스레드로 회신 (+ "(질문에 응답하는 대신 일반 메시지로 보냄)" 안내).
- `/skip` → 현재 질문 dismiss → 다음 대기 질문 노출.

### 2.3 로그 스크롤백 (신규 — 사용자 보고 반영)

#### 2.3.1 원칙

1. **마우스 휠 스크롤** — `Application(mouse_support=True)` + 로그 Window `allow_scroll_beyond_bottom=True`.
2. **PgUp/PgDn** — 로그 Window 직접 스크롤 (커스텀 키바인딩 — 입력줄에서도 동작).
3. **↑/↓ 정책은 사용자 결정** (§4-①) — 기본안: **현행 유지(입력 history)** + PgUp/PgDn·마우스 휠로 로그 스크롤.
4. **Alt+↑/↓** — 입력줄에서 로그 스크롤 단축키 (history와 충돌 없음, 선택지).

#### 2.3.2 구현 요점 (tentative — agent-2 검증 후 확정)

```python
# tui/app.py::__init__ — mouse_support 활성화
app_kwargs = {
    "layout": ...,
    "full_screen": True,
    "paste_mode": True,
    "mouse_support": True,               # ★ 신규: 마우스 휠 스크롤
    ...
}
```

```python
# tui/app.py::_build_layout — 로그 Window 스크롤 허용
log_window = Window(
    self.log_buffer.control(),
    wrap_lines=True,
    allow_scroll_beyond_bottom=True,     # ★ 신규
    # focusable: PgUp/PgDn 바인딩이 앱 레벨이라면 입력줄 포커스 유지 가능
)
```

```python
# tui/input_bar.py::_build_bindings — PgUp/PgDn 로그 스크롤 (앱 레벨 바인딩)
@kb.add("pageup")
def _scroll_up(event):
    app = event.app
    log_window = ...  # app 레이아웃에서 로그 Window 참조
    log_window.vertical_scroll = max(0, log_window.vertical_scroll - 10)

@kb.add("pagedown")
def _scroll_down(event):
    ...  # vertical_scroll + 10
```

> **agent-2 검증 필요:** ① `Window.vertical_scroll`을 앱 레벨 키바인딩에서 조작해도 입력줄 포커스가 유지되는지
> ② `mouse_support=True`가 Windows ConHost/Windows Terminal에서 안정적인지
> ③ ↑/↓를 로그 스크롤로 재매핑 시 FileHistory 탐색과의 충돌/대체 방법.

---

## 3. 사용자 결정 선택지 (★ 결과 문서에 포함 — 요청 반영)

> 아래 3건의 선택지를 결정해 주시면 최종 설계에 반영합니다. 답은 이 문서 댓글/다음 대화로 주셔도 되고,
> 편하신 대로 번호로 알려주셔도 됩니다. (예: "2-1, 3-2")

### 3-① 로그 스크롤 키 정책 (↑/↓ 방향키)

| 선택지 | 동작 | 장점 | 단점 |
|--------|------|------|------|
| **1-A (기본 권장)** | ↑/↓ = **입력 history 유지** + PgUp/PgDn·마우스 휠·Alt+↑/↓ = 로그 스크롤 | history 편의 유지, 혼동 최소, 표준 패턴 | 로그 스크롤은 추가 키 사용 |
| **1-B** | 입력창이 **비어 있을 때만** ↑/↓ = 로그 스크롤 (내용 있으면 history) | 직관적, 키 1벌로 양쪽 | 상태 의존 동작 — 예측성 약간 저하 |
| **1-C** | ↑/↓ = **로그 스크롤 전용** (history는 Ctrl+↑/↓ 또는 Alt+↑/↓로 이동) | 로그 탐색 최우선 | 입력 history UX가 비표준으로 바뀜 |

### 3-② Initial Task UX

| 선택지 | 동작 | 장점 | 단점 |
|--------|------|------|------|
| **2-A (기본 권장)** | 위저드 직후 **TUI 바로 시작** → 로그 안내 + 입력줄로 task 입력 (Enter 제출) | 전체 흐름이 단일 TUI, 위저드→TUI 전환 1회, 일관 UX | 첫 턴 시작 전에 TUI 진입 (약간의 화면 전환) |
| **2-B** | 현행 유지 — 인라인 `_prompt_multiline`(비-TTY도 동일) | 변경 최소 | Initial Task는 여전히 TUI 밖 (사용자 불만 지속) |
| **2-C** | 위저드 안에서 Initial Task를 위저드 단계로 흡수 (마지막 위저드 질문으로) | TUI 시작 전 task 확정, 위저드 일관성 | 위저드가 길어지고 TUI 로그에 task 히스토리 없음 |

### 3-③ 선택지 패널 형태

| 선택지 | 동작 | 장점 | 단점 |
|--------|------|------|------|
| **3-A (기본 권장)** | 옵션 **세로 목록** + 패널 **동적 높이**(최대 8줄) + wrap | 모든 옵션 가독성, 잘림 없음, 화면 효율 | 옵션 많으면 패널이 커짐 (최대 제한) |
| **3-B** | 세로 목록 + **높이 고정 5줄** + 초과 시 "(+N more)" + `/skip` 안내 | 화면 과점유 없음 | 5줄 초과 옵션은 바로 안 보임 |
| **3-C** | 현행 유지 (가로 1줄) + 패널 높이만 확대 | 변경 최소 | 옵션 많은 경우 여전히 잘림 |

---

## 4. 변경 파일 요약 (tentative)

| 파일 | 변경 | 비고 |
|------|------|------|
| `src/agent_augury/cli.py` | `_run_repl_tui`에 **Initial Task 대기 모드**(initial_prompt=None 분기) 추가. `_run_wizard_flow`에서 TTY면 바로 TUI 시작, 비-TTY면 기존 `_prompt_multiline` 유지 | 핵심 |
| `src/agent_augury/tui/app.py` | `mouse_support=True` + 로그 Window `allow_scroll_beyond_bottom=True` + 선택지 패널 **동적 높이(Dimension)+wrap** + `_initial_task_mode` 분기 | 핵심 |
| `src/agent_augury/tui/choice_panel.py` | 옵션 **세로 목록** render + `line_count()` 메서드 | 선택지 패널 |
| `src/agent_augury/tui/input_bar.py` | PgUp/PgDn(± Alt+↑/↓, 선택지 3-① 따라) 로그 스크롤 바인딩 | 로그 스크롤 |
| `src/agent_augury/tui/commands.py` | `/help`에 로그 스크롤 키 안내 추가 (선택지 3-① 확정 후) | 문서 정합 |
| `tests/` | `test_repl.py`(Initial Task 대기 모드), `test_tui_app.py`(동적 높이/스크롤), `test_tui_choice_panel.py`(세로 목록) 신규/갱신 | 회귀 방지 |
| `docs/tui/SESSION_TUI_REDESIGN.md` | D-A5 상태 갱신 ("v2.0 후속" → "본 설계로 확정") | 상위 문서 정합 |

> **SSOT 불변:** `server.py` / `session.py` / `agent/loop.py` / `tools.py` / `system_prompt.py` / `config.py` — 변경 없음.

---

## 5. 검증 방법

### 5.1 헤드리스 (PipeInput)

| # | 시나리오 | 기대 |
|---|----------|------|
| V1 | TUI 시작 + 첫 입력 "hello" → `_initial_task_mode`에서 `_send` 생략 + `on_next_turn("hello")` → `session.run(initial_prompt="hello")` 호출 | Initial Task 통합 |
| V2 | 첫 입력 전에 빈 줄 Enter → ignored (대기 유지) | 빈 입력 안전 |
| V3 | ask_user 옵션 6개 → 패널 높이 동적 계산(질문1+6+queued) + 세로 목록 렌더 → "3" 입력 → 옵션[2] 치환 전송 | 선택지 패널 |
| V4 | PgUp/PgDn → 로그 Window `vertical_scroll` 증감 | 로그 스크롤 |
| V5 | 마우스 휠 이벤트(모의) → 로그 스크롤 | mouse_support |
| V6 | Initial Task 통합 후 `/quit` → 세션 정리 + 프로그램 종료 (기존 종료 경로 회귀 없음) | 종료 정합 |
| V7 | 비-TTY(파이프) → 기존 `_prompt_multiline` fallback → rich print (회귀 0) | fallback |

### 5.2 실측 (Windows Terminal + cmd — 사용자 환경)

| # | 시나리오 |
|---|----------|
| V8 | **(필수)** 위저드 → TUI 진입 → Initial Task 입력(한글 IME) → Enter 제출 → 첫 턴 실행 |
| V9 | 로그가 화면을 넘칠 때 마우스 휠 / PgUp / PgDn 스크롤 실측 (Windows Terminal + ConHost) |
| V10 | ask_user 옵션 8개 → 패널 표시/응답 실측 (잘림 없음 확인) |
| V11 | ↑/↓ 정책(선택지 3-①)에 따른 history/로그 스크롤 동작 실측 |

---

## 6. 열린 결정 (사용자 확정 대기)

| # | 항목 | 기본안 | 상태 |
|---|------|--------|------|
| D-1 | ↑/↓ 키 정책 | **1-A** (history 유지 + PgUp/PgDn·휠·Alt+↑/↓) | ⏳ 사용자 결정 (3-①) |
| D-2 | Initial Task UX | **2-A** (TUI 바로 시작 + 입력줄로 task) | ⏳ 사용자 결정 (3-②) |
| D-3 | 선택지 패널 형태 | **3-A** (세로 목록 + 동적 높이 + wrap) | ⏳ 사용자 결정 (3-③) |
| D-4 | 패널 최대 높이 | 8줄 (v1.0) | ⏳ agent-2 검증 후 확정 |
| D-5 | 로그 스크롤 단위 | 10줄 (PgUp/PgDn) | ⏳ agent-2 검증 후 확정 |

---

## 7. 참고

- `SESSION_TUI_REDESIGN.md` v2.5 — D-A5 (위저드 통합 ⏳ → 본 설계로 확정), §4.4 (IDLE 일반 입력 = 다음 턴 시작)
- `TUI_ENTER_SUBMIT_FIX_DESIGN.md` — Enter=제출 구현 (이미 반영 — Initial Task 키맵도 동일 유지)
- `INPUT_ROUTING_SLASH_UX_DESIGN.md` v0.5.1 — router/선택지 응답/키맵
- `TUI_DISPLAY_INPUT_LAYER.md` v1.6 — Layout 4분할, LogBuffer, ChoicePanel, StatusBar
- prompt_toolkit 3.x — `Window.vertical_scroll` / `allow_scroll_beyond_bottom` / `Dimension` / `mouse_support` (agent-2 검증 대기)
- 사용자 재현: Windows 11 cmd, `agent-augury` (console_scripts)
