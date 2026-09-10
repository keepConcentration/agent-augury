# agent-augury TUI — 입력 라우팅 / 슬래시 명령 / ask_user UX / 키 바인딩 설계

> **Status:** v0.5.1 — **평문 quit/exit 처리 확정** (라우터 일관성: 일반 메시지) · **Date:** 2026-09
> **Author:** agent-4 (입력/UX 담당) · 상위 문서: `SESSION_TUI_REDESIGN.md` (v2.5)
> **hermes 레퍼런스:** `hermes_cli/slash_exec.py` (레지스트리 실행기),
> `hermes_cli/pt_input_extras.py` (키 시퀀스 별칭),
> `tui_gateway/slash_fuzzy.py` (설명-인식 퍼지 스코어링),
> `ui-tui/README.md` (hermes 공식 TUI 키맵 — Enter=Submit / Shift+Enter=newline)
>
> **v0.5 변경 요약 (사용자 UX 확정 반영):**
> - `--repl`을 **기본값**으로 설정 → REPL(=세션 재사용 루프)이 **유일 실행 모드**가 됨.
>   `--repl` 플래그와 그 반대(1회 실행 `_run()`) 코드는 제거 대상. **설계만 반영 (구현 X)**.
> - **빈 줄 Enter = 무시(ignored)로 통일** — 기존 `_run_repl`의 "빈 입력 = quit" 구동작 폐기.
>   "Enter=제출" UX와 결합하면 실수로 빈 Enter를 눌러 세션을 죽이는 사고를 방지.
> - **`/quit` 의미 재정의** — REPL이 유일 모드이므로 `/quit` = REPL 루프 종료 →
>   세션 정리(shutdown) → 프로그램 종료. 백그라운드 세션 유지는 v2.0 범위.
> - 라우팅 로직 자체는 REPL/1회 모드와 **무관하게 동작** (입력 1건 단위 순수 함수) —
>   오히려 단일 모드화로 분기 제거.
>
> **v0.5.1 변경 (agent-1 요청 — 평문 quit/exit 처리 확정):**
> - **평문 `quit`/`exit`는 일반 메시지(plain)로 라우팅** — 기존 `_run_repl`의
>   "평문 quit/exit = 종료" 구동작 폐기. 종료는 `/quit`·`/exit`·Ctrl+D·EOF로만.
> - 근거: ① 라우터는 순수 함수 — 특정 평문 단어에 특수 처리를 넣으면 예측 가능성 훼손
>   ② 사용자가 에이전트에게 "quit"이라는 단어를 전달하고 싶을 수 있음
>   ③ 슬래시 명령 체계(`/quit`)가 표준이므로 `/help` 화면에서 명확히 안내.

---

## 1. 목표

full-screen `Application`(hermes 패턴)으로 전환한 TUI에서, 사용자가 입력줄에
무엇을 치든 **예측 가능하고 안전하게** 라우팅되게 한다:

1. 에이전트가 던진 `ask_user` 선택지에 **번호/텍스트로 정확히 회신** (잘못된 스레드/잘못된 수신자 방지)
2. 일반 텍스트는 항상 **최근 활성 스레드**로 전달 (세션의 passive awareness 원칙 유지)
3. `/`로 시작하는 입력은 **슬래시 명령**으로 해석 (TUI 제어 — 입력 주입과 분리)
4. 키 바인딩/IME가 **Windows + macOS + Linux**에서 일관되게 동작
5. **상태 표시줄(status bar)** 로 사용자가 세션 상황(스레드/게이트/페이즈)을 항상 인지 (v1.0 포함)
6. **REPL이 유일 실행 모드** — 같은 Application + 세션 재사용 루프가 기본 UX
   (`--repl` 플래그·1회 실행 경로 제거, D-A4 승격)

---

## 2. 입력 라우팅 (Input Router)

### 2.1 판정 순서 (우선순위)

```
입력 text (strip 후)
 │
 ├─ 비어 있음 → 무시 (ignored)
 ├─ "/..." 로 시작 → 슬래시 명령 디스패치 (§3)
 │    └─ /quit·/exit → 종료 (kind="quit")
 ├─ 활성 PendingQuestion 존재?
 │    ├─ 숫자 1..N → 옵션 텍스트 치환 → human_send(질문 thread, mentions=[질문 에이전트])
 │    ├─ 숫자 외 → 원문 그대로 → human_send(질문 thread, mentions=[질문 에이전트])
 │    └─ (응답 후 pending → 다음 대기 질문 노출)
 └─ 일반 텍스트 (평문 "quit"/"exit" 포함) → human_send(최근 활성 스레드, mentions=[])
```

> **v0.5 — 빈 입력 의미 통일 (REPL 기본화):** 기존 `_run_repl`의 `input()` 루프는
> "빈 입력 = quit"이었다. REPL이 유일 모드가 되면서 이 의미를 **무시(ignored)**로
> 통일한다. 근거: (a) "Enter=제출" UX에서 빈 Enter는 실수 가능성이 높은 입력 —
> 세션을 죽이면 안 됨. (b) 종료는 `/quit`/`/exit`/Ctrl+D라는 명시적 수단이 이미 존재.
> (c) hermes TUI도 빈 Enter는 무시 (queue 등 특수 상태 외).
>
> **v0.5.1 — 평문 quit/exit는 일반 메시지 (라우터 일관성):** 기존 `_run_repl`은
> 평문 `"quit"`/`"exit"`도 종료로 처리했으나, REPL이 유일 모드가 되면서 **폐기**.
> 평문은 항상 `plain`으로 라우팅 (에이전트에게 전송). 종료는 `/quit`·`/exit`·Ctrl+D·EOF만.
> (agent-1 제안 승인 — §3.2/§8.2 참조)

### 2.2 핵심 정책 (팀 합의 — P1 확정)

**"활성 질문이 있는 동안에는 무조건 질문 스레드 + 질문 에이전트로 회신"**

- 근거: ask_user는 사용자가 응답해야 할 유일한 의무다. 이때 일반 텍스트를
  최근 활성 스레드로 broadcast하면 (a) 질문 에이전트가 답을 못 받고,
  (b) 다른 에이전트들이 혼선을 겪는다.
- 현재 `human_tui.py`는 숫자만 질문 스레드로 보내고, **숫자가 아니면
  최근 스레드로 broadcast**한다 → 이 정책으로 변경.
- 보강 (agent-2): 질문과 무관한 일반 메시지여도 질문 스레드로 가되,
  로그에 `(질문에 응답하는 대신 일반 메시지로 보냄)` 안내 1줄.
- **"기타(직접 입력)"는 이미 내장** — hermes clarify prompt의 "Enter on Other →
  자유 텍스트"와 동형. ask_user options에 없거나 숫자가 아닌 자유 응답은
  "원문 그대로 질문 스레드로 회신" 규칙이 이미 처리 (추가 작업 0, agent-2 확인).
- 탈출구: `/skip`(질문 dismiss), v2.0 `/broadcast <text>`.

> **v0.5 — 라우팅 컨텍스트 수명:** REPL이 유일 실행 모드이므로 이 라우팅 정책은
> **세션 전체 수명 동안 유지**된다. 기존 `_run`(1회 실행) 경로에 있던 TUI 모드 설정
> (`human_cfg`의 response_format/input_prompt/multiline/pin_options/choice_queue)과
> ask_user 핀 패널, `tui_adapter` 생성/해제는 **REPL 루프로 흡수**된다
> (§7 상태 모델 반영). 라우팅 판정에는 변화 없음 — 단지 컨텍스트가 루프 전체를 커버.

### 2.3 router.py — 코드 골격 (순수 함수, 테스트 가능)

```python
# tui/router.py
"""입력 라우팅 — 순수 함수. TUI/터미널 의존 없음 (PipeInput 불필요 테스트)."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Protocol

class HumanSender(Protocol):
    async def human_send(self, thread_id: str, content: str, *,
                         mentions: list[str] | None = None) -> str: ...

@dataclass
class RouteResult:
    """라우팅 결과 — 호출부(TUI)가 실행. 명령/질문 응답/일반 구분."""
    kind: str                       # "command" | "choice" | "question_reply" | "plain" | "ignored" | "quit"
    content: str = ""               # 전송할 최종 content
    thread_id: str | None = None    # 질문 응답 시 질문 thread
    mentions: list[str] | None = None  # 질문 응답 시 [질문 에이전트]
    notice: str | None = None       # 로그에 띄울 안내 문구 (예: "(질문 응답 대신 일반 메시지)")
    command: str | None = None      # 슬래시 명령 이름 (kind=="command")
    args: str = ""                  # 슬래시 명령 인자 문자열

async def route(text: str, ctx: RouterContext) -> RouteResult:
    """입력 1건을 라우팅. ctx: active_question, recent_thread, snapshot 접근자."""
    text = text.strip()
    if not text:
        return RouteResult(kind="ignored")          # ★ 빈 입력 = 무시 (v0.5 — REPL 기본화)
    if text.startswith("/"):
        name, _, args = text[1:].partition(" ")
        if name in ("quit", "exit"):
            return RouteResult(kind="quit", command=name)
        return RouteResult(kind="command", command=name, args=args.strip())
    pq = ctx.active_question
    if pq is not None:
        option_text = _resolve_option(pq, text, response_format=ctx.response_format)
        if option_text is not None:
            return RouteResult(kind="choice", content=option_text,
                               thread_id=pq.thread_id, mentions=[pq.agent_id])
        # 질문과 무관한 일반 텍스트도 질문 스레드+에이전트로 회신 (P1)
        return RouteResult(kind="question_reply", content=text,
                           thread_id=pq.thread_id, mentions=[pq.agent_id],
                           notice="(질문에 응답하는 대신 일반 메시지로 보냄)")
    # 평문 "quit"/"exit" 포함 모든 일반 텍스트 → plain (v0.5.1: 평문 종료 폐기)
    return RouteResult(kind="plain", content=text,
                       thread_id=ctx.recent_thread, mentions=None)

def _resolve_option(pq: PendingQuestion, text: str, *, response_format: str) -> str | None:
    """숫자 입력이 유효 옵션이면 옵션 텍스트(또는 번호) 반환, 아니면 None."""
    if not pq.options:
        return None
    try:
        idx = int(text.strip())
    except ValueError:
        return None
    if 1 <= idx <= len(pq.options):
        return pq.options[idx - 1] if response_format == "text" else text.strip()
    return None
```

- `route()`는 **결정만** 하고 실행(await human_send)은 호출부(TUI)에서 —
  테스트는 RouteResult 필드만 단언하면 됨.
- `_resolve_option`은 기존 `HumanTUIAdapter._resolve_option` 로직을 그대로 승격
  (호환 shim 유지).
- **평문 `quit`/`exit`는 `plain`으로 처리** (v0.5.1) — `/quit`만 종료. 라우터는
  특정 평문 단어를 특별 취급하지 않는다 (순수 함수 원칙).

### 2.4 전송 피드백 (v1.0 필수 — agent-2 UX 1번)

- **전송 성공 시 로그에 안내 1줄**: `✓ human → thread-1: <내용 앞부분>`
  (민감정보 마스킹 적용).
- 근거: "전송이 안 됐다"고 느끼는 현재 화면 깨짐 문제의 UX 해법.
  입력이 human_send로 즉시 push되고 다음 step에서 [radio]로 흡수되는 흐름은
  그대로 두되, 사용자가 **눈으로 확인**할 수 있게만 함.
- 실패 시: `✗ 전송 실패: <오류>` (예: 스레드 없음, human 미등록).

---

## 3. 슬래시 명령 (Slash Commands)

### 3.1 레지스트리 패턴 (hermes `EXECUTORS` 축소판)

```python
# tui/commands.py
"""슬래시 명령 레지스트리 — 순수 함수. 렌더링/실행은 호출부(TUI)에서."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable

@dataclass(frozen=True)
class SlashCommand:
    name: str
    summary: str
    handler: Callable[[dict[str, Any], str], str]  # (ctx, args) → 출력 텍스트

COMMANDS: dict[str, SlashCommand] = {}

def register(cmd: SlashCommand) -> None:
    COMMANDS[cmd.name] = cmd

def dispatch(name: str, args: str, ctx: dict[str, Any]) -> str:
    """미지의 명령이면 안내 문자열 반환. 핸들러 예외도 안내로."""
    cmd = COMMANDS.get(name)
    if cmd is None:
        return f"알 수 없는 명령: /{name}  (/help 참고)"
    try:
        return cmd.handler(ctx, args)
    except Exception as exc:  # noqa: BLE001 — 명령 실패는 로그 안내로
        return f"/{name} 실행 실패: {exc}"

# -- v1.0 명령 정의 -----------------------------------------------------------

register(SlashCommand("help", "명령 + 키 바인딩 안내",
    lambda ctx, a: _help_text()))
register(SlashCommand("status", "스레드/메시지 수, 게이트, 페이즈",
    lambda ctx, a: _status_text(ctx["snapshot"], ctx.get("gate"), ctx.get("phase"))))
register(SlashCommand("threads", "스레드 목록 + 최근 활성 스레드",
    lambda ctx, a: _threads_text(ctx["snapshot"], ctx.get("recent_thread"))))
register(SlashCommand("clear", "로그 버퍼 비우기",
    lambda ctx, a: _clear_buffer(ctx)))   # ctx["log_buffer"].clear() 후 ""
register(SlashCommand("skip", "현재 질문 dismiss → 다음 질문",
    lambda ctx, a: _skip_question(ctx)))  # ctx["choice_panel"].pop() 후 안내
# quit/exit은 router에서 처리 (kind="quit") — 명령으로 등록하지 않음
```

- 핸들러는 **순수 함수**: ctx(snapshot, gate, phase, log_buffer, choice_panel 접근자)를
  받아 출력 문자열 반환. 렌더링(ANSI/스타일)은 호출부(TUI 로그 버퍼)에서.
- `ctx`는 dict로 전달 (타입 힌트만 `dict[str, Any]`) — 의존성 최소화, 테스트 용이.
- hermes `slash_exec.py`의 `CommandContext`/`CommandReply` dataclass 분리를 축소 적용
  (surface 분리는 지금 불필요 — 단일 surface).

### 3.2 v1.0 명령 집합

| 명령 | 동작 | 출력 |
|------|------|------|
| `/quit` `/exit` | **REPL 루프 종료 → 세션 정리(shutdown) → 프로그램 종료** — router kind="quit" | 없음 (루프 break + shutdown) |
| `/help` | 명령 + 키 바인딩 안내 | 텍스트 → 로그 버퍼 |
| `/status` | 스레드/메시지 수, 게이트, 페이즈 — 상태바 상세판 | `snapshot()` 기반 |
| `/threads` | 스레드 목록 + 최근 활성 스레드 표시 | `snapshot()` 기반 |
| `/clear` | 로그 버퍼 비우기 | — |
| `/skip` | 현재 PendingQuestion dismiss → 다음 대기 질문 | 확인 문구 |

> **v0.5 — `/quit` 의미 재정의 (REPL 기본화):** REPL이 **유일** 실행 모드이므로
> "입력 루프만 종료하고 세션은 계속"이라는 기존 정의는 더 이상 성립하지 않는다.
> `/quit` = **REPL 루프 종료 → `session.close()` (mirror/backend 정리) → alternate
> screen 복원 → 프로그램 종료**가 기본 의미. "백그라운드로 세션을 두고 나중에
> 재개" 같은 시나리오는 v2.0 범위 (D-A12 busy 큐/세션 재개와 함께)로 명시.
>
> **v0.5.1 — 평문 quit/exit는 종료 아님:** 종료 수단은 **`/quit`·`/exit`·Ctrl+D·EOF** 4가지뿐.
> 평문 `"quit"`/`"exit"`는 일반 메시지로 에이전트에게 전송된다. `/help` 화면에서
> "종료: /quit (또는 Ctrl+D)"로 명확히 안내 — 사용자가 평문 quit를 입력해도
> 종료되지 않는 이유를 인지하도록.

### 3.3 v2.0 후보 (설계만, 구현 보류)

- `/broadcast <text>` — 질문 무시하고 일반 메시지 broadcast
- `/reconfigure` — 위저드 재실행
- `/gate` — 게이트 상태 확인/조작 (protocol 연동)
- `/focus <agent>` — 특정 에이전트에게만 전송 (mentions 지정)
- 슬래시 명령 **자동완성** — `slash_fuzzy.py` 설명-인식 퍼지 스코어링 이식
  (prompt_toolkit `Completer`로 `/` 입력 시 드롭다운)

### 3.4 설계 원칙

- 슬래시 명령은 **에이전트에게 전달되지 않는다** (사용자 메시지 아님).
- `/quit`은 **REPL 루프 종료 + 세션 정리 + 프로그램 종료** (v0.5 재정의 — §3.2 참조).
- 평문 텍스트는 **모두 에이전트 메시지** (v0.5.1 — 평문 quit/exit도 예외 없음).
- 모든 명령 핸들러는 **동기 + 예외 안전** (`try/except` → 오류 문구를 로그에).

---

## 4. ask_user 선택지 UX

### 4.1 pinned 패널 (full-screen 레이아웃의 중간 고정)

```
┌─────────────────────────────────────────────┐
│ ① 로그 영역 (FormattedTextControl, 스크롤)    │
├─────────────────────────────────────────────┤
│ ② 선택지 패널 (ConditionalContainer, 고정)    │
│    ❓ agent-1: DB는 뭘 쓸까?                 │
│    [1] postgres  [2] mysql  (대기 2개)       │
├─────────────────────────────────────────────┤
│ ③ 상태바 (1줄, 항상 표시)                    │
│    threads=3 · msgs=12 · gate=OPEN · phase=P3_EXECUTE · agents=2
├─────────────────────────────────────────────┤
│ ④ 입력줄 (TextArea, 하단 고정)               │
│    👤 > _                                     │
└─────────────────────────────────────────────┘
```

- `ConditionalContainer` 조건: `len(pending_queue) > 0`
- 응답/스킵 시 `pop()` → 다음 질문 표시, 큐 비면 패널 숨김.
- 기존 `bottom_toolbar` 렌더링 로직(`_render_toolbar`)을 패널 렌더러로 승격.

### 4.2 다중 질문 큐 (기존 코드 재사용)

```python
# tui/choice_panel.py
class ChoicePanel:
    def __init__(self) -> None:
        self.queue: deque[PendingQuestion] = deque()
        self._app: Application | None = None

    @property
    def active(self) -> PendingQuestion | None:
        return self.queue[0] if self.queue else None

    @property
    def has_pending(self) -> bool:       # ConditionalContainer filter
        return bool(self.queue)

    def push(self, pq: PendingQuestion) -> None:
        """on_ask_user → push. 패널 즉시 갱신."""
        self.queue.append(pq)
        if self._app is not None:
            self._app.invalidate()

    def pop(self) -> PendingQuestion | None:
        pq = self.queue.popleft() if self.queue else None
        if self._app is not None:
            self._app.invalidate()
        return pq

    def skip(self) -> PendingQuestion | None:
        """/skip — 현재 질문 dismiss → 다음 질문 노출."""
        return self.pop()

    def reset(self) -> None:
        self.queue.clear()

    def control(self) -> FormattedTextControl:
        return FormattedTextControl(text=self.render)

    def render(self) -> FormattedText:
        pq = self.active
        if pq is None:
            return FormattedText("")
        lines = [f"❓ {pq.agent_id}: {pq.question}"]
        if pq.options:
            lines.append("  " + "   ".join(f"[{i+1}] {opt}" for i, opt in enumerate(pq.options)))
        if len(self.queue) > 1:
            lines.append(f"  (대기 {len(self.queue) - 1}개)")
        return FormattedText(HTML("<br/>".join(lines)))
```

- **FIFO (도착 순서)** — P3: HITL에선 도착 순서 응답이 자연스러움 (hermes는 최신 우선
  이지만, 다중 에이전트가 연속으로 질문할 때 밀리면 안 됨).
- `_pending_question` 단일 필드 → `queue`로 통일 (호환 shim: `@property
  _pending_question` → `self.queue[0] if self.queue else None`).

### 4.3 질문 메타데이터 (v2.0 — tools.py 스키마 확장 후보)

```python
# tools.py ask_user 스키마에 선택 필드 추가 (v2.0)
"timeout": {"type": "integer"},        # 초 단위, 미응답 시
"default": {"type": "string"},         # 기본 응답 (timeout 시 전송)
```

- 미응답 타임아웃 → `default` 옵션 자동 전송 + 로그에 "(자동 응답)" 표시.
- v1.0에서는 미구현 — 에이전트가 응답을 기다리되 fire-and-forget 원칙 유지.

---

## 5. 키 바인딩 & IME (hermes `pt_input_extras` 이식)

### 5.1 hermes 공식 TUI 키맵 (ui-tui/README.md — 팀 논쟁 종결 근거)

```
Enter              → Submit the current draft
Shift+Enter/Alt+Enter → Insert a newline
\ + Enter          → Append line to multiline buffer (fallback)
Ctrl+C             → Interrupt active run, or clear draft, or exit if nothing pending
Ctrl+D             → Exit
Up/Down            → edit queued messages first, then walk input history
```

- **Enter=제출, Shift+Enter=개행이 hermes 표준 UX** → 우리 기본안(agent-4 제안)과 일치.
- ask_user 선택지도 hermes clarify prompt(질문+선택지)와 동형:
  - `Up/Down`+`Enter` 선택 이동/확정 · **숫자 1-9 퀵픽** (우리 번호 응답 동일) ·
    `Enter on "Other"` 자유 텍스트 (우리 "숫자 아니면 원문 회신" 규칙이 이미 처리).
- **REPL 기본화와의 정합 (v0.5):** hermes 공식 키맵의 `Ctrl+C 3단계`에서 "exit if
  nothing pending"은 우리 REPL에서도 동일 — 드래프트가 비어 있고 실행 중인 세션이
  없을 때만 Ctrl+C가 종료로 승격 (v2.0 검토). v1.0은 드래프트 클리어로 고정.

### 5.2 신규 모듈: `tui/key_aliases.py`

hermes의 `pt_input_extras.py`에서 아래 4가지를 이식 (축소):

| 함수 | 역할 | 필요성 |
|------|------|--------|
| `install_shift_enter_alias()` | Shift+Enter → Alt+Enter 바이트 매핑 | 멀티라인 개행 |
| `install_ctrl_enter_alias()` | Ctrl+Enter → Alt+Enter | 멀티라인 개행 |
| `install_modify_other_keys_aliases()` | Kitty CSI-u / modifyOtherKeys 정상화 (Ctrl/Alt/Shift+letter) | 한글 IME + 조합키 안정 |
| `install_ignored_terminal_sequences()` | `ESC[I`/`ESC[O` → Keys.Ignore | 터미널 포커스 전환 버퍼 오염 방지 |

```python
# tui/key_aliases.py — 골격
def install_tui_key_aliases() -> int:
    """모든 키 별칭을 1회 설치. 실패해도 무해. 반환: 변경된 시퀀스 수."""
    total = 0
    for installer in (install_shift_enter_alias, install_ctrl_enter_alias,
                      install_modify_other_keys_aliases, install_ignored_terminal_sequences):
        try:
            total += installer()
        except Exception:
            pass
    return total
```

- 각 함수는 `ANSI_SEQUENCES`에 `setdefault`로 매핑 추가 (기존 사용자 매핑 보존).
- **import 시 1회 호출** — `tui/__init__.py` 또는 `app.py`에서.
- 테스트: `ANSI_SEQUENCES` 상태를 검증하는 순수 단위 테스트.

### 5.3 입력줄 정책 (팀 합의 — P2 확정, hermes 표준 근거)

**단일 시각의 채팅 입력: `TextArea(multiline=True)` + `accept_handler`(Enter=제출) +
Shift/Esc+Enter=개행**

- ⚠️ 함정 (agent-2 검증): `TextArea(multiline=True)`는 내부적으로 자체 키바인딩을
  구성하고 `accept_handler`로 Enter를 처리한다. `multiline=True`면 기본 Enter =
  newline. 커스텀 `KeyBindings`를 넘겨도 내부 accept 처리가 우선순위에서 겹칠 수
  있어, 단순 `@kb.add("enter")`만으론 예측 불가 동작이 나올 수 있다.
- 해결: **`accept_handler` 활용** — TextArea 위젯 그대로 (스타일/프롬프트/높이 유지):
  ```python
  def _accept(buff: Buffer) -> bool:
      text = buff.text
      buff.reset()                          # 입력줄 비움
      app.create_task(router.route(text))   # 비동기 전달 (fire-and-forget)
      return True                           # True = accept (Enter가 버퍼에 남지 않음)

  self.input_area = TextArea(
      multiline=True,
      accept_handler=_accept,               # Enter = 제출 (multiline과 무관하게)
      key_bindings=kb,                      # Shift/Esc+Enter 개행, Ctrl+D/C만 커스텀
  )
  ```
- `accept_handler`는 **동기 함수**여야 하므로 비동기 전달(router.route가 await를
  씀)은 `app.create_task(...)` 또는 내부 큐(별도 태스크가 소비)로.
- Shift+Enter 개행은 `key_aliases.py`의 `install_shift_enter_alias`로 시퀀스
  정규화가 전제.
- **Windows IME**: prompt_toolkit win32 네이티브 입력이 IME 조합을 앱 레벨 처리.
  조합 확정용 Enter는 버퍼에 안 들어가고 확정 후 별도 Enter로 전달.
  Windows Terminal에서 실측 검증을 테스트 항목으로 명시.

### 5.4 키 바인딩 표 (최종 — agent-2, P12 반영)

| 키 | 동작 | 구현 |
|----|------|------|
| Enter | 제출 | `TextArea.accept_handler` (multiline=True 유지) |
| Shift+Enter / Esc+Enter | 개행 | kb → `buff.insert_text("\n")` (기존 Esc+Enter 호환) |
| Ctrl+D | **REPL 루프 종료 (세션 정리 + 프로그램 종료)** | kb → `app.exit()` |
| Ctrl+C | 입력 드래프트 클리어 (세션은 계속) | kb → `buff.reset()` — v1.0 포함 (hermes 3단계 중 2단계) |
| ↑/↓ | 입력 히스토리 | FileHistory 기본 (TextArea에 history 연결) |
| Tab | (v2.0) 슬래시 명령 자동완성 | Completer |

> **v0.5 — Ctrl+D 의미 통일 (REPL 기본화):** 기존 정의 "입력 루프 종료 (세션은 계속)"은
> `/quit`과 동일하게 **REPL 루프 종료 → 세션 정리 → 프로그램 종료**로 재정의.
> Ctrl+D와 `/quit`은 동일 종료 경로 (`app.exit()` → cli finally 정리).

- hermes 공식 키맵(ui-tui/README.md): `Enter=Submit` / `Shift+Enter=newline` /
  `Ctrl+C` 3단계(인터럽트/클리어/종료) / `Ctrl+D=Exit` — 우리 기본안과 일치.
- Ctrl+C를 "드래프트 클리어"로 매핑 (v1.0) — 세션을 죽이지 않으면서 사용자가
  실수로 긴 입력을 지울 수 있는 안전망. `app.exit()` 아님.
- v2.0에서 Ctrl+C 3단계(인터럽트/클리어/종료) 검토.

---

## 6. 상태 표시줄 (Status Bar) — v1.0 포함 (agent-2 제안 + agent-1/4 동의)

### 6.1 표시 내용

```
threads=3 · msgs=12 · gate=OPEN · phase=P3_EXECUTE · agents=2
```

- `threads` / `msgs` — `session.server.snapshot()` 기반 (스레드/메시지 수)
- `gate` — `session.gate.is_open` → OPEN/CLOSED (없으면 n/a)
- `phase` — `session.protocol.phase` (없으면 n/a)
- `agents` — 에이전트 수 (+ 활성 여부)
- 너무 길면 축약 (예: `thr=3 msgs=12 gate=OPEN ph=P3 ag=2`)

### 6.2 갱신 방식 (agent-1 제안 — 하이브리드)

- **이벤트 기반**: `log_buffer.append` / `on_ask_user` / `on_tool_event` 시
  상태바 `invalidate()` → 스레드·게이트·페이즈 즉시 반영.
- **주기 보정**: 1초 타이머로 `session.server.snapshot()` 폴링 → 이벤트 없이
  바뀌는 상태(다른 에이전트의 스레드 생성 등)도 따라잡음.
- 상태바는 `FormattedTextControl` 1개라 성능 영향 없음 (agent-2 확인).

### 6.3 Layout 반영 (agent-2 최종 코드 — 4분할)

```python
# tui/app.py — SessionTUIApplication 레이아웃 (최종)
from prompt_toolkit.application import Application
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea

class SessionTUIApplication:
    def _build_layout(self):
        self.status_bar = StatusBar(self.session)   # 상태바 (6.4)
        log_window = Window(self.log_buffer.control(), wrap_lines=True)
        choice_window = ConditionalContainer(
            Window(self.choice_panel.control(), height=3, style="bg:#333333"),
            filter=self.choice_panel.has_pending,      # 대기 질문 없으면 영역 소멸
        )
        self.input_area = TextArea(multiline=True, prompt="👤 > ", height=3,
                                   accept_handler=_accept, key_bindings=kb)
        return Layout(HSplit([
            log_window,                               # ① 로그 (1fr)
            choice_window,                            # ② 선택지 (조건부 3줄)
            Window(self.status_bar.control(), height=1, style="bg:#222222"),  # ③ 상태바
            Window(self.input_area, height=3),        # ④ 입력 (하단 고정)
        ]))
```

### 6.4 StatusBar 구현 (agent-2 최종 원고)

```python
# tui/status_bar.py
class StatusBar:
    def __init__(self, session, *, refresh_interval=1.0):
        self._session = session
        self._text = "agents=…"
        self._formatted: FormattedText | None = None

    def _snapshot_line(self) -> str:
        snap = self._session.server.snapshot()
        gate = self._session.gate
        phase = self._session.protocol.phase if self._session.protocol else "n/a"
        return (
            f"threads={len(snap['threads'])} · msgs={len(snap['messages'])} · "
            f"gate={'OPEN' if gate and gate.is_open else ('CLOSED' if gate else 'n/a')} · "
            f"phase={phase} · agents={len(snap['agents'])}"
        )

    async def run(self, app):            # 1초 주기 보정 태스크 (session.run과 병렬)
        while True:
            await asyncio.sleep(refresh_interval)
            self.invalidate()            # 이벤트 기반 갱신 + 주기 보정
            app.invalidate()

    def invalidate(self):                # 이벤트 발생 시 즉시 갱신 (on_step/on_tool_event/on_ask_user에서 호출)
        self._text = self._snapshot_line()
        self._formatted = None

    def control(self) -> FormattedTextControl:
        return FormattedTextControl(text=self._snapshot_line)
```

---

## 7. 상태 모델 (HumanTUIAdapter 확장)

```python
@dataclass
class PendingQuestion:
    thread_id: str
    agent_id: str
    question: str
    options: list[str]
    created_at: float
    status: Literal["pending", "answered", "dismissed"]
    # v2.0: timeout: float | None, default: str | None

class HumanTUIAdapter:
    # 기존 유지
    _recent_thread: str | None
    _pending_queue: deque[PendingQuestion]   # _pending_question → queue[0] 통일 (shim)
    _history_path: Path
    # 신규 (full-screen, tui/ 위임)
    _tui: SessionTUIApplication | None
    _router: RouterContext | None
```

- 호환 shim: `_pending_question` property, `_resolve_option`, `_deliver` —
  기존 `test_human_tui.py`가 쓰는 API는 유지 (내부 구현만 변경).

> **v0.5 — REPL 기본화 반영:** REPL 루프가 유일 실행 모드이므로,
> - 기존 `_run`(1회 실행) 경로에 있던 **TUI 모드 설정**(`human_cfg`:
>   response_format/input_prompt/multiline/pin_options/choice_queue)과
>   **`tui_adapter` 생성/해제**(`_make_tui_adapter`, `_human_input_loop_tui`)가
>   REPL 루프 생성 시점으로 흡수된다.
> - `RouterContext`는 **REPL 루프 전체 수명**을 커버한다 — 라우팅 상태
>   (active_question, recent_thread, snapshot)가 루프 동안 유지/갱신된다.
> - 세션 종료는 `/quit`/Ctrl+D → `app.exit()` → cli finally에서
>   `tui_app.shutdown()` → `session.close()` 순서로 정리된다 (§3.2/§5.4와 정합).

---

## 8. 테스트 계획

### 8.1 단위 (헤드리스, 순수 함수 — PipeInput 불필요)

- `test_tui_router.py` — `route()` 판정 순서
  - 질문 활성 + "2" → choice (옵션 치환, thread/mentions 정확)
  - 질문 활성 + "아무말" → question_reply (질문 thread, mentions=[에이전트], notice)
  - 질문 비활성 + "3" → plain (최근 스레드, mentions=None)
  - "/quit" → kind="quit" / "/status x" → kind="command", args="x"
  - **평문 "quit"/"exit" → plain (v0.5.1 — 평문 종료 폐기 회귀 방지)**
  - **빈 입력/공백 → ignored (v0.5 — "빈 줄=quit" 구동작과의 차이 회귀 방지)**
- `test_tui_commands.py` — 레지스트리/디스패치
  - `/help`, `/status`, `/threads`, `/skip` 출력 검증 (fake ctx)
  - 미지의 명령 → 안내 문구
  - 핸들러 예외 → "실행 실패" 문구
- `test_tui_key_aliases.py` — `install_*` 후 `ANSI_SEQUENCES` 매핑 검증
- `test_tui_status_bar.py` — 상태바 텍스트 포맷 (snapshot fake 주입)

### 8.2 통합 (Session + fake backend)

- **REPL 기본 모드 (v0.5):** `test_repl.py` 재작성 — "REPL = 같은 Application +
  세션 재사용 루프"가 **기본/유일 실행 모드**. 첫 실행(initial prompt) → 다음 질문
  루프 → `/quit`/Ctrl+D 종료 → 세션 정리(shutdown) 검증. `--repl` 플래그 분기
  테스트는 **삭제** (플래그 자체가 사라짐).
- **빈 입력 Enter → ignored** (v0.5 — 세션 종료로 이어지지 않음을 검증)
- **평문 "quit"/"exit" → 일반 메시지 전송** (v0.5.1 — 세션 종료로 이어지지 않음을 검증)
- 활성 질문 중 일반 텍스트 → **질문 스레드+에이전트**로 회신 (P1 검증)
- `/quit` 입력 → REPL 루프 종료 + 세션 정리
- 다중 질문 큐 FIFO 순차 응답
- ask_user → 패널 고정 → "2" 입력 → human_send(thread, options[1], mentions=[agent]) → [radio] 흡수
- 전송 피드백: `✓ human → ...` 로그 출력 검증
- `input_bar`: accept_handler 호출/버퍼 리셋 검증 (agent-1 추가)

### 8.3 회귀

- 기존 `test_human_tui.py`의 `_deliver`/`_resolve_option` 시그니처 유지
  (호환 shim으로 통과 유지).
- `test_repl.py` — "REPL 유일 모드" 기준 재작성 (기존 "`--repl` vs 1회" 분기
  테스트와 평문 quit/exit 종료 테스트는 폐기 — 1회 경로·평문 종료 삭제됨).

---

## 9. 열린 결정 (Open Decisions)

| # | 항목 | 제안 | 상태 |
|---|------|------|------|
| P1 | 활성 질문 중 일반 텍스트 라우팅 | 무조건 질문 스레드+에이전트 회신 (+안내 1줄) | ✅ 확정 |
| P2 | 입력줄 정책 | multiline=True, Enter=제출(accept_handler), Shift/Esc+Enter=개행 (hermes 표준) | ✅ 확정 |
| P3 | 다중 질문 순서 | FIFO (도착 순서) | ⏳ 최종 확인 |
| P4 | 슬래시 명령 v1.0 범위 | 5종+quit (/help /status /threads /clear /skip) | ⏳ 최종 확인 |
| P5 | `/broadcast`, 자동완성 | v2.0 | ✅ 보류 |
| P6 | 상태바 v1.0 포함 | [로그/선택지/상태바/입력] 4분할, 하이브리드 갱신 | ✅ 확정 (agent-1/2/4) |
| P7 | 전송 피드백 | `✓ human → ...` 로그 1줄 | ✅ v1.0 필수 (agent-2) |
| P8 | Ctrl+C 동작 | 드래프트 클리어 (v1.0), 3단계는 v2.0 | ✅ 확정 (agent-2) |
| **P9** | **빈 입력 의미 (REPL 기본화)** | **무시(ignored)** — 기존 "빈 줄=quit" 폐기. 빈 Enter로 세션 종료 방지 (Enter=제출 UX와 결합) | ✅ 확정 (v0.5) |
| **P10** | **`/quit`·Ctrl+D 의미 재정의 (REPL 기본화)** | REPL 유일 모드 → **루프 종료 + 세션 정리 + 프로그램 종료**. 백그라운드 유지는 v2.0 | ✅ 확정 (v0.5) |
| **P11** | **평문 quit/exit 처리 (REPL 기본화)** | **일반 메시지(plain)** — 평문 종료 폐기. 종료는 `/quit`·`/exit`·Ctrl+D·EOF 4가지만 | ✅ 확정 (v0.5.1, agent-1 제안 승인) |

---

## 10. 변경 파일 요약 (담당분)

```
src/agent_augury/
  tui/router.py              # 신규: 입력 라우팅 (순수 함수) — route()/RouteResult/_resolve_option
  tui/commands.py            # 신규: 슬래시 명령 레지스트리 (순수 함수)
  tui/choice_panel.py        # 신규: PendingQuestion 큐 + 패널 렌더러 (FIFO)
  tui/status_bar.py          # 신규: 상태바 텍스트 포맷 (순수 함수) + 하이브리드 갱신
  tui/key_aliases.py         # 신규: 키 시퀀스 별칭 (hermes 이식)
  channel/human_tui.py       # 수정: 라우팅/큐/패널을 tui/ 위임 + 호환 shim 유지
  cli.py                     # 수정(v0.5): --repl 플래그·1회 실행(_run) 경로 제거.
                             #   _run_repl → REPL 루프(유일 모드)로 승격 + TUI 설정 흡수.
tests/
  test_tui_router.py         # 신규
  test_tui_commands.py       # 신규
  test_tui_key_aliases.py    # 신규
  test_tui_status_bar.py     # 신규
  test_human_tui.py          # 수정 (호환 shim 검증 추가)
  test_repl.py               # 재작성 (REPL=유일 모드 기준, --repl 분기·평문 quit/exit 종료 테스트 삭제)
```
