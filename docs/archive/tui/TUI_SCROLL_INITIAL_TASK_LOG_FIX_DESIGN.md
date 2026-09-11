# agent-augury TUI — 로그 스크롤 미작동 & Initial Task 첫 입력 로그 누락 (원인 분석/구현 설계)

> 버전: 0.6.2 대상 (현재 0.6.1)
> 날짜: 2026-09-10
> 상태: 설계 초안 (구현 전)
> 관련 문서:
> - docs/tui/TUI_SCROLLABLE_INPUT_DESIGN.md (v1.2 — ScrollablePane 도입)
> - docs/TUI_UX_FIX_DESIGN.md (v1.1 — 휠/F키/선택지 스크롤)
> - docs/tui/INITIAL_TASK_TUI_INTEGRATION_RESULT.md (v1.0 — Initial Task 대기 모드)

---

## 1. 개요

사용자가 보고한 결함 두 가지를 분석하고 구현 설계를 제시한다.

| # | 증상 | 영향 |
|---|------|------|
| B1 | 에이전트 작업 이력(로그)이 스크롤되지 않는다 | 과거 로그 열람 불가 |
| B2 | Initial Task 대기 모드에서 사용자가 첫 입력을 보내면 그 입력이 로그에 안 남는다 | 세션 시작 시 사용자 입력 이력 누락 |

결론 요약:
- B1의 주 원인은 prompt_toolkit의 마우스 휠 이벤트가 키 바인딩(`Keys.ScrollUp`/`Keys.ScrollDown`)을 타지 않고 **좌표 기반 mouse handler**로 전달되는데, `ScrollablePane` 내부 `Window`가 이 휠을 **자기 자신의 vertical_scroll**로 처리하려 하기 때문이다. 우리가 조작하는 `scrollable.vertical_scroll`은 휠 경로에서 갱신되지 않는다.
- B2는 `handle_input`의 Initial Task 분기가 `_send` 로깅 경로를 거치지 않고 `return`하며, `session.run(initial_prompt=...)`도 `human_send`를 거치지 않아 `send_message` 이벤트가 없기 때문이다.

---

## 2. 증상 재현

### B1 — 로그 휠 스크롤 미작동
1. `agent-augury`를 TTY에서 실행하고 Initial Task를 입력해 멀티 에이전트 작업을 시작한다.
2. 에이전트 스텝/도구 이벤트가 로그에 다수 쌓인다.
3. 로그 영역에서 마우스 휠을 위/아래로 돌린다.
4. 기대: 로그가 함께 스크롤되고 상태바가 `SCROLL`로 바뀐다.
5. 실제: 로그가 움직이지 않고 `FOLLOW` 상태가 유지된다. (PgUp/PgDn/Alt+↑/↓는 코드 경로상 동작하나, 휠이 주 수단이라 전체가 안 되는 것으로 보인다.)

### B2 — Initial Task 첫 입력 로그 누락
1. `initial_prompt=None`으로 TUI 실행(Initial Task 대기 모드).
2. TUI 입력줄에 "A 기능 분석해줘"를 입력하고 Enter.
3. 로그에는 `--- Initial Task ---` 안내만 있고, 사용자가 보낸 "A 기능 분석해줘"가 없다.
4. 이후 에이전트 스텝 로그만 쌓인다.

---

## 3. 원인 분석

### 3.1 B1 — 휠 스크롤이 `scrollable.vertical_scroll`을 건드리지 않는다

#### (1) prompt_toolkit 휠 입력 경로
- 표준 터미널의 마우스 휠은 `Keys.ScrollUp`/`Keys.ScrollDown` 키가 아니다.
- xterm SGR/Urxvt 휠은 `Keys.Vt100MouseEvent`, Windows 휠은 `Keys.WindowsMouseEvent`로 입력된다.
- prompt_toolkit 기본 `load_mouse_bindings()`의 `Keys.Vt100MouseEvent`/`Keys.WindowsMouseEvent` 핸들러가 이벤트를 파싱한 뒤, **마우스 좌표에 해당하는 `renderer.mouse_handlers` 핸들러**를 호출한다.

따라서 현재 `app.py::_build_app_kb()`에 있는 아래 바인딩은 표준 휠 경로에서 **발화하지 않는다(dead code)**.
```python
@kb.add(Keys.ScrollUp, eager=True)
def _wheel_up(event): ...
@kb.add(Keys.ScrollDown, eager=True)
def _wheel_down(event): ...
```
`Keys.ScrollUp`/`Keys.ScrollDown`은 일부 터미널이 `\x1b[62~`/`\x1b[63~`을 직접 보내는 예외 경로에서만 의미가 있다.

#### (2) ScrollablePane 내부 Window가 휠을 가로챈다
`ScrollablePane.write_to_screen()`은 내부 content(`HSplit([log_window, input_bar.widget])`)를 **content preferred height 전체 높이**의 가상 스크린에 렌더링한 뒤, `scrollable.vertical_scroll` 만큼 crop해 실제 화면에 복사한다.

이 구조에서 내부 `Window`들(`log_window`, TextArea window)은 자기 콘텐츠 전체 높이를 부여받으므로 `Window.vertical_scroll == 0`이다. 휠 이벤트는 `Window._mouse_handler()`가 호출되어 `self._scroll_up()`/`self._scroll_down()`으로 **해당 Window 자신**을 스크롤하려 하지만, 이미 콘텐츠 전체가 보이므로 아무 동작도 하지 않는다.

결과적으로 우리가 PgUp/PgDn/키바인딩에서 조작하는 `scrollable.vertical_scroll`은 휠 경로에서 전혀 갱신되지 않는다.

#### (3) 키 기반 스크롤은 동작하지만 보완 필요
`pageup`/`pagedown`/`escape+up`/`escape+down`은 app-level 비-eager 바인딩이 기본 `page_navigation`/`basic` 바인딩보다 우선순위가 높아 동작한다. 이 동작은 유지하되, 휠과 동일한 스크롤 헬퍼로 통합해 일관성을 확보한다(5.3).

### 3.2 B2 — Initial Task 입력이 로깅 경로를 타지 않는다

`app.py::handle_input()`의 `plain` 분기:
```python
if result.kind == "plain":
    if self._initial_task_mode:
        if self._on_next_turn is not None:
            self._on_next_turn(result.content)
        self._initial_task_mode = False
        return          # ← 로깅 없이 반환
    await self._send(result.thread_id, result.content, mentions=None)
```

- Initial Task 모드에서는 `_send()`를 타지 않으므로 기존 `append_text("✓ human → ...")` 로깅이 없다.
- 이후 `cli._run_repl_tui`가 `session.run(initial_prompt=task)`를 호출하는데, `session._run_impl()`은 이 initial prompt를 `server.human_send`를 거치지 않고 각 `agent.conversation`에 직접 append한다. 따라서 `send_message` 이벤트도 발생하지 않아 TUI 로그 어디에도 남지 않는다.

---

## 4. 설계 목표와 원칙

| # | 원칙 |
|---|------|
| P1 | 휠 스크롤은 로그+입력창을 함께 움직이는 `ScrollablePane`의 `vertical_scroll`을 조작한다 (기존 v1.2 설계 유지) |
| P2 | 휠 스크롤 시 `_log_follow=False`로 전환하고 상태바를 `SCROLL`로 갱신한다 (기존 v1.1 인프라 재사용) |
| P3 | 키보드(PgUp/PgDn/Alt+↑/↓)와 휠이 같은 스크롤 헬퍼를 공유해 동작 차이를 없앤다 |
| P4 | 선택지 패널이 활성화되어 있으면 휠은 로그가 아니라 패널 옵션을 스크롤한다 (기존 의도 복원) |
| P5 | Initial Task 첫 입력도 일반 입력과 동일한 수준으로 로그에 기록한다 (형식은 thread 없음을 명시) |
| P6 | 내부 prompt_toolkit private API 사용 범위를 최소화하고, prompt_toolkit 3.0.53 기준으로 회귀 테스트를 고정한다 |

---

## 5. 구현 설계

### 5.1 신규 모듈 — `tui/scrollable_pane.py`

`ScrollablePane`과 `Window`를 얇게 상속해 휠 이벤트를 앱 콜백으로 라우팅한다.

```python
from __future__ import annotations

from prompt_toolkit.layout import ScrollablePane, Window
from prompt_toolkit.mouse_events import MouseEventType


class FollowScrollablePane(ScrollablePane):
    # 로그+입력 영역의 휠 스크롤을 바깥 앱 콜백으로 위임한다.

    def __init__(self, content, wheel_handler=None, **kwargs):
        super().__init__(content, **kwargs)
        self._wheel_handler = wheel_handler

    def _copy_over_mouse_handlers(
        self, mouse_handlers, temp_mouse_handlers, write_position, virtual_width
    ):
        super()._copy_over_mouse_handlers(
            mouse_handlers, temp_mouse_handlers, write_position, virtual_width
        )
        if self._wheel_handler is None:
            return

        y0 = write_position.ypos
        x0 = write_position.xpos
        for y in range(write_position.height):
            row = mouse_handlers.mouse_handlers.get(y + y0)
            if not row:
                continue
            for x in range(virtual_width):
                handler = row.get(x + x0)
                if handler is not None:
                    row[x + x0] = self._wrap(handler)

    def _wrap(self, handler):
        wheel = self._wheel_handler

        def wrapped(event):
            if event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
                return wheel(event)
            return handler(event)

        return wrapped


class WheelScrollWindow(Window):
    # 선택지 패널처럼 고정 Window의 휠 스크롤을 앱 콜백으로 위임한다.

    def __init__(self, *args, wheel_handler=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._wheel_handler = wheel_handler

    def _mouse_handler(self, mouse_event):
        if self._wheel_handler is not None and mouse_event.event_type in (
            MouseEventType.SCROLL_UP,
            MouseEventType.SCROLL_DOWN,
        ):
            return self._wheel_handler(mouse_event)
        return super()._mouse_handler(mouse_event)
```

설계 포인트:
- `FollowScrollablePane`은 `super()._copy_over_mouse_handlers(...)`가 좌표를 이미 변환해 복사한 뒤, 해당 영역 핸들러만 감싼다. 따라서 비(非)휠 이벤트(클릭/드래그 등)는 기존 핸들러에 그대로 위임되어 TextArea 커서 이동 등이 보존된다.
- `MouseHandlers`는 렌더마다 새로 만들어지므로 핸들러가 이중으로 감싸일 일이 없다.

### 5.2 `app.py` 통합

#### (1) import/상수
```python
from prompt_toolkit.mouse_events import MouseEventType
from .scrollable_pane import FollowScrollablePane, WheelScrollWindow
```
```python
_WHEEL_DELTA = 3   # 기존 휠 키바인딩과 동일한 3줄 단위
```

#### (2) 공통 스크롤 헬퍼 추가
```python
def _scroll_log(self, delta: int) -> None:
    self.scrollable.vertical_scroll = max(
        0, self.scrollable.vertical_scroll + delta
    )
    self._set_log_follow(False)
    self.app.invalidate()
```

#### (3) 휠 핸들러 추가
```python
def _handle_log_wheel(self, event) -> None:
    if event.event_type == MouseEventType.SCROLL_UP:
        self._scroll_log(-self._WHEEL_DELTA)
    else:
        self._scroll_log(self._WHEEL_DELTA)
    return None

def _handle_panel_wheel(self, event) -> None:
    if event.event_type == MouseEventType.SCROLL_UP:
        self.choice_panel.scroll_line_up(self._CHOICE_MAX_LINES)
    else:
        self.choice_panel.scroll_line_down(self._CHOICE_MAX_LINES)
    return None
```

#### (4) ScrollablePane 교체
```python
self.scrollable = FollowScrollablePane(
    HSplit([self.log_window, self.input_bar.widget]),
    keep_cursor_visible=False,
    keep_focused_window_visible=False,
    wheel_handler=self._handle_log_wheel,
)
```

#### (5) 선택지 패널 Window 교체
`_build_layout()`의 선택지 패널 `Window`를 `WheelScrollWindow`로 바꾼다.
```python
ConditionalContainer(
    WheelScrollWindow(
        self.choice_panel.control(),
        height=self._choice_height,
        wrap_lines=True,
        style="class:choice",
        wheel_handler=self._handle_panel_wheel,
    ),
    filter=self.choice_panel.has_pending,
)
```

#### (6) 키 바인딩을 공통 헬퍼로 리팩터링
기존 `_build_app_kb()`의 PgUp/PgDn/Alt+↑/↓/`Keys.ScrollUp`/`Keys.ScrollDown` 핸들러에서 로그 스크롤 로직을 `_scroll_log()` 호출로 교체한다. 패널 활성 분기는 유지한다.

예:
```python
@kb.add("pageup")
def _pgup(event):
    if self.choice_panel.has_pending_bool():
        self.choice_panel.scroll_up(self._CHOICE_MAX_LINES)
        return
    self._scroll_log(-self._LOG_SCROLL_PAGE)
```

```python
@kb.add(Keys.ScrollUp, eager=True)
def _wheel_up(event):
    if self.choice_panel.has_pending_bool():
        self.choice_panel.scroll_line_up(self._CHOICE_MAX_LINES)
        return
    self._scroll_log(-self._WHEEL_DELTA)
```
`Keys.ScrollUp`/`Keys.ScrollDown` 바인딩은 예외 경로용으로 남겨두되, 이제 같은 헬퍼를 사용한다.

### 5.3 Initial Task 입력 로깅

`handle_input()`의 Initial Task 분기에서 반환 전에 로그를 기록한다.

```python
if result.kind == "plain":
    if self._initial_task_mode:
        preview = mask_sensitive(result.content)
        if len(preview) > 80:
            preview = preview[:77] + "..."
        self.append_text(f"✓ human → (initial task): {preview}")
        if self._on_next_turn is not None:
            self._on_next_turn(result.content)
        self._initial_task_mode = False
        return
    await self._send(result.thread_id, result.content, mentions=None)
```

형식 결정:
- 기존 `_send()`가 `✓ human → {thread_id} ({who}): {preview}`를 기록하므로, thread가 아직 없는 Initial Task는 `(initial task)`로 표기해 일관성을 유지한다.
- `append_text()`가 내부적으로 `_follow_log_tail()`을 호출하므로 기록 직후 최하단 follow가 유지된다.

---

## 6. 테스트 계획

### B1 스크롤
| 테스트 | 검증 |
|--------|------|
| `test_handle_log_wheel_up_down` | `_handle_log_wheel(SCROLL_UP/DOWN)` 호출 시 `scrollable.vertical_scroll`이 ±3 변하고 `_log_follow=False`, 상태바 `SCROLL` 반영 |
| `test_follow_scrollable_pane_wraps_wheel` | `FollowScrollablePane._wrap`이 SCROLL 이벤트를 wheel_handler로 보내고, 그 외 이벤트는 원 핸들러에 위임 |
| `test_panel_wheel_scrolls_options` | 패널 활성 시 `_handle_panel_wheel`이 `choice_panel.scroll_line_up/down`을 호출하고 로그 스크롤은 불변 |
| `test_key_scroll_uses_shared_helper` | PgUp/PgDn/Alt+↑/↓가 `_scroll_log`와 동일한 결과를 냄 (기존 테스트 갱신) |
| 회귀 | 기존 `test_pgup_pgdn_scroll_log`, `test_wheel_up_disables_follow`, `test_follow_log_tail`, `test_typing_restores_follow`, `test_submit_restores_follow` 유지 |

### B2 Initial Task 로깅
| 테스트 | 검증 |
|--------|------|
| `test_initial_task_mode_logs_first_input` | `initial_task_mode=True`에서 `handle_input("hello")` 후 `export_tail()`에 `✓ human → (initial task)`와 `hello` 포함 |
| 회귀 | `test_initial_task_mode_first_input_triggers_next_turn_only`는 `on_next_turn` 호출/`human_send` 미호출을 그대로 검증하되, 로그 검증 추가 |

---

## 7. 리스크와 완화

| 리스크 | 완화 |
|--------|------|
| prompt_toolkit private API(`_copy_over_mouse_handlers`, `_mouse_handler`) 사용 | `prompt-toolkit>=3.0.53`을 이미 고정. CI에서 3.0.53 기준 테스트 유지 + 버전 업그레이드 시 회귀 점검 |
| 휠이 입력창 위에서도 로그를 스크롤하게 됨 | 의도된 동작(v1.2 "로그+입력 함께 스크롤"). 입력창 클릭/드래그는 기존 핸들러 위임으로 보존 |
| follow sentinel(10**9) 직후 첫 휠이 한 박자 늦게 보일 수 있음 | 기존 `_follow_log_tail` 방식 유지. append마다 invalidate가 빈번해 체감 영향 미미. 필요 시 렌더 후 클램프값 캐시로 후속 보강 가능 |
| 선택지 패널 휠과 로그 휠 충돌 | 패널은 ScrollablePane 밖 고정 + `WheelScrollWindow`로 분리 라우팅. `has_pending_bool()` 분기 유지 |

---

## 8. 변경 파일 요약

```
src/agent_augury/tui/
  scrollable_pane.py        # 신규: FollowScrollablePane + WheelScrollWindow
  app.py                    # 휠 핸들러/공통 _scroll_log/패널 Window 교체/Initial Task 로깅
tests/
  test_tui_app.py           # B1/B2 테스트 추가 및 키 스크롤 테스트 갱신
  test_initial_task_tui.py  # (필요 시) cli 경로 로깅 검증 보강
docs/tui/
  TUI_SCROLL_INITIAL_TASK_LOG_FIX_DESIGN.md  # 본 문서
```

---

## 9. 검증 및 롤백

1. `uv run pytest tests/test_tui_app.py tests/test_initial_task_tui.py tests/test_tui_choice_panel.py` 통과.
2. 실제 TTY에서:
   - 로그 영역/입력창 위에서 휠 스크롤 시 로그가 함께 올라가고 상태바가 `SCROLL`로 전환.
   - `F` 키로 `FOLLOW` 복귀 시 최하단 정렬.
   - 선택지 패널 활성 시 휠이 패널 옵션을 스크롤.
   - Initial Task 입력 직후 로그에 `✓ human → (initial task): ...` 표시.
3. 문제 시 롤백: `FollowScrollablePane`/`WheelScrollWindow`를 제거하고 `ScrollablePane`/`Window`로 되돌린다. Initial Task 로깅은 `handle_input` 한 분기만 되돌리면 된다.
