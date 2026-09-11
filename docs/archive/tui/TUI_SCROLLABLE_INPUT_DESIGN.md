# agent-augury — TUI 입력창 스크롤 연동 설계 (Scrollable Input)

> **Superseded in part by** [TUI_BOTTOM_DOCK_DESIGN.md](TUI_BOTTOM_DOCK_DESIGN.md) (v1.3):
> choice panel now lives **inside** the ScrollablePane above the input;
> `full_screen=False`. Log+input co-scroll and `keep_cursor_visible=False` remain.

> **Task:** 사용자 요청 — "사용자 입력 창을 고정시킬 필요가 없어. 타 cli 앱들 보니까 그렇게 안 하고 위로 스크롤 하면 사용자 입력창도 함께 아래로 내려가. 사용자가 입력했을 때 스크롤 다시 돌아오고. 이렇게 구현하기 위한 설계 문서 먼저 작성해줘."
> **Date:** 2026-09 · agent-1 (설계) · 기여: agent-2 (ScrollablePane 기술 검증·max_scroll/포커스), agent-3 (요구사항 명확화·기술 검토), agent-4 (라이브러리 영향·테스트 보강·**렌더 결함 분석 및 수정안**)
> **Scope:** 설계 문서. 기존 TUI_UX_FIX_DESIGN.md v1.1(② follow 인프라)의 확장.
> **Status:** **v1.0 확정 (옵션 A + 렌더 결함 수정)** — 사용자 방향 A 선택 확인. 실제 ScrollablePane 소스 분석으로 `keep_cursor_visible`/`keep_focused_window_visible` 기본값의 결함 발견 → 수정안 반영.

---

## 1. 요구사항 (사용자 명확화 반영)

사용자가 네 차례에 걸쳐 요구를 명확히 했다:

1. **"입력창을 고정시킬 필요가 없어. 타 cli 앱들 보니까 그렇게 안 하고 위로 스크롤 하면 사용자 입력창도 함께 아래로 내려가. 사용자가 입력했을 때 스크롤 다시 돌아오고."**
2. **"입력창도 로깅하라는건 아니야. 입력창은 CLI 맨 아래 고정이지만 스크롤 여부와 상관없이 고정하라는 건 아냐."**
3. **"입력창을 로깅하지 말라는 거고 사용자의 입력 내용은 기존처럼 로깅해야해."**
4. **"이렇게 되면 TUI관련 몇몇 라이브러리는 필요없어지는거지? 기본 cli와 비슷한 것 같은데"**

### 1.1 해석 (오해 방지)

| 항목 | 의미 | 오해 금지 |
|------|------|-----------|
| 입력창 기본 위치 | 화면 **맨 아래** (CLI 표준) | — |
| 스크롤 동작 | 위로 스크롤하면 **입력창도 함께 위로 올라감** | 입력창이 화면에 **강제 고정**되면 안 됨 |
| 입력 시 복귀 | 사용자가 **입력**하면 스크롤 **최하단 follow 복귀** | 복귀 기준(타이핑 vs 제출)은 §5.3 |
| 로깅 — 위젯 | **입력창 위젯 자체를 로그 버퍼에 넣는 게 아님** | 위젯은 스크롤 콘텐츠의 일부 |
| 로깅 — 입력 내용 | **사용자 입력 내용은 기존처럼 로그에 기록** (`✓ human → ...`) | 기존 동작 유지 |
| 라이브러리 | **prompt_toolkit/rich 유지** — 오히려 ScrollablePane 활용 | 제거되는 의존성 없음 (§3) |

### 1.2 현재 구조 (v1.2-1 구현 반영)

`tui/app.py::_build_layout()` (v1.2-1, ScrollablePane 도입):

```python
return HSplit([
    self.scrollable,          # ScrollablePane(HSplit([log_window, input_bar.widget]), keep_cursor_visible=True)
    ConditionalContainer(Window(choice_panel...), filter=has_pending),  # 고정
    Window(status_bar.control(), height=1),   # 고정
])
```

### 1.3 사용자 입력 로깅 경로 (유지 대상 — 변경 없음)

```python
async def _send(self, thread_id, content, *, mentions):
    ...
    self.append_text(f"✓ human → {thread_id} ({who}): {preview}")  # ← 사용자 입력 내용 로깅 (불변)
```

---

## 2. 설계 목표와 원칙

### 2.1 목표

1. 위로 스크롤하면 **로그 + 입력창이 함께** 올라간다 (일반 CLI 앱 방식).
2. 최하단 follow면 **입력창이 화면 맨 아래**에 위치한다.
3. 사용자가 **입력**하면 스크롤 **최하단 복귀**.
4. **입력 UX(TextArea 커서/IME/멀티라인) 보존**.
5. 기존 v1.1 ② 인프라(휠/F 토글/상태 표시) **재사용/확장**.
6. **사용자 입력 내용 로깅 유지**.
7. **TUI 라이브러리 유지** (prompt_toolkit/rich 제거 없음).

### 2.2 원칙

| # | 원칙 | 근거 |
|---|------|------|
| P1 | 입력 위젯 TextArea 유지 — 렌더/스크롤만 통합 | Windows IME·멀티라인·히스토리 보호 |
| P2 | 입력창 위젯과 로그 버퍼 분리 — 입력 내용은 기존 경로 로깅 | 사용자 명확화 2·3 |
| P3 | 스크롤 단위 = 전체 콘텐츠 (로그 + 입력창) | 요구사항 |
| P4 | follow 기본 True = 최하단 = 입력창 화면 하단 | CLI 표준 |
| P5 | 입력 시 follow 복귀 — §5.3 (제출 시 명시적, 타이핑 시 훅) | 요구사항 |
| P6 | 선택지 패널/상태바는 스크롤 밖 고정 | 이 앱 특유 UX |
| P7 | prompt_toolkit/rich 유지 — ScrollablePane(내장) 활용 | 사용자 질문 4 |
| **P8** | **ScrollablePane 자동 가시화 기본값을 끈다** (`keep_cursor_visible=False`, `keep_focused_window_visible=False`) — 렌더마다 입력창을 화면에 강제 고정하는 동작 제거 | **agent-4 렌더 결함 분석 (§4.4)** |

---

## 3. 라이브러리 영향 (사용자 질문 4번에 대한 답)

### 3.1 결론: 필요없어지는 라이브러리는 **없음** — 오히려 prompt_toolkit을 더 깊게 활용

| 라이브러리 | 역할 | 이번 변경 | 제거 여부 |
|-----------|------|-----------|-----------|
| **prompt_toolkit** | full-screen, TextArea(IME/멀티라인/히스토리), 마우스 휠, 레이아웃, **ScrollablePane** | ScrollablePane으로 로그+입력 감싸기 | ❌ 유지 |
| **rich** | 로그 ANSI 렌더링 | 불변 | ❌ 유지 |

**왜 기본 CLI(`print`+`input()`)로 못 바꾸나:**
1. prompt_toolkit이 하는 일은 "고정 입력창"만이 아님 — full-screen, 한글 IME, 히스토리, 키바인딩, 마우스, 실시간 로그+상태바, HSplit/Window 레이아웃.
2. "기본 CLI와 비슷"은 **스크롤 UX가** 그렇다는 것 — 구현 인프라는 유지.
3. 순수 `input()` 전환 시 잃는 것: 선택지 패널, 상태바, 실시간 로그 스트리밍+동시 입력(블로킹), 방금 해결한 UX 문제들이 재발. → B는 후순위 `--plain-cli` 옵션으로만 언급.

---

## 4. 구현 옵션 비교 (권장 확정)

### 4.1 옵션 A — ScrollablePane으로 로그+입력 감싸기 (권장)

```python
scroll_content = HSplit([self.log_window, self.input_bar.widget])
self.scrollable = ScrollablePane(scroll_content, keep_cursor_visible=True)
HSplit([self.scrollable, choice_panel(고정), status_bar(고정)])
```

### 4.2 옵션 B — 스크롤 동기화 (대안)

log_window만 스크롤하되 전체 콘텐츠 기준으로 해석 — "정말 함께 스크롤"이 아니라 시각적 효과.

### 4.3 옵션 비교

| 기준 | A (ScrollablePane) | B (스크롤 동기화) |
|------|--------------------|--------------------|
| "함께 스크롤" 실제 동작 | ✅ 진짜로 함께 | ⚠️ 덮는 방식 |
| 타이핑 시 복귀 | ✅ 훅으로 처리 (P8) | 🔄 수동 구현 |
| 입력 UX 보존 | ✅ TextArea 그대로 | ✅ TextArea 그대로 |
| 라이브러리 영향 | ✅ 제거 없음 | ✅ 제거 없음 |

**권장: 옵션 A + P8 수정.**

### 4.4 ⚠️ 렌더 결함 분석 (agent-4 — ScrollablePane 소스 근거) ★핵심

**현상 (사용자 회귀):** v1.2-1 구현(keep_cursor_visible=True 기본)에서 "로그 3줄만 보이고 입력창+상태바만 고정" — 사용자가 없애려던 "입력창 고정"이 재현됨.

**원인 (`.venv/.../scrollable_pane.py::write_to_screen` / `_make_window_visible`):**
1. `ScrollablePane`은 **렌더할 때마다** 포커스된 window(입력창)를 찾아 `_make_window_visible()`을 호출.
2. `keep_cursor_visible=True`(기본): 커서(입력창)가 항상 보이도록 `vertical_scroll`을 조정.
3. `keep_focused_window_visible=True`(기본): **포커스된 window(입력창)가 화면 안에 들어오도록** scroll을 조정 (`window_min_scroll`/`window_max_scroll` 계산).
4. → 우리가 `vertical_scroll=10**9`로 follow해도 **렌더 직후 입력창이 화면 하단에 붙도록 scroll이 되돌려짐**. 로그는 위로 밀려 "3줄만 보임".
5. 추가: `preferred_height`가 `min=0` — HSplit에서 scrollable이 화면 공간을 제대로 못 받으면 로그 영역이 0에 가까워질 수 있음.

**수정안 (P8):**
```python
self.scrollable = ScrollablePane(
    HSplit([self.log_window, self.input_bar.widget]),
    keep_cursor_visible=False,        # 렌더마다 커서(입력창) 강제 가시화 끔
    keep_focused_window_visible=False,# 렌더마다 입력창 강제 화면 고정 끔
    show_scrollbar=True,
)
# HSplit에서 스크롤 영역에 weight 부여 — 로그+입력이 남는 공간을 차지
HSplit([(self.scrollable, 1), choice_panel(고정), status_bar(고정)])
```

- **위로 스크롤**: `vertical_scroll` 감소 → 로그+입력창이 함께 위로 (진짜 스크롤).
- **타이핑**: `on_text_changed` 훅 → `_set_log_follow(True)` + `_follow_log_tail()` (keep_cursor_visible 대체).
- **제출**: `handle_input()` 시작에서 `_set_log_follow(True)` + `_follow_log_tail()`.

---

## 5. 상세 설계 (옵션 A + P8 기준)

### 5.1 레이아웃 변경

```python
def _build_layout(self) -> HSplit:
    return HSplit([
        (self.scrollable, 1),                          # ← 로그+입력 (weight 1, 함께 스크롤)
        ConditionalContainer(
            Window(self.choice_panel.control(), height=self._choice_height,
                   wrap_lines=True, style="class:choice"),
            filter=self.choice_panel.has_pending,      # 고정 (질문 항상 가시)
        ),
        Window(self.status_bar.control(), height=1, style="class:status"),  # 고정
    ])
```

### 5.2 기존 v1.1 ② 인프라 매핑 + max_scroll

| v1.1 ② 요소 | ScrollablePane 기준 변경 |
|-------------|--------------------------|
| `_log_follow` 기본 True | 유지 — "전체 콘텐츠 최하단 follow"로 확장 |
| `_follow_log_tail()` | `scrollable.vertical_scroll = 10**9` (내부 클램프) — **단 P8로 자동 가시화 꺼졌으므로 유지됨** |
| 휠 업/PgUp/Alt+↑ | `vertical_scroll` 감소 + `_log_follow=False` |
| 휠 다운/PgDn/Alt+↓ | `vertical_scroll` 증가 (+ 최하단 follow는 F키/제출로) |
| F 키 토글 | follow 토글 — True 시 최하단 복귀 |
| 상태바 FOLLOW/SCROLL | 유지 |

### 5.3 입력 시 follow 복귀 — 기준 확정 (agent-4 보강)

| 시점 | 동작 | 구현 |
|------|------|------|
| **타이핑 시작/중** | `_log_follow=True` + 최하단 복귀 — **`on_text_changed` 훅** | `input_bar.py`에 훅 추가 → `app._on_typing()` |
| **Enter 제출** | 명시적 최하단 복귀 + `_log_follow=True` | `handle_input()` 진입 전 |

```python
# input_bar.py — TextArea에 on_text_changed 연결
self.widget = TextArea(
    multiline=True, prompt=prompt, height=height,
    accept_handler=_accept, history=hist,
    on_text_changed=self._on_text_changed,   # ★ 타이핑 훅
)
def _on_text_changed(self, _buf) -> None:
    if self._app is not None and hasattr(self._app, "_on_typing"):
        self._app._on_typing()
```

```python
# app.py
def _on_typing(self) -> None:
    self._set_log_follow(True)
    self._follow_log_tail()      # scrollable.vertical_scroll = 큰 값 → 최하단
```

> P8 덕분에 `keep_cursor_visible`의 렌더 강제가 없으므로, 타이핑 훅이 follow를 실제로 제어한다.

### 5.4 사용자 입력 내용 로깅 (기존 동작 유지 — 변경 없음)

- `_send()`의 `append_text(f"✓ human → ...")` 로그 기록 **그대로 유지** (P2).
- `test_user_input_still_logged`로 v1.2-1부터 회귀 고정.

### 5.5 선택지 패널 상호작용 (기존 ③ 유지)

- 패널/상태바는 ScrollablePane 밖 고정 → 항상 가시.
- 패널 활성 시 PgUp/PgDn = 패널 옵션 스크롤, 휠 = ScrollablePane 스크롤 (기존 분기 유지).
- 로그 백업(①) 유지.

---

## 6. 결정 사항

| 요소 | 결정 | 근거 |
|------|------|------|
| status_bar | 하단 고정 (밖) | 세션 상태 상시 표시 |
| choice_panel | 항상 가시 (밖) | ask_user 질문 보호 |
| 입력창 위젯 | 함께 스크롤 (안) | 사용자 요구 |
| 사용자 입력 내용 로깅 | 기존 경로 유지 | 사용자 명확화 3 |
| 라이브러리 | 유지 (제거 없음) | 사용자 질문 4 |
| **ScrollablePane 자동 가시화** | **끔 (P8)** — 훅으로 대체 | 렌더 결함 (§4.4) |

---

## 7. 구현 로드맵 (갱신)

| 단계 | 범위 | 통과 기준 |
|------|------|-----------|
| **v1.2-1** | ScrollablePane 레이아웃 + **P8(자동 가시화 끔)** + v1.1 ② 매핑 + 로깅 회귀 테스트 | 로그+입력 함께 스크롤, follow 시 입력창 하단, `✓ human → ...` 로깅 유지 |
| **v1.2-2** | 타이핑 훅(`on_text_changed`) + 제출 복귀 | 타이핑→하단 복귀, 제출→하단 복귀, 로깅 유지 |
| **v1.2-3** | 패널/상태바 고정 정책 + PgUp/PgDn 분기 | 패널/상태바 항상 가시 |

---

## 8. 테스트 계획

| 테스트 | 검증 내용 |
|--------|-----------|
| `test_scrollable_pane_layout` | ScrollablePane(log+입력) + 고정 패널/상태바, **keep_cursor_visible=False** |
| `test_follow_tail_uses_scrollable` | `_follow_log_tail()`이 큰 값 설정 |
| `test_wheel_up_input_scrolls_away` | 휠 업 → 스크롤 감소, `_log_follow=False` |
| `test_typing_restores_follow` | **타이핑 훅 → follow 복귀** (신규) |
| `test_submit_restores_follow` | Enter 제출 → follow 복귀 + 최하단 |
| `test_user_input_still_logged` | `✓ human → ...` 로깅 유지 (v1.2-1부터) |
| `test_status_bar_always_visible` | 스크롤 중 상태바 고정 |
| (회귀) 기존 v1.1 | 휠/F/패널/로그 백업 등 |

---

## 9. 리스크와 대가

| 리스크 | 완화 |
|--------|------|
| ScrollablePane 자동 가시화가 follow를 깨뜨림 | **P8: keep_cursor_visible=False + keep_focused_window_visible=False** — 렌더 강제 제거, 훅으로 제어 |
| 타이핑 시 커서가 화면 밖 | `on_text_changed` 훅 → 명시적 follow 복귀 |
| max_scroll 추정 오차 | 큰 값(10**9) + 내부 클램프 |
| 제출 직후 커서 위치 | v1.2-2 명시 복귀 + 테스트 |
| PgUp/PgDn 패널/스크롤 충돌 | `has_pending_bool()` 분기 유지 |
| 입력 내용 로깅 누락 | `test_user_input_still_logged` 회귀 고정 |
| Windows IME | TextArea 불변 + 실제 실행 테스트 |

---

## 10. 변경 파일 요약

```
src/agent_augury/
  tui/app.py               # ScrollablePane(+P8), weight=1, _on_typing(), follow 매핑, 로깅 유지
  tui/input_bar.py         # on_text_changed 훅 (타이핑 시 follow)
  tui/status_bar.py        # (불변 — 하단 고정)
tests/
  test_tui_app.py          # §8 테스트 — v1.2-1부터 test_user_input_still_logged 포함
docs/
  tui/TUI_SCROLLABLE_INPUT_DESIGN.md   # 본 문서 (v1.0 확정)
```

---

## 11. 결론

사용자 요구 = **"입력창을 화면 하단에 강제 고정하지 말고, 로그와 하나의 스크롤 스트림으로 움직이게 하라"** + **"입력 내용은 기존처럼 로그에 기록"** + **"라이브러리는 유지"**.

- **옵션 A + P8** 로 구현: ScrollablePane의 **자동 가시화 기본값을 꺼서** 렌더마다 입력창이 강제 고정되는 결함을 제거하고, **로그+입력창이 진짜로 함께 스크롤**되게 한다.
- **타이핑** = `on_text_changed` 훅으로 follow 복귀, **제출** = `handle_input()`에서 명시 복귀.
- **입력 위젯(TextArea) 불변** → Windows IME·멀티라인·히스토리 보존.
- **입력 내용 로깅(`✓ human → ...`) 기존 경로 유지** — 회귀 테스트로 고정.
- **패널/상태바는 고정** — 정보/질문 창구 보호.
- **라이브러리 제거 없음** — ScrollablePane은 prompt_toolkit 내장.

**v1.2-1(P8+레이아웃+로깅 회귀) → v1.2-2(타이핑/제출 복귀) → v1.2-3(패널/상태바 정책)** 순으로 검증하며 진행한다.
