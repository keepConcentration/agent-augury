# agent-augury TUI — 표시/입력 계층 상세 설계 (agent-2 담당)

> **역할:** agent-2 (prompt_toolkit Application / 표시 계층 / 입력 계층)
> **Date:** 2026-09 · **Rev:** v1.6 — **사용자 UX 확정 반영: "빈 입력 무시"**.
> ① 빈 입력 = **ignored (세션 계속)** — **사용자 확정** ✅ (기존 v1.3 설계와 동일 방향)
>    Enter=제출 UX에서 빈 Enter는 실수 가능성이 높은 입력 → 종료 조건으로 쓰지 않음.
>    종료는 `/quit`·`/exit`·`Ctrl+D`(EOF)·KeyboardInterrupt 4종으로만.
> ② test_repl.py의 `test_repl_exit_on_blank_input` → "빈 입력 → session.run 재호출 안 함 + 루프 지속" 검증 확정
> v1.5 변경사항: ① 종료 트리거 **4종** 표기 통일 (agent-4 v0.5.1 정합) — `/quit`·`/exit` / `Ctrl+D` / EOF(터미널 닫힘) / KeyboardInterrupt
> ② 평문 `quit`/`exit` = 일반 메시지 확정 (agent-4 P11) ③ Ctrl+D와 EOF 동일 ^D 입력임을 명시
> v1.4 변경사항: ① 평문 `quit`/`exit` 라우팅 확정 (일반 메시지, 종료 아님 — 라우터 순수 함수 원칙) ② 종료 트리거 정리
> v1.3 변경사항: ① **Ctrl+D 의미 재정의**: "입력 루프 종료 (세션은 계속)" → **"REPL 종료 → 세션 정리 → 프로그램 종료"**
> ② **빈 입력 = ignored (세션 계속)** ③ 종료 시퀀스 통일: `Ctrl+D → app.exit() → cli finally: tui_app.shutdown() → session.close()`
> v1.2 변경사항: ① `--repl` 플래그/1회 실행(`_run`) 경로 제거 → REPL 루프가 유일 진입점으로 승격
> ② 입력줄(Enter 제출)이 곧 "다음 질문" 입력 ③ §9 체크리스트 6번 강화
> v1.1 변경사항: ① InputBar `app` 참조 주입 명시 ② LogBuffer 콜백 주입 방식으로 통일 (agent-1 검증 반영)
> **상위 문서:** `SESSION_TUI_REDESIGN.md` (통합본 v2.5, agent-3/agent-1)
> **목적:** full-screen `Application` 전환의 Layout 구성, 로그 버퍼, 입력 키바인딩,
> 상태바, 종료/복원을 구현 가능한 수준으로 명세.
> **검증 근거:** hermes-agent `ui-tui/README.md` 공식 UX 키맵, `hermes_cli/pt_input_extras.py`
> (키 시퀀스 별칭), rich `Console(record=True)` API, prompt_toolkit 3.x API.

---

## 1. Layout 최종 확정 (4분할)

```
┌──────────────────────────────────────────────────────────┐
│ ① 로그 영역 (Window, wrap_lines=True, 1fr)               │
│    · FormattedTextControl(text=lambda: log_buffer.render())│
│    · tail view 자동 (콘텐츠 초과 시 하단 표시)             │
├──────────────────────────────────────────────────────────┤
│ ② 선택지 패널 (ConditionalContainer, height=3)           │
│    · 대기 PendingQuestion 있을 때만 표시                  │
│    · 없으면 영역 자체가 사라져 로그 공간 확보             │
├──────────────────────────────────────────────────────────┤
│ ③ 상태바 (Window, height=1)                              │
│    · threads / msgs / gate / phase / agents               │
│    · 이벤트 기반 즉시 갱신 + 1초 주기 보정 (하이브리드)   │
├──────────────────────────────────────────────────────────┤
│ ④ 입력줄 (TextArea, multiline=True, height=3)            │
│    · Enter=제출 (accept_handler) / Shift+Enter·Esc+Enter=개행 │
│    · Ctrl+D=프로그램 종료(세션 정리) / Ctrl+C=드래프트 클리어 │
└──────────────────────────────────────────────────────────┘
```

```python
# tui/app.py — SessionTUIApplication (골격)
from prompt_toolkit.application import Application
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

class SessionTUIApplication:
    def __init__(self, session, renderer, router, *, history_file):
        self.log_buffer = LogBuffer(self._invalidate)   # 콜백 주입 (테스트 용이)
        self.choice_panel = ChoicePanel()
        self.status_bar = StatusBar(session)
        self.input_area = InputBar(router, app_ref=self, history=history_file).widget
        self.app = Application(
            layout=Layout(self._build_layout()),
            full_screen=True,
        )
        # 컴포넌트들이 필요로 하는 app 참조를 여기서 최종 연결
        self.log_buffer.set_app(self.app)
        self.choice_panel.set_app(self.app)

    def _invalidate(self) -> None:
        """LogBuffer에 주입할 invalidate 콜백 (app 생성 전 안전)."""
        if hasattr(self, "app"):
            self.app.invalidate()

    def _build_layout(self):
        return HSplit([
            Window(self.log_buffer.control(), wrap_lines=True),
            ConditionalContainer(
                Window(self.choice_panel.control(), height=3, style="bg:#333333"),
                filter=self.choice_panel.has_pending,
            ),
            Window(self.status_bar.control(), height=1, style="bg:#222222"),
            Window(self.input_area, height=3),
        ])
```

**app 참조 획득 규칙 (agent-1 검증 반영):**
- `LogBuffer` / `ChoicePanel`은 **생성자에 invalidate 콜백 주입** (app 없이 단위 테스트 가능).
- `InputBar`는 `app_ref`(SessionTUIApplication 자신)를 주입받아 `self._app.create_task(...)` 사용.
- 최종 `app` 인스턴스 생성 후 `set_app(app)`으로 바인딩 — 생성 순서 의존성 제거.

---

## 2. LogBuffer — 로그 버퍼 (dirty 캐시 + tail view)

```python
# tui/log_buffer.py
from collections import deque
from prompt_toolkit.formatted_text import ANSI, FormattedText

class LogBuffer:
    """deque(maxlen=1000) + FormattedText 캐시. append 시 invalidate 콜백 호출."""

    def __init__(self, invalidate: Callable[[], None] | None = None, maxlen: int = 1000):
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._cache: FormattedText | None = None
        self._invalidate = invalidate      # 콜백 주입 (app 없이 단위 테스트 가능)
        self._app = None                   # set_app()으로 최종 연결

    def set_app(self, app) -> None:
        """Application 인스턴스 생성 후 호출 — invalidate를 app.invalidate에 연결."""
        self._app = app
        self._invalidate = app.invalidate

    def append(self, ansi_block: str) -> None:
        """render_event()가 만든 ANSI 블록을 줄 단위로 append."""
        self._lines.extend(ansi_block.split("\n"))
        self._cache = None                  # dirty
        if self._invalidate is not None:
            self._invalidate()

    def render(self) -> FormattedText:
        if self._cache is None:
            self._cache = ANSI("\n".join(self._lines))
        return self._cache

    def control(self) -> FormattedTextControl:
        return FormattedTextControl(text=self.render)
```

**성능 원칙:** `render()`는 dirty일 때만 `ANSI(...)` 재구성. 매 렌더 사이클 콜백이어도
캐시 히트 시 O(1). PgUp/PgDn 스크롤백은 v1.1 (`Window`에 `scroll_offset` 확장).

---

## 3. ChoicePanel — ask_user 선택지 패널

```python
# tui/choice_panel.py
class ChoicePanel:
    """PendingQuestion 큐 + ConditionalContainer 렌더러."""

    def __init__(self, invalidate: Callable[[], None] | None = None):
        self._queue: deque[PendingQuestion] = deque()
        self._formatted: FormattedText | None = None
        self._invalidate = invalidate
        self._app = None

    def set_app(self, app) -> None:
        self._app = app
        self._invalidate = app.invalidate

    def on_ask_user(self, agent_id, args) -> None:
        """cli.on_tool_event → tool==ask_user 분기에서 호출 (D8 경로 유지)."""
        self._queue.append(PendingQuestion(
            thread_id=args["thread"], agent_id=agent_id,
            question=args["question"], options=list(args.get("options") or []),
        ))
        self._formatted = None
        if self._invalidate is not None:
            self._invalidate()

    def answer(self) -> PendingQuestion | None:      # 응답 후 popleft
        pq = self._queue.popleft() if self._queue else None
        self._formatted = None
        if self._invalidate is not None:
            self._invalidate()
        return pq

    def reset(self) -> None:
        self._queue.clear()
        self._formatted = None

    def has_pending(self) -> bool:
        return bool(self._queue)

    def render(self) -> FormattedText:
        if self._formatted is None:
            q = self._queue[0] if self._queue else None
            if q is None:
                self._formatted = FormattedText([("", "")])
            else:
                lines = [f"❓ {q.agent_id}: {q.question}"]
                if q.options:
                    lines.append("   " + "   ".join(f"[{i+1}] {o}" for i, o in enumerate(q.options)))
                if len(self._queue) > 1:
                    lines.append(f"   (대기 질문 {len(self._queue)-1}개)")
                self._formatted = FormattedText([("bold", "\n".join(lines))])
        return self._formatted

    def control(self) -> FormattedTextControl:
        return FormattedTextControl(text=self.render)
```

**응답 규칙 (agent-4 라우터와 연동):**
- 활성 질문 + 숫자 `1..N` → `options[idx]` 치환 → `human_send(thread, mentions=[질문 에이전트])`
- 활성 질문 + 일반 텍스트 → 원문 그대로 같은 스레드/에이전트로 회신
- 활성 질문 없음 → 최근 활성 스레드로 broadcast

---

## 4. StatusBar — 상태바 (하이브리드 갱신)

```python
# tui/status_bar.py
class StatusBar:
    """threads / msgs / gate / phase / agents — 1초 주기 보정 + 이벤트 즉시 갱신."""

    def __init__(self, session, *, refresh_interval: float = 1.0):
        self._session = session
        self._refresh_interval = refresh_interval

    def _line(self) -> str:
        snap = self._session.server.snapshot()
        gate = self._session.gate
        phase = self._session.protocol.phase if self._session.protocol else "n/a"
        return (
            f"threads={len(snap['threads'])} · msgs={len(snap['messages'])} · "
            f"gate={'OPEN' if gate and gate.is_open else ('CLOSED' if gate else 'n/a')} · "
            f"phase={phase} · agents={len(snap['agents'])}"
        )

    async def run(self, app) -> None:
        """1초 주기 보정 태스크 — session.run과 병렬."""
        while True:
            await asyncio.sleep(self._refresh_interval)
            app.invalidate()          # snapshot()은 text=lambda에서 매 렌더 호출

    def control(self) -> FormattedTextControl:
        return FormattedTextControl(text=self._line)
```

- 이벤트 발생(로그 append / ask_user / on_tool_event) 시 `app.invalidate()`를 이미 호출하므로
  상태바도 같은 렌더 사이클에서 즉시 갱신됨 — **별도 이벤트 훅 불필요**.
- 1초 타이머는 이벤트가 없는 상태 변화(다른 에이전트의 스레드 생성 등) 보정용.

---

## 5. 입력 계층 — TextArea + accept_handler (핵심 함정 해결)

**함정:** `TextArea(multiline=True)`에 커스텀 `kb`만 넘기면 Enter 제출이 내부
accept 처리와 충돌할 수 있음. → **`accept_handler`로 Enter 제출을 명시**한다.

**v1.2+ (REPL 기본 모드 반영):** REPL(=세션 재사용 루프)이 **유일 실행 모드**이므로,
이 입력줄이 곧 "다음 질문" 입력 수단이다. 별도의 REPL 입력 경로(`input()`)는
존재하지 않는다 — Enter 제출 → `router.route` → human_send / 다음 `session.run(initial_prompt=...)`
이 단일 루프로 수렴한다. **추가 키바인딩/입력 수단이 필요 없다.**

```python
# tui/input_bar.py
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.widgets import TextArea

class InputBar:
    def __init__(self, router, *, app_ref=None, prompt: str = "👤 > ", history=None):
        """app_ref: SessionTUIApplication (또는 Application) — 비동기 전달용 create_task."""
        self._router = router          # tui/router.py (agent-4 담당)
        self._app = app_ref            # ★ app 참조 주입 (agent-1 검증 반영)
        self._kb = self._build_bindings()

        def _accept(buff: Buffer) -> bool:
            text = buff.text
            buff.reset()                                   # 입력줄 비움
            if text.strip():
                if self._app is not None:
                    self._app.create_task(self._router.route(text))
                else:
                    # app 없이 단위 테스트하는 경우: route를 동기 호출하거나
                    # 라우터가 이미 async면 asyncio.create_task로 (이벤트 루프 1개)
                    import asyncio
                    asyncio.create_task(self._router.route(text))
            return True                                    # accept (빈 입력도 accept — ignored로 라우팅)

        self.widget = TextArea(
            multiline=True,
            prompt=prompt,
            height=3,
            accept_handler=_accept,
            key_bindings=self._kb,
            history=history,             # FileHistory — ↑/↓ recall
        )

    def _build_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("escape", "enter")        # 기존 Esc+Enter 호환 (개행)
        def _nl1(event):
            event.current_buffer.insert_text("\n")

        @kb.add("shift", "enter")         # hermes 표준 (pt_input_extras로 시퀀스 정규화)
        def _nl2(event):
            event.current_buffer.insert_text("\n")

        @kb.add("c-d")                    # REPL 종료 → 프로그램 종료 (세션 정리, v1.3)
        def _quit(event):
            event.app.exit()

        @kb.add("c-c")                    # 드래프트 클리어 (세션은 계속 — 입력만 지움)
        def _clear(event):
            event.current_buffer.reset()

        return kb
```

**REPL 기본 모드에서의 역할 정리 (v1.6):**
- **Enter 제출 = "다음 질문" 제출.** REPL 루프의 `while True: question = input("> ")`가
  이 입력줄의 `accept_handler`로 대체된다. 루프 제어는 router의 `kind`로:
  - 일반 텍스트 → `session.run(initial_prompt=text)` (다음 턴 실행)
  - **평문 `quit`/`exit` → 일반 메시지 (plain/question_reply).** 라우터 순수 함수 원칙상
    평문은 에이전트에게 전송된다 — 종료 아님 (v1.4 확정, agent-1 제안 채택, agent-4 P11)
  - `/quit` `/exit` → **입력 루프 종료 → 세션 정리 → 프로그램 종료** (아래 §7)
  - **빈 입력 → ignored (세션 계속) — 사용자 UX 확정 ✅ (v1.6).** 기존 "enter=quit"(빈 입력=종료)은
    **폐기** — Enter=제출 UX에서 빈 Enter는 실수 가능성이 높은 입력. 종료는 `/quit`/Ctrl+D로만.
- **Ctrl+D = REPL 종료 → 프로그램 종료 (세션 정리 포함).** REPL이 유일 모드이므로
  입력 루프를 끝내면 더 이상 질문을 받을 수단이 없다 → "세션은 계속"이 성립하지 않는다.
  (v1.3 — agent-4 v0.5와 정합)

**키맵 최종 표 (v1.6):**

| 키 | 동작 | 구현 |
|----|------|------|
| `Enter` | 제출 (다음 질문/응답/명령) | `TextArea.accept_handler` (multiline=True 유지) |
| `Shift+Enter` / `Esc+Enter` | 개행 | `kb` → `buff.insert_text("\n")` |
| `Ctrl+D` | **REPL 종료 → 프로그램 종료 (세션 정리)** | `kb` → `app.exit()` |
| `Ctrl+C` | 드래프트 클리어 (세션/입력루프 계속) | `kb` → `buff.reset()` |
| `↑`/`↓` | 입력 히스토리 | `FileHistory` 기본 |

**라우팅 판정 요약 (입력 → action, v1.6):**

| 입력 | 라우팅 (router.kind) | 동작 |
|------|----------------------|------|
| 빈 입력 | `ignored` | 아무것도 안 함 — 루프/세션 계속 (**사용자 UX 확정** ✅) |
| `/quit` `/exit` | `quit` | 입력 루프 종료 → 세션 정리 → 프로그램 종료 |
| `/...` (기타) | `command` | 슬래시 명령 디스패치 |
| 활성 질문 + 숫자 `1..N` | `choice` | 옵션 치환 → 질문 스레드/에이전트로 회신 |
| 활성 질문 + 텍스트 | `question_reply` | 원문 → 질문 스레드/에이전트로 회신 (+안내 1줄) |
| 평문 텍스트 (quit/exit 포함) | `plain` | 최근 활성 스레드로 broadcast — **에이전트에게 전송** |
| Ctrl+D / EOF (터미널 닫힘) | (앱 레벨) | 입력 루프 종료 → 세션 정리 → 프로그램 종료 |

**Windows IME 주의:** 한글 조합 중 Enter는 win32 IME가 가로채 조합 확정용으로
처리 — 확정 후 별도 Enter 키 이벤트가 버퍼로 전달되므로 제출과 충돌 없음.
Windows Terminal에서 실측 검증을 테스트 항목으로 포함.

---

## 6. key_aliases — hermes pt_input_extras 이식

```python
# tui/key_aliases.py — import 시 1회 호출 (순수 함수로 테스트 가능)
def install_tui_key_aliases() -> int:
    """hermes pt_input_extras 축소판:
    - install_shift_enter_alias / install_ctrl_enter_alias
    - install_modify_other_keys_aliases (Kitty CSI-u / xterm modifyOtherKeys)
    - install_ignored_terminal_sequences (ESC[I/ESC[O → Keys.Ignore)
    """
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys
    ...
```

- 이식 범위: Shift+Enter/Ctrl+Enter → `(Escape, ControlM)`, Ctrl+letter/Alt+letter
  정규화, 포커스 이벤트 Ignore.
- 세부 구현은 hermes `pt_input_extras.py` 참조 (BSD-3-Clause, Apache-2.0 호환).

---

## 7. 종료/복원 (D13 — v1.3 Ctrl+D 의미 재정의 반영)

```python
# tui/app.py — lifecycle
async def run(self) -> None:
    try:
        await self.app.run_async()        # 세션 루프와 병렬 태스크
    finally:
        self.shutdown()

def shutdown(self) -> None:
    """세션 종료/예외 시 호출 — alternate screen 복원 + 상태 초기화."""
    try:
        self.app.exit()                   # full-screen 복원 (idempotent)
    except Exception:
        pass
    self.choice_panel.reset()
    self.input_area.buffer.reset()
```

**v1.2+ (REPL 기본 모드 반영):** `cli._run`(1회 실행)이 제거되고 REPL 루프가 유일
진입점이므로, 종료 시퀀스는 REPL 주 실행 경로(명칭 후보: `_run_repl` 유지 또는
`_run_session` 승격 — 통합 문서 v2.5에서 확정)의 finally에 귀속된다.

```
cli._run_repl (유일 진입점 — --repl 플래그 없음, --config/위저드 공통)
  ├─ Session.from_config(cfg, on_step, on_tool_event)
  ├─ SessionTUIApplication(...) 생성 + run_async (입력/표시 단일 화면)
  ├─ 루프: session.run(initial_prompt) → 다음 질문 입력 (accept_handler) → session.run(...)
  └─ finally:
       tui_app.shutdown()
       await _close_session(session)   # mirror/backend close (기존 유지)
```

**종료 트리거 4종 (v1.5 — agent-4 v0.5.1과 정합):**

| # | 트리거 | 동작 | 비고 |
|---|--------|------|------|
| 1 | `/quit` `/exit` (router kind="quit") | 입력 루프 종료 → 세션 정리 → 프로그램 종료 | 명시적 슬래시 명령 |
| 2 | `Ctrl+D` (입력줄) | `app.exit()` → `run_async()` 반환 → cli finally: `tui_app.shutdown()` → `session.close()` → 프로그램 종료 | ^D — `kb` 바인딩 |
| 3 | **EOF (터미널 닫힘/파이프 종료)** | 동일 경로 — 입력 스트림 종료 → 루프 break → 세션 정리 | Ctrl+D와 동일 입력(^D)이지만, 터미널 자체가 닫히는 "복구 불가" 케이스도 포함 (agent-4) |
| 4 | KeyboardInterrupt (프로세스 시그널) | Application exit → finally에서 동일 정리 | 전체 프로세스 시그널 (드래프트 클리어 아님) |

> 표기 정리: **Ctrl+D와 EOF는 터미널에서 동일한 ^D 입력**이다. 위 3번은 "터미널이 닫히거나
> stdin이 EOF로 끝나는 복구 불가 케이스"를 의미적으로 명시한 것 — `test_repl_eof_exits`가 검증.
> 평문 `quit`/`exit`은 **종료 트리거가 아님** (일반 메시지 — v1.4, agent-4 P11).
> 빈 입력도 **종료 트리거가 아님** (ignored — 사용자 UX 확정 ✅).

- **v1.3 변경:** 기존 "입력 루프 종료 (세션은 계속 — 패시브 원칙)"은 REPL 유일 모드에서
  **성립하지 않음** (입력 루프 종료 후 질문을 받을 수단이 없음). 따라서 입력 루프 종료 =
  세션 정리 = 프로그램 종료로 수렴. "패시브 원칙"은 **세션 실행 중**에는 유지된다
  (에이전트 루프는 사용자 입력과 독립적으로 계속 돌고, Ctrl+C가 드래프트만 지움).
- Ctrl+C: Application이 KeyboardInterrupt로 exit → finally에서 동일 정리.
- full-screen 동안 Ctrl+C는 **입력줄에만 전달**(드래프트 클리어)되어 에이전트 루프를 죽이지 않음.
- `/quit`/Ctrl+D 시 **현재 실행 중인 `session.run()` 태스크**: 완료 대기 후 정리하거나
  cancel — 통합 문서 v2.5의 session task 관리(§4.3)와 정합. (기본: run_async 반환 시점에
  session task 취소 → finally에서 close)

---

## 8. 렌더러 — event → ANSI 문자열 (비-TTY fallback 겸용)

```python
# tui/renderer.py
_style_console = Console(record=True, file=io.StringIO(), force_terminal=True)

def render_event(event: dict) -> str | None:
    """이벤트 → ANSI 문자열 1개. 표시할 게 없으면 None.
    기존 cli._log_step/_log_tool_event의 포맷 코드를 그대로 옮김 (동작 보존).
    비-TTY fallback: 반환값을 그대로 print() 하면 기존 rich 출력과 동일."""
    t = event["type"]
    if t == "step":
        if not event["result"].text:
            return None
        _style_console.print(f"💭 {event['agent_id']}:")
        _style_console.print(Markdown(event["result"].text))
    elif t == "send_message":
        if event["content"].startswith("[ask-user]"):   # D12 스킵
            return None
        _style_console.print(f"💬 [{event['author']} → {', '.join(event['delivered_to']) or 'broadcast'}][{event['thread_id']}]")
        _style_console.print(Markdown(_mask_sensitive(event["content"])))
    elif t == "tool":
        if event["tool"] in ("send_message", "create_thread", "read_resource"):  # D2-dedup
            return None
        if event["tool"] == "ask_user":                 # D11 — pinned 패널이 대신
            return None
        ...  # 기존 아이콘/path 포맷 그대로
    elif t == "create_thread":
        ...
    elif t == "read_resource":
        ...
    return _style_console.export_text(styles=True).rstrip("\n")
```

**주의 (agent-1 검증):** 기존 `_log_step`/`_log_tool_event`는 `_console.print()` 2회
호출이라 record 모드에서 끊김 → **"이벤트 → ANSI 문자열 1개" 순수 함수로 추출**.
기존 포맷(이모지/마크다운/마스킹) 100% 보존.

---

## 9. 검증 체크리스트 (통합 문서 적용 기준)

1. §4 TuiRenderer: `Console(record=True, force_terminal=True)` + 단일 print 리팩토링
2. §5 TextArea: `accept_handler` + Shift+Enter/Esc+Enter 개행 + Ctrl+C 드래프트 클리어
3. Layout 4분할: 로그/선택지(Conditional)/상태바/입력 순서 + 상태바 하이브리드 갱신
4. 종료 시퀀스: **종료 트리거 4종** (`/quit`·`/exit` / `Ctrl+D` / EOF / KI) → `app.exit()` →
   alternate screen 복원 → `session.close()` finally (**v1.3: "세션은 계속" 폐기 — 입력 루프 종료 = 세션 정리 = 프로그램 종료**)
5. 비-TTY fallback: `render_event` 결과 print → 기존 rich 출력 동일 (test_wiring의
   `_make_tui_adapter` None mock과 호환)
6. **REPL = 기본(유일) 실행 모드 (v1.2~v1.6):** TUI 안에서 다음 질문 루프 = 입력줄
   `accept_handler` 1개로 수렴. `--repl` 플래그/`_run`(1회 실행) 경로 **제거**.
   **빈 입력 = ignored (사용자 확정 ✅) / 평문 quit·exit = 일반 메시지 / 종료는 `/quit`·Ctrl+D·EOF·KI**.
   test_repl.py 재작성 — 세션 재사용 6건 유지 + 플래그 테스트 2건 삭제 (아래 §10)
7. **app 참조 획득 (v1.1 추가):** LogBuffer/ChoicePanel은 콜백 주입, InputBar는
   `app_ref` 주입, 최종 `set_app(app)` 바인딩 — 생성 순서 의존성 없음

---

## 10. 참고

- hermes-agent `ui-tui/README.md` — 공식 UX 키맵 (Enter=Submit, Shift+Enter=newline)
- hermes-agent `hermes_cli/pt_input_extras.py` — 키 시퀀스 별칭 (BSD-3-Clause)
- hermes-agent `hermes_cli/cli_output.py` — 출력 헬퍼 (참고용)
- prompt_toolkit 3.x — `Application`/`Layout`/`TextArea`/`BufferControl`/`PipeInput`
- rich 15 — `Console(record=True)` / `export_text(styles=True)`

**test_repl.py 영향 (v1.2~v1.6 — 사용자 UX 확정 + agent-1/4 정합 반영):**

| 테스트 | 상태 | 사유 |
|--------|------|------|
| `test_session_run_called_twice_conversation_accumulates` | 유지 (재작성) | 세션 재사용 핵심. input side_effect `["hello", ""]`(빈 입력=종료 전제) → `["hello", "world"]`(연속 질문) 또는 `["hello", "", "quit"]`(빈 입력 무시 + /quit 종료)로 변경 (agent-1) |
| `test_repl_exit_on_quit` | **재작성** | 평문 `quit` → `/quit`(슬래시) 기준으로 변경 (v1.4) — 종료 + 세션 정리 검증 |
| `test_repl_exit_on_exit` | **재작성** | 평문 `exit` → `/exit`(슬래시) 기준으로 변경 (v1.4) |
| `test_repl_exit_on_blank_input` | **변경 (재작성)** | 빈 입력 Enter = **ignored + 세션 계속** (종료 아님 — **사용자 UX 확정 ✅**). "빈 입력 → session.run 재호출 안 함 + 루프 지속" 검증으로 대체 (agent-4 P9) |
| `test_repl_eof_exits` | 유지 (재작성) | **EOF(터미널 닫힘/^D) → REPL 종료 → 세션 정리** 검증 (v1.5 — 종료 트리거 3번) |
| `test_repl_keyboard_interrupt_exits` | **재작성** | full-screen에서 Ctrl+C는 **드래프트 클리어** — "Ctrl+C → 세션/입력루프 계속" 검증으로 변경. 프로세스 KI 종료 경로는 별도 (agent-1) |
| `test_session_setup_called_once` | 무변경 | Session 재사용 가드 (설계 무관) |
| `test_session_close_called_once` | 무변경 | close idempotent (설계 무관) |
| `test_cli_repl_flag_with_config` | **삭제** | `--repl` 플래그 제거 |
| `test_cli_without_repl_uses_single_run` | **삭제/대체** | 1회 실행(`_run`) 경로 제거 → "`--config`도 REPL" 검증으로 대체 (agent-1) |
| `test_cli_wizard_default_is_repl` | 유지 | 위저드 모드 = REPL 기본 (이미 정합) |
| (신규) `test_cli_config_always_repl` | 신규 | `--config PATH` = 항상 REPL (agent-1 제안 반영) |
| (신규) `test_plain_quit_is_message` | 신규 | 평문 `quit`/`exit` = 일반 메시지로 라우팅 (v1.4 — route() 단위 테스트와 정합, agent-4 P11) |
