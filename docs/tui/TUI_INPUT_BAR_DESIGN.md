# agent-augury — 상시 입력창(TUI) 도입 설계 및 라이브러리 조사

> **Task:** agent-augury에 상시 입력창(TUI) 도입 — 사용자가 inbox에 언제든 메시지를 넣을 수 있게 하고, 에이전트가 부여한 선택지(ask_user)가 에이전트 대화에 밀려 흘러가 사용자가 못 보지 않게 고정(pin)하기.
> **Date:** 2026-09
> **Author:** agent-4 (textual 심층 조사 + prompt_toolkit trade-off + 구현 방향) · 팀 협업: agent-1(통합/검증), agent-2(prompt_toolkit), agent-3(문서 통합)
> **Scope:** 설계 문서 (구현 코드 아님). 기존 HITL 인프라(`server.register_human/human_send`, `ask_user` 도구, `--interactive`)를 전제로, **표시/입력 계층(HumanAdapter)만 교체**하는 방향.
> **참조:** `USER_INTERVENTION_DESIGN.md` §4.5 HumanAdapter · `LIBRARY_RESEARCH_prompt_toolkit.md` (agent-2) · `LIBRARY_RESEARCH_textual.md` (agent-4) · `TUI_INPUT_DESIGN.md` (agent-2 통합 초안) · **최종 합의 (D8 + 안A + 로그 억제 — agent-1/agent-3/agent-4 교차 검증 완료)**

---

## 1. 문제 정의

### 1.1 현재 상태 (코드 근거)

현재 HITL(USER_INTERVENTION_DESIGN.md v1.1)은 이미 구현되어 있다:

| 컴포넌트 | 위치 | 역할 |
|----------|------|------|
| `register_human()` / `human_send()` | `server.py` | `human` 참가자 등록 + 사용자 메시지 주입 (에이전트 inbox push) |
| `ask_user` 도구 | `agent/tools.py` | 에이전트가 사용자에게 질문/선택지 전달 (`mentions=["human"]`) |
| `--interactive` 모드 | `cli.py` `_human_input_loop` | `input()`을 `run_in_executor`로 돌려 세션 도중 입력 수용 |
| 출력 파이프라인 | `Session._output_queue → _output_consumer → cli._log_tool_event` | 에이전트 step/도구/메시지 로그 실시간 출력 (rich) |
| ask_user 로그 분기 | `cli._log_tool_event` `if tool == "ask_user":` | 질문/선택지를 args에서 읽어 `👤 ... asks:` 출력 (이미 구현) |
| ask_user tool 이벤트 | `session.py from_config` `on_tool_call` lambda | `_emit_event({"type":"tool","tool":"ask_user","args":{question,options,thread},...})` (이미 구현) |

### 1.2 핵심 결함 3가지

1. **입력 프롬프트가 로그에 밀림** — `input()`은 단일 라인 에디터로, 프롬프트가 항상 로그 스트림 맨 아래에 붙는다. 에이전트 출력이 동시에 흐르면 프롬프트가 섞여 사라지고, 사용자가 입력 중인 줄도 출력에 의해 덮일 수 있다.
2. **ask_user 선택지가 대화에 흘러감** — `ask_user`는 `send_message`로 구현되어 있어 질문/선택지가 일반 로그처럼 출력되고 스크롤로 밀려 사라진다. 현재는 send_message 로그(`💬 [agent-1 → human] [ask-user] ...`)와 tool 로그(`👤 agent-1 asks:`)가 **둘 다 출력되는 중복**까지 존재한다. 다행히 **구조적 `tool` 이벤트(`tool=="ask_user"`, args에 question/options/thread)는 이미 존재**한다 (D8 합의, §3.2).
3. **UX 부족** — 멀티라인 붙여넣기, 히스토리/위/아래 화살표, 한글 IME 조합, 선택지 번호 선택 등이 없다.

### 1.3 요구사항 정리

| ID | 요구사항 | 우선순위 |
|----|----------|:---:|
| R1 | 세션 도중 **언제든** 입력 가능한 상시 입력창 (하단 고정) | P0 |
| R2 | ask_user 질문/선택지가 **로그 스크롤에 밀려 사라지지 않게 고정**(pinned 패널) | P0 |
| R3 | 선택지 번호/키 입력으로 빠르게 응답 | P0 |
| R4 | 에이전트 로그는 계속 실시간 스크롤 (기존 rich 출력 유지) | P0 |
| R5 | 멀티라인 입력/붙여넣기, 히스토리, 한글 IME | P1 |
| R6 | 기존 `--interactive`/비인터랙티브 모드와 **후방 호환** (옵트인) | P1 |
| R7 | Windows(사용자 환경) + macOS/Linux 호환 | P1 |

---

## 2. 라이브러리 조사

### 2.1 후보 비교 요약

| 라이브러리 | 모델 | 로그와 공존 | 편집 가능 입력 | 선택지 패널 | asyncio 통합 | Windows | 추천 |
|------------|------|:---:|:---:|:---:|:---:|:---:|:---:|
| **prompt_toolkit 3.x** | 인라인 프롬프트 + 툴바 / full-screen | ✅ (인라인) | ✅ TextArea(멀티라인/히스토리/완성) | ✅ `bottom_toolbar` 고정 | ✅ `prompt_async()` / `Application.run_async()` | ✅ (win32+colorama, ConHost도 동작) | ★★★★★ |
| **textual 1.x/2.x** | full-screen TUI | ⚠️ (전체 화면 전환) | ✅ Input/TextArea | ✅ OptionList + CSS | ✅ 네이티브 asyncio | ⚠️ ConPTY 필수 (Windows Terminal) | ★★★★☆ (전면 개편 시) |
| **rich-only** (Layout+Live) | 인라인/전체 | ✅ | ❌ (편집 불가) | ⚠️ (표시만) | ✅ | ✅ | ★★☆☆☆ |
| **urwid** | full-screen TUI | ❌ | ✅ | ✅ | ⚠️ (튜닝 필요) | ⚠️ | ★★☆☆☆ |
| **npyscreen / PyInquirer** | full-screen / form | ❌ | ✅ | ✅ | ❌ (동기 중심) | ⚠️ | ★☆☆☆☆ |
| **ink(JS)** | — | — | — | — | — | — | ❌ (Python 프로젝트와 이질) |

> rich 15.0.0은 **이미 설치**되어 있고 `cli.py`가 사용 중이다. prompt_toolkit / textual은 **미설치** (추가 의존성 결정 필요).

### 2.2 prompt_toolkit 상세 (agent-2 담당, 요약 — 상세는 `LIBRARY_RESEARCH_prompt_toolkit.md`)

- **`PromptSession` (권장)** — 단발 `prompt()` 대신 세션 유지: `history=FileHistory(...)`, `auto_suggest=AutoSuggestFromHistory()`, `multiline=True`, `enable_open_in_editor=True`.
- **`prompt_async()`** — asyncio first-class. 내부적으로 이벤트 루프와 통합되어 **별도 스레드 불필요** → 기존 `_human_input_loop`의 `run_in_executor(input())`을 제거 가능.
- **`patch_stdout()`** — rich `Console` 출력과 공존. 출력 시점마다 프롬프트 재렌더링 → **고빈도 로그 스트리밍 시 깜빡임(flicker) 주의** (agent-1/agent-2 동일 지적). 고정이 필요한 선택지는 patch_stdout만으로 부족 → `bottom_toolbar` 사용.
- **full-screen `Application` + `Layout`** — `HSplit([로그 Window, 입력 TextArea])` 구조로 **선택지 고정의 정답**이 될 수 있음. `FormattedTextControl`에 rich 스타일 토큰/HTML/ANSI 삽입 가능. 단, rich 콘솔 로그를 전부 버퍼로 재라우팅해야 함 (큰 변경).
- **`PipeInput`** — 테스트/자동화에서 키 입력 시뮬레이션 (E2E 테스트 핵심).
- **Windows** — win32 API + colorama 기반, **ConHost에서도 네이티브 콘솔 API로 동작** (textual보다 Windows 호환 폭이 넓음).
- **한글 IME** — Windows IME 조합을 네이티브로 처리. `input()`보다 안정적.

**선택지(ask_user) 고정 3레벨 (agent-2):**

| 레벨 | 방식 | 장점 | 단점 |
|------|------|------|------|
| A | 인라인 + patch_stdout | 기존 rich 파이프라인 거의 유지 | 스크롤되면 사라짐 — '고정' 아님 |
| B | **하단 고정 프롬프트 + bottom toolbar** | 프롬프트는 항상 하단 고정, 로그는 위로 흐름, 변경 최소 | 선택지가 toolbar에 들어가야 함 (공간 제약) |
| C | full-screen Layout (위=로그/중간=선택지/아래=입력) | 완전한 고정, 반응형 | rich 로그 출력을 앱 렌더링으로 이관 (큰 변경) |

→ **권장: B를 최소 변경 경로(v1.0)로, C를 v1.1+ 옵션**으로.

### 2.3 textual 상세 (본 문서 — agent-4 담당)

#### 2.3.1 기본 정보

- Textualize(Will McGugan, rich 저자)의 **full-screen TUI 프레임워크**. 내부 렌더링이 rich 기반 → 기존 rich 스타일/컬러와 자연 호환.
- Python 3.9+ (최신 1.x/2.x 기준), `pip install textual`. `textual[dev]`에 테스트 러너(`run_test` pilot) 포함.
- **네이티브 asyncio**: `App.run_async()`(비동기 진입점), `@work` 데코레이터 + `run_worker()`(백그라운드 태스크), `async with app.run_test() as pilot`(헤드리스 테스트).

#### 2.3.2 상시 입력창 관점의 핵심 위젯

| 위젯 | 용도 | 상시 입력창 요구와의 대응 |
|------|------|--------------------------|
| `Input` | 단일 라인 텍스트 입력 | 하단 입력창 기본. `placeholder`, `Input.Submitted` 이벤트 |
| `TextArea` | 멀티라인 편집 | R5 멀티라인/붙여넣기. `TextArea.Submitted`(Ctrl+Enter) |
| `RichLog` | 로그 스트리밍 표시 | 기존 `_output_queue` 이벤트를 `RichLog.write()`로 이식. `max_lines`로 스크롤백 제한 |
| `OptionList` | 선택지 목록 + 선택 이벤트 | **R2/R3 — ask_user pinned 패널로 정확히 부합**. `on_option_list_option_selected` |
| `Static` / `Markdown` | 고정 텍스트/마크다운 | 질문 문구 표시 |
| `VerticalScroll` / `ScrollView` | 스크롤 컨테이너 | 로그 영역 |

#### 2.3.3 레이아웃 (textual CSS)

```css
Screen {
    layout: vertical;
}

#log {            /* 상단: 에이전트 로그 (RichLog) */
    height: 1fr;
    border: round $primary;
}

#ask-panel {      /* 중간: pinned 선택지 패널 — ask_user 있을 때만 표시 */
    height: auto;
    max-height: 10;
    display: none;
    border: round $warning;
}

#input-bar {      /* 하단: 상시 입력창 (Input/TextArea) */
    height: 3;
    dock: bottom;
    border: round $accent;
}
```

- `dock: bottom` + `height` 고정으로 **입력창이 항상 하단 고정** (R1).
- ask_user 도착 시 `#ask-panel`을 `display: block`으로 전환, 응답 후 `display: none` (R2).
- `@media (max-width: 80)` breakpoints로 좁은 터미널 대응.

#### 2.3.4 reactive + 이벤트

```python
class AuguryApp(App):
    pending_question: reactive[str | None] = reactive(None)
    pending_options: reactive[list[str]] = reactive([])

    @watch("pending_question")
    def _on_question(self) -> None:
        # ask_user 도착 → 패널 갱신 + 입력창 포커스 유지
        self.query_one("#ask-panel", Static).update(self.pending_question)
```

- reactive 상태 변경 시 `@watch`로 자동 UI 갱신. 서버 이벤트 → App 상태 → 위젯 반영 구조가 자연스럽다.

#### 2.3.5 Windows 호환

- **Windows 10 1809+의 ConPTY 필수.** Windows Terminal / VS Code 통합 터미널 / JetBrains 터미널은 ConPTY 기반이라 정상. **구형 cmd.exe(ConHost)는 full-screen TUI가 불안정**할 수 있음 (prompt_toolkit은 ConHost도 네이티브 지원 — textual이 더 엄격).
- 한글 IME: Windows Terminal + ConPTY 환경에서 입력/조합 지원 (터미널 의존적).
- 사용자 환경(Windows, `C:\Users\test`) 기준으로는 Windows Terminal 사용을 전제로 안내 필요.

#### 2.3.6 textual 도입 시의 침습 범위 (중요)

- textual은 **full-screen(alternate screen buffer)** 모델: 앱 시작 시 화면 전체를 교체하고, 종료 시 원래 화면 복원.
- **기존 rich 인라인 로그 출력(`_console.print`)과 공존 불가** — textual 실행 중에는 print 기반 출력이 안 보이거나 깨진다.
- 즉 textual 채택 = **CLI를 "채팅 앱 스타일 TUI"로 전면 개편**하는 결정. 로그/도구 이벤트를 전부 `RichLog` 위젯으로 이식해야 한다 (`_log_tool_event` 로직 재작성).
- 장점: 선택지/입력/로그/상태를 **하나의 위젯 트리로 통합 관리** → R2(고정 패널)가 선언적 CSS로 해결. 단점: 러닝 커브(CSS/reactive 패러다임) + 기존 출력 파이프라인 이식 비용.

### 2.4 rich-only (Layout + Live + Prompt) 검토

- 의존성 추가 **0** (rich 15.0.0 설치됨). `rich.layout.Layout` + `rich.live.Live`로 상단 로그/하단 입력 영역을 인라인 갱신.
- 그러나 **편집 가능한 입력창 구현 불가** — rich는 출력 전용. 커서 이동/히스토리/IME/멀티라인을 직접 구현해야 하며 사실상 비현실적.
- 결론: **단독 채택 부적합.** 다만 "rich + prompt_toolkit" 조합에서 로그 영역은 기존 rich를 그대로 쓰는 것이 자연스럽다.

### 2.5 trade-off 종합

| 관점 | prompt_toolkit (1안) | textual (2안) |
|------|----------------------|----------------|
| 침습 범위 | **작음** — 기존 rich 로그 유지, 입력/패널만 추가 | 큼 — 전체 출력 파이프라인 TUI 이식 |
| R2(선택지 고정) | `bottom_toolbar`(B레벨) / full-screen Layout(C레벨) | CSS 패널로 해결 (선언적, 더 자연스러움) |
| R1(상시 입력) | `prompt_async()` 루프 (`PromptSession`) | `Input` 위젯 |
| 기존 코드 변경 | `loop.py` 1줄 + `cli.py`/`config.py` 소량 + 신규 모듈 1개 | `cli.py` 대부분 + 출력 이벤트 포맷 |
| 러닝 커브 | 낮음 (프롬프트 API) | 중간 (CSS/reactive) |
| UI 표현력 | 제한적 (인라인+툴바) → full-screen으로 확장 가능 | 높음 (full-screen 위젯) |
| Windows | ✅ win32+colorama (ConHost도 동작) | ⚠️ ConPTY 필수 (더 엄격) |
| 테스트 | `PipeInput`으로 입력 주입 | `run_test` pilot 내장 (헤드리스 테스트 강점) |
| 로그 공존 | ✅ patch_stdout (깜빡임 주의) | ❌ full-screen 전환 필요 |

**결론:** 요구사항(R1~R7)의 본질은 **"기존 로그 흐름을 유지하면서 입력창 + 고정 선택지 패널을 얹는 것"**이다. 침습 범위가 작고 기존 rich 파이프라인과 공존하며 Windows 호환 폭이 넓은 **prompt_toolkit을 1안**으로, textual은 **"전면 개편형 채팅 TUI"**가 필요해질 때(후속) 2안으로 제안한다. (4개 트랙 합의)

---

## 3. 구현 방향 설계

### 3.1 아키텍처 개요

```
                        ┌───────────────────────────────────────┐
                        │            사용자 (human)             │
                        │  터미널: 상단 로그 / 중간 선택지 패널   │
                        │         / 하단 상시 입력창             │
                        └──────────────────┬────────────────────┘
                                           │ prompt_async() / human_send()
                                           ▼
   ┌─────────────────────────────────────────────────────────────┐
   │              MessageServer (SSOT)  [변경 없음]               │
   │  register_human / human_send / ask_user(mentions=[human])   │
   └───────────────┬──────────────────────────┬─────────────────┘
                   │                          │
        ┌──────────▼──────────┐    ┌──────────▼──────────┐
        │   AgentLoop (N개)    │    │   HumanAdapter       │
        │  step() inbox drain  │    │  (신규: TUI 계층)     │
        │  ask_user 도구 호출   │    │  · 입력 태스크        │
        └─────────────────────┘    │  · 선택지 pinned 패널  │
                                   │  · 로그 출력 유지      │
                                   └─────────────────────┘
```

### 3.2 ask_user 식별 + thread ref 해석 — **최종 합의 (D8 + 안A)**

**핵심 발견 (agent-1/agent-3 교차 검증):** `ask_user`는 `tools.py`에서 `server.send_message(mentions=["human"])`으로 구현되어 **메시지 레벨에선 별도 이벤트 타입이 아니다**. 그러나 **구조적 `tool` 이벤트는 이미 존재**한다:

```python
# session.py from_config (이미 구현 — 변경 없음)
on_tool_call=lambda agent_id, tool, args, result, _server=server: (
    _server._emit_event({
        "type": "tool",
        "agent_id": agent_id,
        "tool": tool,          # ← "ask_user"
        "args": args,          # ← {question, options, thread} 구조적 데이터
        "result": result,
        "timestamp": ...,
    })
)
```

- `loop.py step()`은 `self.on_tool_call(self.agent_id, call.name, call.arguments, result)`로 **모든 도구 호출**에 대해 이 이벤트를 발생시킨다.
- `cli._log_tool_event`에도 `if tool == "ask_user":` 분기가 **이미 존재** (question/options를 args에서 읽음).

#### ⚠️ v1.0 블로커: thread ref (agent-1 검증 확정)

`loop.py step()` 실제 코드:
```python
args = self._resolve_refs(call.arguments)          # ① resolve된 args
result = await self._execute_tool(call.name, args) # ② 실행은 resolve된 값으로
...
self.on_tool_call(self.agent_id, call.name, call.arguments, result)  # ③ ★ resolve 전 원본!
```

- `on_tool_call`(→ session.py lambda → `type:"tool"` 이벤트 → cli)에는 **resolve 전 `call.arguments`** 가 전달된다.
- 에이전트가 `"thread": "$thread:0"`을 쓰면 cli는 `$thread:0`을 받는다. 이걸 그대로 `session.human_send(thread_id="$thread:0", ...)`에 넘기면 **`KeyError: no such thread: $thread:0`** — v1.0 E2E 실패.

**해결: 안 A (권장, v1.0 필수)** — `loop.py` 1줄 변경:
```python
# loop.py — 기존
self.on_tool_call(self.agent_id, call.name, call.arguments, result)
# 변경
self.on_tool_call(self.agent_id, call.name, args, result)   # resolve된 args
```
- `args`는 이미 `_resolve_refs`를 거친 값 — `$thread:N` → 실제 `thread-<n>`, `$thread_by_name:` → 실제 id.
- session.py lambda는 파라미터명 `args` 그대로 — **시그니처 불변, 변경 0**.
- cli `_log_tool_event`의 기존 tool 로그(`path` 등)도 resolve 후 값으로 일관 — 파일 도구는 ref가 없어 영향 없음.
- **회귀 확인 포인트:** `test_wiring.py`/`test_parallel.py` 등에서 `on_tool_call`의 args를 검증하는 테스트가 있다면 resolve 전/후 차이(파일 도구는 동일, thread만 실제 id) 확인 필요.

#### 최종 합의 (D8 + 안A + 로그 억제)

| 항목 | 합의 |
|------|------|
| ask_user 식별 | **기존 `tool` 이벤트(`tool=="ask_user"`) 사용** — 구조적 데이터, tools.py/server.py/session.py 변경 0 |
| thread ref 해석 | **안 A**: `loop.py` 1줄 — `on_tool_call`에 **resolve된 `args`** 전달 (v1.0 필수) |
| 로그 중복 방지 (TUI 모드 한정) | ① send_message 이벤트: content가 `[ask-user]`로 시작하면 로그 스킵 ② tool(ask_user) 이벤트: `👤 asks:` 로그 대신 **pinned 패널 표시** (로그 억제) ③ `pin_options: false`면 기존 로그 출력 유지 (config로 복귀) |
| 응답 라우팅 | `human_send(thread_id=args["thread"](resolve 후), content=..., mentions=[질문 에이전트])` |
| v1.1 | `Message.kind`(B안) + 대상 스레드 자동 추적 정식화 |

> **순서 보장 (skip-then-show, agent-1 검증):** `_execute_tool` 내부 `server.send_message`가 **먼저** send_message 이벤트를 emit하고, 그 후 `on_tool_call`이 tool 이벤트를 emit한다. 같은 asyncio 루프 + FIFO 큐이므로 cli는 ① send_message(`[ask-user]` prefix → **스킵**) → ② tool(ask_user)(**pinned 패널 갱신**) 순서로 처리 — **"먼저 숨기고, 그다음 고정 표시"가 구조적으로 보장**된다. 별도 이벤트 추가 없이 문제없음 (D8).

### 3.3 새 컴포넌트: `channel/human_tui.py` (신규)

기존 `_human_input_loop`(cli.py)를 **독립 모듈로 승격**하고 prompt_toolkit 기반으로 재작성 (B레벨: 하단 프롬프트 + bottom toolbar):

```python
# channel/human_tui.py (설계 초안)
"""Human-in-the-loop TUI adapter: 상시 입력창 + pinned ask_user 패널.

- 로그 출력은 기존 rich 파이프라인 그대로 (Session._output_consumer 유지,
  prompt_toolkit patch_stdout()으로 공존)
- 입력은 prompt_toolkit PromptSession.prompt_async() (run_in_executor(input()) 대체)
- ask_user는 기존 tool 이벤트(tool=="ask_user") 수신 → bottom_toolbar에
  선택지 고정 표시 + 번호 응답 (R2/R3)
- thread는 안 A로 resolve된 실제 thread id (loop.py 1줄 변경)
"""

class HumanTUIAdapter:
    def __init__(self, session: Session) -> None:
        self.session = session
        self._pending_questions: deque[dict] = deque()   # PendingQuestions 큐 (다중 질문 순차)
        self._ps = PromptSession(
            history=FileHistory(HISTORY_PATH),          # R5
            auto_suggest=AutoSuggestFromHistory(),      # R5
            multiline=True,                             # R5
            enable_open_in_editor=True,                 # R5
        )

    async def run_input_loop(self) -> None:
        """상시 입력 루프 — Session.run()과 병렬 asyncio 태스크."""
        with patch_stdout():                          # rich 로그와 공존
            while True:
                text = await self._ps.prompt_async(
                    "👤 > ",
                    bottom_toolbar=self._render_toolbar,   # pinned 선택지/상태
                    key_bindings=self._bindings,            # 번호 선택 단축키
                )
                text = text.strip()
                if not text:
                    continue
                await self._deliver(text)                   # human_send()

    def on_ask_user(self, agent_id: str, question: str, options: list[str], thread_id: str) -> None:
        """cli.on_tool_event에서 tool=="ask_user" 수신 → PendingQuestions 큐에 append.

        thread_id는 안 A로 resolve된 실제 thread id.
        """
        self._pending_questions.append({
            "agent_id": agent_id, "question": question,
            "options": options, "thread_id": thread_id,
        })
        # toolbar가 다음 렌더에서 queue[0] 표시 (R2 고정, 다중 질문 순차)

    def _render_toolbar(self) -> Any:
        """prompt_toolkit bottom_toolbar 렌더러 — 질문/선택지를 항상 하단 고정."""
        if not self._pending_questions:
            return HTML(" agent-augury | <b>Ctrl+D</b> 종료")
        q = self._pending_questions[0]
        wait = len(self._pending_questions) - 1
        lines = [f"❓ {q['agent_id']}: {q['question']}"]
        if q["options"]:
            lines.append("   " + "  ".join(f"[{i+1}] {opt}" for i, opt in enumerate(q["options"])))
        if wait:
            lines.append(f"   (대기 질문 {wait}개)")
        return HTML("<br/>".join(lines))

    async def _deliver(self, text: str) -> None:
        # 번호 선택(예: "2")이면 옵션 텍스트로 치환, 그 외엔 원문 전달
        # → session.human_send(thread_id=현재 질문의 thread(resolve 후),
        #                      content=resolved, mentions=[질문한 agent_id])
        # → 응답 후 popleft() → 다음 대기 질문 노출
        ...
```

### 3.4 이벤트 흐름 (ask_user → pinned → 응답) — 최종 합의 기준 (skip-then-show 순서 보장)

```
에이전트: ask_user(thread, question, options)
   │
   ▼
loop.py step(): args = _resolve_refs(call.arguments)   [안 A: resolve 먼저]
   │
   ├─► _execute_tool("ask_user", args) → tools.py: server.send_message(mentions=["human"])
   │     └─► send_message 이벤트 (content="[ask-user] ...")
   │           └─► cli._log_tool_event: TUI 모드에서 "[ask-user]" prefix → **로그 스킵** (1순위)
   │
   └─► on_tool_call(agent_id, "ask_user", args, result)   [안 A: resolve된 args 전달]
         └─► session.py lambda → _emit_event({"type":"tool",
               "tool":"ask_user", "args":{question, options, thread(실제 id)}, ...})   [기존]
               └─► cli._log_tool_event (tool=="ask_user" 분기 — 기존 존재)
                     ├─► (비TUI 모드) "👤 agent-N asks: ..." 로그 출력   [기존 동작 유지]
                     └─► (TUI 모드, pin_options: true)
                           ├─► HumanTUIAdapter.on_ask_user() → PendingQuestions 큐 append
                           │     → bottom_toolbar **고정 표시** (R2) (2순위)
                           │     → 사용자가 "1"/"2"/... 입력 → human_send(
                           │         thread_id=args["thread"], mentions=[질문 에이전트])
                           │     → 에이전트 inbox push → 다음 step() [radio] 흡수 (기존 그대로)
                           └─► (완료)
```

**순서 보장 (agent-1 검증):** 같은 asyncio 루프 + FIFO 큐이므로 cli는 ① `send_message`(스킵) → ② `tool(ask_user)`(고정 표시) 순서로 처리 — **"먼저 숨기고, 그다음 고정 표시"가 구조적으로 보장**된다.

**핵심:** pinned 패널은 **표시 계층**일 뿐, 메시지 전달은 기존 `human_send → inbox push → [radio]` 경로를 그대로 사용한다. SSOT/프로토콜 변경 없음. **tools.py/server.py/session.py 변경 0** (D8), **loop.py만 1줄** (안 A).

### 3.5 입력 → 전달 규칙 (agent-1 보강 반영: 응답 라우팅 스레드 정확성)

| 입력 | 동작 |
|------|------|
| 일반 텍스트 | `session.human_send(thread_id, content=text)` → 에이전트 브로드캐스트 |
| `1`, `2`, ... (선택지 표시 중) | 해당 옵션 텍스트로 치환 후 전달 (사용자 편의) |
| `/quit` 또는 Ctrl+D | 입력 루프 종료 (세션은 계속 진행 — 패시브 원칙) |
| 빈 줄 | 무시 |

- **응답 라우팅 (권장):** `session.human_send(thread_id=질문한 args["thread"](안 A로 resolve된 실제 id), content=..., mentions=[질문한 agent_id])` — ask_user를 보낸 에이전트에게만 회신 (브로드캐스트보다 정밀).
- 기존 `_human_input_loop`가 "첫 번째 스레드"에 보내던 문제를 이 기회에 해결 (활성 ask_user의 thread로).

### 3.6 config 스키마 확장 (agent-1안 반영: `human.tui` 옵트인)

기존 `human.interface`는 유지하고, **신규 `human.tui` 키**로 옵트인한다 (기존 동작 100% 호환):

```yaml
human:
  id: human
  interface: cli            # 기존 그대로: cli | discord | file
  tui: prompt_toolkit       # 신규(선택): prompt_toolkit | textual | none(기존 input)
  pin_options: true         # 신규(선택): ask_user 선택지 하단 고정 여부 (기본 true)
  history_file: ~/.agent-augury/human_history.txt   # 선택 (R5 히스토리)
  input_prompt: "👤 > "      # 선택
```

- `config.py` 검증: `human.tui` 값이 `prompt_toolkit`/`textual`/`none`(또는 생략) 중 하나인지 확인. `tui` 키가 없으면 **기존 `input()` 동작 그대로** (옵트인, 마이그레이션 0).
- CLI: `agent-augury --config session.yaml --interactive` (기존 플래그 그대로) + `human.tui: prompt_toolkit` → 새 입력창 활성화. `--interactive`만 → 기존 input().
- **`human.interface` vs `human.tui` 관계:** `interface`는 채널 종류(현재 cli만), `tui`는 그 cli 채널 내 입력 UI 구현체를 선택하는 세분화 키. `pin_options`는 R2 고정 여부 토글.

### 3.7 의존성

- **신규 의존성:** `prompt-toolkit>=3.0` (pyproject `dependencies` 또는 `[tui]` extras에 추가). rich는 기존 유지.
- prompt_toolkit은 **순수 Python**, 경량 의존성(wcwidth, pygments 정도) — agent-augury에 추가 부담 적음 (agent-2 확인).
- textual은 **필수 아님** (2안/전면 개편 시에만). 필요 시 `[tui-textual]` extras로 분리 가능.
- `pip install prompt-toolkit` — Windows(win32+colorama)에서 ConHost 포함 동작.

### 3.8 변경 파일 요약 (최종 — D8 + 안A, agent-1 확정)

```
src/agent_augury/
  agent/loop.py             # (1줄) on_tool_call에 resolve된 args 전달 (안 A — v1.0 필수)
  channel/human_tui.py      # 신규: HumanTUIAdapter (입력 루프 + pinned 패널 + PendingQuestions 큐)
  cli.py                    # (소) --interactive 분기: human.tui=prompt_toolkit → HumanTUIAdapter 사용
                            #      + _log_tool_event에서 tool=="ask_user" → adapter 배선
                            #      + send_message "[ask-user]" prefix 로그 스킵 (TUI 모드 한정)
                            #      + tool(ask_user) 로그 대신 pinned 패널 (pin_options: true)
                            #      + 세션 종료 시 ps.app.exit() + PendingQuestions 초기화
  config.py                 # (소) human.tui / pin_options / history_file 검증 추가
  agent/tools.py            # (변경 없음) ask_user 그대로 — D8 (tool 이벤트는 이미 존재)
  session.py                # (변경 없음) on_tool_call 배선 이미 존재 (파라미터명 args 그대로)
  server.py                 # (변경 없음) human_send 그대로 — SSOT 불변
pyproject.toml              # prompt-toolkit 의존성 추가
examples/
  human_tui_demo.yaml       # 신규: TUI 데모 (fake 백엔드, ask_user 포함, $thread:0 사용 검증)
tests/
  test_human_tui.py         # 신규: HumanTUIAdapter 단위 테스트 (헤드리스, PipeInput)
```

> **변경 파일 최소화 (agent-1 확정):** `loop.py`(1줄) / `cli.py` / `config.py` / `pyproject.toml` + 신규 `human_tui.py`. **tools.py/server.py/session.py/system_prompt.py는 변경 0.** 기존 테스트(`test_human_in_the_loop.py` 등)는 서버/세션 레벨 변경이 없으므로 **그대로 통과** (단, `on_tool_call` args를 검증하는 테스트는 resolve 전/후 차이 확인 — agent-1).

---

## 4. 테스트 전략 (agent-1 검증 보강 반영)

- **헤드리스 테스트:** prompt_toolkit 입력은 `PipeInput`/`DummyInput`으로 시뮬레이션 (agent-2 조사). `HumanTUIAdapter`의 `_deliver`/`_resolve_option` 로직은 TUI와 분리해 순수 함수로 테스트.
- **E2E (fake 백엔드):** agent-1이 `ask_user` 호출(`thread: "$thread:0"` 포함) → **안 A로 resolve된 실제 thread id**가 tool 이벤트 args로 전달 → TUI 어댑터가 PendingQuestions 큐에 append → 테스트에서 옵션 번호 주입 → `human_send(thread_id=실제 id, mentions=[agent-1])` → 에이전트 다음 step `[radio]` 흡수 → 최종 결과에 반영. **`KeyError: no such thread: $thread:0` 없음 확인 (안 A).**
- **호환성 테스트:** `human.tui` 미지정/`none`일 때 기존 `input()` 경로 회귀 없음 + 비TUI 모드 기존 중복 출력(질문 로그 + asks 로그) 그대로 유지.
- **중복 스킵 테스트 (TUI 모드 한정):** send_message 이벤트 content가 `[ask-user]`로 시작하면 로그 출력 생략 + tool(ask_user) 이벤트 `👤 asks:` 로그 대신 pinned 패널.
- **순서 정합성 테스트 (skip-then-show):** `_execute_tool` 내부 send_message 이벤트 → on_tool_call tool 이벤트 순서로 cli에 도달하는지 단언 (agent-1 검증 §2.1).

### v1.0 통과 기준 (agent-1 제안 단언 포함)

```
시나리오 A (상시 입력): 세션 도중 언제든 사용자 입력이 human_send로 주입되고,
  에이전트가 다음 step()에서 [radio]로 흡수.
  assert human_send 호출 → 에이전트 inbox push 확인
  assert 다음 step() drained_count >= 1

시나리오 B (pinned 선택지): ask_user tool 이벤트 도착 시
  HumanTUIAdapter._pending_questions에 append, "2" 입력 → options[1] 치환 전달.
  assert _pending_questions[0]["options"] 저장됨
  assert _resolve_option("2") == options[1]

시나리오 C (tool 이벤트 경로 + 안 A — D8): ask_user("$thread:0" 사용) 호출 시
  tool 이벤트 args["thread"]가 **resolve된 실제 thread id**인지 확인.
  assert event["type"] == "tool" and event["tool"] == "ask_user"
  assert event["args"]["question"] == "DB는 뭘 쓸까?"
  assert event["args"]["options"] == ["postgres", "mysql"]
  assert event["args"]["thread"] == "thread-1"   # resolve 후 (안 A)
  # tools.py/server.py/session.py 변경 없음 (D8), loop.py 1줄 (안 A)

시나리오 D (중복 스킵 — TUI 모드 한정): send_message 이벤트 content가
  "[ask-user]"로 시작하면 로그 출력 스킵 + tool(ask_user) 로그 대신 pinned 패널.
  assert "[ask-user]" prefix send_message 로그 미출력 (TUI 모드)
  assert "👤 asks:" 로그 미출력 (TUI 모드, pin_options: true)
  assert 비TUI 모드: 기존 중복 출력 유지 (회귀 0)

시나리오 E (응답 라우팅 — agent-1 보강): ask_user의 resolve된 thread로 human_send,
  mentions=[질문한 agent_id] → 해당 에이전트만 [radio] 흡수.
  assert human_send(thread_id="thread-1", mentions=[agent-1]) → agent-1 inbox push
  assert 다른 에이전트 inbox는 비어 있음

시나리오 F (후방 호환): human.tui 미지정/none → 기존 input() 경로, TUI 코드 미활성.
  assert test_human_in_the_loop.py 전체 통과 (회귀 없음)
```

---

## 5. 리스크와 대가

| 리스크 | 완화 |
|--------|------|
| prompt_toolkit 추가 의존성 | 경량(순수 Python), rich와 충돌 없음. extras(`[tui]`)로 분리 가능 (agent-1 확인) |
| Windows 구형 cmd.exe | prompt_toolkit은 win32+colorama로 ConHost도 네이티브 동작. 다만 **Windows Terminal 권장** (사용자 환경이 Windows이므로 README에 안내) |
| bottom_toolbar가 로그와 겹침 | toolbar는 터미널 최하단 고정 — rich 로그는 그 위 영역에서 스크롤. 터미널 크기 작을 때는 로그 영역 축소 (선택: 최소 높이 경고) |
| 고빈도 로그 스트리밍 시 patch_stdout 깜빡임 | 선택지는 toolbar로 분리해 로그와 무관하게 고정 (B레벨). 로그 자체는 기존 rich 파이프라인 유지 (agent-1/agent-2 지적). 깜빡임이 심하면 v1.1 full-screen(C레벨) 전환 |
| ask_user 질문이 로그에도 남아 중복 표시 | 최종 합의: tool 이벤트가 pinned 패널 정식 표시, send_message `[ask-user]` prefix 로그 **스킵 (TUI 모드 한정)** + `👤 asks:` 로그 대신 pinned 패널. 비TUI 모드는 기존 중복 유지 (회귀 0). `pin_options: false`로 복귀 가능 |
| `$thread:N` ref를 human_send에 그대로 전달 → KeyError (agent-1 검증 — v1.0 블로커) | **안 A: loop.py 1줄** — on_tool_call에 resolve된 args 전달. E2E 시나리오 C로 고정 |
| 다중 ask_user 동시 도착 | PendingQuestions 큐 순차 노출 + "대기 N개" 배지 (agent-2 D3) |
| 응답이 다른 에이전트로 오인 전달 (agent-1) | `mentions=[질문한 agent_id]`로 회신 정밀화 (§3.5) |
| `on_tool_call` args 변경의 회귀 (안 A) | 파일 도구는 ref가 없어 영향 없음. thread만 실제 id로 변경. `test_wiring.py`/`test_parallel.py`에서 args 검증 테스트 확인 (agent-1) |
| 영속화 로그 재생 시 ask_user 식별 불가 | v1.1에서 B안(kind 필드 + aiosqlite **ALTER TABLE** 마이그레이션)으로 정식화 (agent-2 요청) |
| `prompt_async`가 종료 안 되어 세션 종료 지연 | 세션 종료 시 `ps.app.exit()` 명시 호출 + PendingQuestions 큐·pinned 상태 초기화 (agent-1 §2.4) |
| 세션 종료 후 입력 루프 정리 | `Session.close()`/`_run` finally에서 TUI 태스크 취소 (기존 `_human_input_loop` cancel 패턴 재사용) |
| 터미널 크기/리사이즈 | prompt_toolkit 자동 처리 (툴바 래핑). textual 2안 시 `on_resize` 대응 |

---

## 6. 로드맵

| 단계 | 범위 | 산출물 | 통과 기준 |
|------|------|--------|-----------|
| **v1.0** | **안 A(loop.py 1줄)** + `HumanTUIAdapter`(prompt_toolkit B레벨) + `human.tui` 옵트인 + pinned 선택지 패널(PendingQuestions) + 번호 응답 + `[ask-user]` prefix 로그 스킵(TUI 한정) + 응답 라우팅(mentions) + 히스토리 | `loop.py`(1줄), `channel/human_tui.py`, `config.py`, `cli.py`, `pyproject.toml` | 시나리오 A~F (헤드리스 + fake E2E, PipeInput, `$thread:0` 포함) |
| **v1.1** | B안 kind 필드 정식화(**aiosqlite ALTER TABLE** 마이그레이션), 대상 스레드 자동 추적 정식화, 멀티 ask_user 큐 심화, 응답 타임아웃, (선택) full-screen C레벨 전환 | `server.py`, `human_tui.py` 개선 | 영속화 ask_user 재생, 연속 ask_user 처리, 타임아웃 시 기본 경로 |
| **v2.0 (선택)** | textual 전면 개편형 채팅 TUI (`[tui-textual]` extras) | `channel/human_tui_textual.py` | RichLog 이식 + OptionList 선택 E2E |

---

## 7. 결론

- 요구사항의 본질은 **"기존 로그 흐름 유지 + 하단 상시 입력창 + 선택지 고정 패널"** 이며, 이는 기존 rich 출력 파이프라인과 **공존 가능한 인라인 모델**이 적합하다.
- **1안: prompt_toolkit** — 침습 범위 최소(`loop.py` 1줄 + `cli.py`/`config.py` 소량 + 신규 모듈 1개), `bottom_toolbar`로 R2(선택지 고정)를 직접 해결, `prompt_async()`로 R1/R5 해결, `patch_stdout()`으로 rich 로그와 공존, Windows ConHost까지 네이티브 지원. **권장 (4개 트랙 합의).**
- **2안: textual** — "전면 개편형 채팅 TUI"가 요구될 때 유효. 현재 요구에는 과설계(전체 화면 전환 + 출력 파이프라인 이식).
- **ask_user 식별은 최종 합의: 기존 `tool` 이벤트(`tool=="ask_user"`) 사용** — 구조적 데이터(question/options/thread)가 이미 args로 전달되며 `cli._log_tool_event` 분기도 이미 존재. **tools.py/server.py/session.py 변경 0.** send_message의 `[ask-user]` prefix는 TUI 모드에서 로그 스킵의 보조 신호로만 사용 (비TUI 회귀 0). v1.1에서 B안(kind + ALTER TABLE) 정식화.
- **thread ref 블로커는 안 A로 해결 (v1.0 필수)** — `loop.py`의 `on_tool_call`에 resolve된 `args` 전달 (1줄). `$thread:N` → 실제 thread id 보장 → `human_send` KeyError 방지.
- **config는 `human.tui` 옵트인** — 기존 동작 100% 호환, 마이그레이션 0. 변경 파일은 loop.py(1줄)/cli.py/config.py/pyproject.toml + 신규 human_tui.py.
- 기존 HITL 인프라(`human_send`, `ask_user`, `[radio]` 흡수)는 **그대로 재사용** — 표시/입력 계층(HumanAdapter)만 교체하는 최소 침습 설계다.
- Windows 사용자 환경은 prompt_toolkit 네이티브 지원이 넓지만 **Windows Terminal(ConPTY) 사용을 권장**.
