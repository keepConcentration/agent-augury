# agent-augury TUI — Enter 제출 불가 버그 분석 & 수정 설계

> **Status:** 구현 확정 · 설계 통합본 v1.1 (사용자 UX: 6-1 안B / 6-2 Enter 통일 / 6-3 안내 영어)
> **Date:** 2026-09 · **Author:** agent-2 (분석/초안) · **검토/통합:** agent-1 · agent-3 · agent-4
> **Rev:** v1.1 — **agent-3 구현 디테일 검증 반영**: ① `@kb.add("shift","enter")` 제거
> (prompt_toolkit에 "shift" 단독 키 없음 → `_parse_key` ValueError, 기존 주석 근거 복원)
> ② win32 Ctrl+Enter 매핑 정정: `[Escape, ControlJ]`(ControlM 아님) → 안 B에서
> `(escape, c-j)` 개행 바인딩 추가로 Ctrl+Enter 제출 오염 방지 ③ §2.3 서술 정정.
> **검증 상태:** 4인 정적 분석 완전 수렴 (prompt_toolkit 3.0.53 설치본 소스 근거)
> **관련 문서:**
> - `SESSION_TUI_REDESIGN.md` v2.5 — 통합 상위 설계 (Enter=제출 확정 D-A3, D-A14, 멀티라인 유지 §3.5)
> - `INPUT_ROUTING_SLASH_UX_DESIGN.md` v0.5.1 — 키바인딩 표 §5.4 (Enter=제출)
> - `TUI_IMPLEMENTATION_REVIEW.md` — 기존 리뷰 (P0 Ctrl+D 데드락 — **이미 수정 완료**)
> - `TUI_ENTER_SUBMIT_FIX_DESIGN_agent4.md` — 보조 문서 (통합본 하위 참조, 중복 없음)
> - prompt_toolkit 소스 (venv): `key_binding/bindings/basic.py`, `emacs.py`,
>   `application/application.py`(_CombinedRegistry), `input/win32.py`(ConsoleInputReader),
>   `widgets/base.py`(TextArea), `buffer.py`(accept_handler/validate_and_handle/append_to_history),
>   `keys.py`(Enter=ControlM alias, shift 단독 키 부재), `key_binding/key_bindings.py`(_parse_key)

---

## 0. 한 줄 결론

> **현재 구현은 "Enter=제출" 설계(D-A3, §5.4)를 실제로 충족하지 못한다.**
> `TextArea(multiline=True)`에 **`enter` 단독 키바인딩이 없어서** Enter가
> prompt_toolkit 기본 바인딩의 "multiline newline"에 가로채여 **항상 개행만** 되고
> `accept_handler`(제출)는 **한 번도 호출되지 않는다.**
> → 사용자가 겪은 "메시지는 입력되는데 보내는 방법을 모르겠다"의 정확한 원인.
>
> **수정 (4인 합의 + v1.1 디테일):**
> 1. `tui/input_bar.py` — `@kb.add("enter", eager=True)` + `@kb.add("c-j", eager=True)`
>    핸들러에서 `event.current_buffer.validate_and_handle()` 호출 (accept_handler 경유 —
>    기존 라우팅 재사용). `_accept`는 `buff.reset()` **먼저 하지 않고** `return False`
>    (reset/append_to_history 순서를 validate_and_handle에 위임 → **2차 버그
>    (history 저장 누락) 동시 수정**).
> 2. **안 B(멀티라인 유지)를 1순위로 확정** — v2.5 §3.5 사용자 확정("Enter=제출 +
>    Shift/Esc+Enter 개행")과 일치 + 붙여넣기/여러 줄 시나리오 대응.
>    안 A(단일 라인)는 사용자가 멀티라인 포기를 선택할 때만 (§6.1).
> 3. **개행은 `escape+enter`(=Shift+Enter 정규화 결과) + `escape+c-j`(win32 Ctrl+Enter)로**
>    — `shift` 단독 키는 prompt_toolkit에 없어 직접 바인딩 불가 (key_aliases가
>    Shift+Enter 시퀀스를 `(Escape, ControlM)`으로 정규화). `c-j`를 제출로 바꾸므로
>    win32 Ctrl+Enter(`(Escape, ControlJ)`)는 `escape+c-j` 개행으로 명시 보호.
> 4. 위저드 "Initial Task" 프롬프트(`cli.py::_prompt_multiline`) 키맵 통일 여부는
>    **사용자 의견 대기** (§6.2).
> 5. Ctrl+D 데드락(P0)은 이미 수정된 상태임을 확인 — 회귀 방지 테스트만 추가.

---

## 1. 문제 정의 — 사용자가 겪은 증상과 진단

### 1.1 사용자 재현 시나리오 (Windows 11, cmd)

```
C:\Users\test> agent-augury

Using saved model config. Config saved to: C:\Users\test\.agent-augury\agent-augury-session.yaml

--- Initial Task ---
What would you like to do? [Multi-agent collaboration]  (Esc+Enter로 제출)

(사용자: 한 줄의 메시지만 보낼 수 있는 것 같다)

→ 메시지 입력 후 Enter → full-screen TUI로 전환
→ 입력줄(👤 >)에 메시지는 타이핑되지만 Enter를 눌러도 제출이 안 됨
→ "보내는 방법을 모르겠다"
```

### 1.2 증상 분해

| # | 증상 | 1차 원인 | 근거 코드 |
|---|------|----------|-----------|
| S1 | "Initial Task"에서 **한 줄의 메시지만** 보낼 수 있음 | 위저드 프롬프트가 `_prompt_multiline` + **Esc+Enter 제출** — Enter는 개행 | `cli.py::_prompt_multiline` (kb: `escape+enter`만 등록) |
| S2 | full-screen 전환 후 **메시지 입력은 되지만 Enter가 제출 안 됨** | `InputBar`에 `enter` 단독 바인딩 부재 → 기본 newline이 가로챔 | `tui/input_bar.py::_build_bindings` (escape+enter / c-d / c-c만) |
| S3 | TUI 진입 후 첫 세션은 "Initial Task"의 task로 실행되므로, 그 뒤 대화를 이어가려면 **다음 턴 입력이 필수**인데 그 입력이 불가 | S2와 동일 — `session_loop`가 `next_turn.get()`에서 대기 | `cli.py::_run_repl_tui::session_loop` |

> **왜 "한 줄만" 보낼 수 있다고 느꼈는가:** 위저드 단계에서 Esc+Enter로 task를
> 제출하면 그 task가 첫 세션 턴의 `initial_prompt`가 된다. 그 턴이 끝나면
> `session_loop`는 다음 입력을 `next_turn.get()`으로 기다리는데, TUI 입력줄에서
> Enter가 개행만 하므로 **영원히 다음 턴을 시작할 수 없다.** 즉 "대화 1회 후
> 더 이상 진행 불가"가 사용자가 느낀 "한 줄만 보낼 수 있다"의 실체.

---

## 2. 근본 원인 분석 (코드 정적 분석 — 4인 확정)

### 2.1 prompt_toolkit의 Enter 처리 규칙

`TextArea`는 내부적으로 `Buffer`를 감싼다. `Buffer.multiline`과
`accept_handler`(BufferAcceptHandler)가 Enter 동작을 결정한다:

```python
# buffer.py — multiline 파라미터 문서
":param multiline: ... When not set, pressing `Enter` will call the
 accept_handler. Otherwise, pressing `Esc-Enter` is required."
```

Enter 키에 대한 실제 바인딩은 prompt_toolkit 기본 키바인딩 두 곳에 있다:

```python
# key_binding/bindings/basic.py
@handle("enter", filter=insert_mode & is_multiline)
def _newline(event):
    """Newline (in case of multiline input)."""
    event.current_buffer.newline(copy_margin=not in_paste_mode())

# key_binding/bindings/emacs.py
# Enter: accept input in single line mode.
handle("enter", filter=insert_mode & is_returnable & ~is_multiline)(
    get_by_name("accept-line")
)
```

- `multiline=True` → **basic.py의 newline이 활성**, emacs.py의 accept-line은 `~is_multiline`이라 **비활성**.
- `accept-line`(named command)이 `Buffer.validate_and_handle()` → `accept_handler` 호출 경로.
- 즉 **multiline=True에서 Enter=제출이 되려면** 명시적 `enter` 바인딩으로
  `validate_and_handle()`을 직접 호출해야 한다 (기본 newline보다 우선순위에서 이김).

### 2.2 키바인딩 우선순위 — 컨트롤 kb가 이긴다 (4인 소스 확인)

`Application`은 `_CombinedRegistry`(application.py)로 키바인딩을 병합한다:

```python
# _CombinedRegistry._create_key_bindings — 컨트롤 kb를 뒤에(우선) 배치
key_bindings.append(self.app._default_bindings)
key_bindings = key_bindings[::-1]   # ← 현재 컨트롤의 kb가 마지막 = 최우선
return merge_key_bindings(key_bindings)
```

- 병합 리스트를 뒤집으므로 **현재 포커스된 컨트롤의 `key_bindings`가 전역 기본
  바인딩보다 항상 먼저 매칭**된다.
- `key_processor._process`는 `matches[-1]`을 호출 → merged 목록의 **마지막이
  컨트롤 kb**이므로, 컨트롤 kb에 `enter` 단독 바인딩이 있으면 사용자 바인딩이 이긴다.
- **eager에 관해 (팀 합의):** `enter`(=ControlM)는 더 긴 매치(`escape+enter`)의
  접두사가 아니므로(`_is_prefix_of_longer_match` False) **eager 없이도 동작**한다
  (agent-1 확인). 다만 **명시적 `eager=True`로 기본 newline과의 경합을 계약으로
  고정**하는 것을 권장 (agent-3/4) — 무해하고 회귀 방지에 명확.
- ⚠️ **`Keys.ControlJ`(c-j, `\n`)는 별개 키.** Windows 일반 Enter는
  `Keys.ControlM`(enter)이므로 c-j와 무관하지만, 일부 터미널/환경에서 Enter가
  `\n`으로 도착할 수 있으므로(기본 바인딩 주석이 "Linux subsystem for Windows 등
  일부 터미널은 Enter를 \n으로 보냄" 명시) **enter와 c-j를 함께 바인딩**하는 것이
  안전하다.

### 2.3 Windows 입력 경로 (win32) — 4인 확인 (+v1.1 정정)

```python
# input/win32.py::ConsoleInputReader._event_to_key_presses
# Windows가 보내는 \n을 \r로 치환 (unix 호환)
if self.mappings[ascii_char] == Keys.ControlJ:
    u_char = "\n"  # Windows sends \n, turn into \r for unix compatibility.
result = KeyPress(self.mappings[ascii_char], u_char)

# Ctrl+Enter → [Escape, ControlJ]로 변환 (vt100과 달리 탐지 가능)
if (... Ctrl pressed ...) and result.key == Keys.ControlJ:
    return [KeyPress(Keys.Escape, ""), result]
```

- **일반 Enter** → `KeyPress(Keys.ControlM)` = `"enter"` 키 (`keys.py`:
  `Enter = ControlM` alias, `KEY_ALIASES["enter"]="c-m"`).
- **Ctrl+Enter** → `[Keys.Escape, Keys.ControlJ]` (**ControlM이 아니라 ControlJ** —
  v1.1 정정). `escape+enter`(=(Escape, ControlM))와는 **다른 시퀀스**다.
- ConHost(ConsoleInputReader)와 Windows Terminal VT(Vt100ConsoleInputReader) 모두
  동일 매핑 확인.
- ⚠️ **v1.1 함정 정리:** 현재 `InputBar`의 `escape+enter` 바인딩은 (Escape, ControlM)
  매치이므로 win32 Ctrl+Enter(Escape, ControlJ)와 **불일치**한다. 기존에는 Ctrl+Enter
  → `escape`만 매치(기본 escape no-op) 후 `ControlJ` → 기본 `c-j` → `feed(ControlM)` →
  multiline newline으로 **우연히 개행**되었다. 안 B에서 `c-j`를 제출로 바꾸면 이
  우연 경로가 제출로 오염되므로, **`(escape, c-j)` 개행 바인딩을 명시**해야 한다 (§3.2).

### 2.4 현재 InputBar 코드 — 결함 확정 (+2차 버그)

```python
# tui/input_bar.py (현재)
def _build_bindings(self) -> KeyBindings:
    kb = KeyBindings()
    @kb.add("escape", "enter")     # (Escape, ControlM) 개행 — win32 Ctrl+Enter(Escape, ControlJ)와는 다름
    def _nl1(event): event.current_buffer.insert_text("\n")
    @kb.add("c-d")
    def _quit(event): ...
    @kb.add("c-c")
    def _clear(event): ...
    return kb

self.widget = TextArea(
    multiline=True,
    accept_handler=_accept,       # 있지만 호출 경로가 없음
    ...
)
self.widget.control.key_bindings = self._kb   # 컨트롤 kb 부착

def _accept(buff: Buffer) -> bool:
    text = buff.text
    buff.reset()                  # ← 2차 버그: validate_and_handle의
    self._dispatch(text)          #   append_to_history가 빈 text로 저장 실패
    return True
```

- **결함 ① (Enter 제출 불가):** `enter` 바인딩 부재 → multiline newline이 가로챔
  → `_accept` 호출 경로 없음.
- **결함 ② (history 저장 누락 — agent-4 발견):** `_accept`가 `buff.reset()`을
  **먼저** 호출한 뒤 `return True`. 정상 경로인 `Buffer.validate_and_handle()`은
  `accept_handler` 호출 → `append_to_history()` → `reset()` 순서인데, 이미 reset된
  상태면 `self.text == ""`이라 `append_to_history`의 `if self.text:` 가드에 걸려
  **입력이 히스토리에 저장되지 않는다.** (PromptSession의 accept는
  `get_app().exit(result=buff.document.text)` 후 True 반환 + "reset later" 패턴이라
  이 문제가 없지만 우리 구현만 reset을 먼저 함.)
- `self.widget.control.key_bindings = self._kb` — 이 할당 자체는 `BufferControl`
  생성 후라 동작하지만, `enter` 바인딩이 없어 의미가 없다.
- `TextArea.__init__`에 `key_bindings` 파라미터가 **없다** (prompt_toolkit 3.0.x
  소스 확인 — widgets/base.py) — 컨트롤 후속 할당은 유일한 방법이 맞다.

### 2.5 위저드 `_prompt_multiline` — 동일 기제

```python
# cli.py::_prompt_multiline
kb = KeyBindings()
@kb.add("escape", "enter")        # Esc+Enter 제출
def _submit(event): buff.validate_and_handle()
session = PromptSession(multiline=True, key_bindings=kb)
hint = "  (Esc+Enter로 제출)"
```

- `multiline=True` + Enter 바인딩 부재 → **Enter는 개행**.
- `PromptSession`의 기본 accept-line은 `~is_multiline`이라 비활성 (emacs.py).
- Esc+Enter만 `validate_and_handle()` → 제출.
- **사용자가 "한 줄만 보낼 수 있다"고 느낀 직접 원인** — 위저드에서도 Enter는
  개행이고, "Esc+Enter로 제출" 안내를 보고 제출해야 했다.

---

## 3. 해결 설계 — 수정안 (4인 합의 + v1.1 디테일)

### 3.1 설계 원칙 (상위 문서와 정합)

1. **Enter=제출은 사용자가 확정한 UX(D-A14)이며, hermes 공식 키맵 표준이다.**
   → "왜 전송은 Enter로 하고 싶어?"에 대한 근거:
   - 채팅/메신저·CLI REPL·IDE 터미널의 **보편적 관례** (한 줄 입력의 제출 키).
   - **IME(한글) 안전**: 조합 중 Enter는 OS가 가로채므로, Enter=제출이 조합
     확정과 충돌하지 않는다. (반대로 Esc+Enter는 조합 중 Esc가 먼저 소비될 수
     있어 불안정하다.)
   - **빈 줄 Enter=무시(ignored) 정책(D-A20)과 결합**하면 실수로 빈 Enter를
     눌러도 아무 일도 없어 안전하다.
   - 멀티라인이 필요하면 **Shift+Enter/Esc+Enter(개행)** 로 명시 — hermes 표준
     (`Enter=Submit / Shift+Enter=newline`).
2. **SSOT·서버·세션·에이전트 루프 변경 0** — 표시/입력 계층만 수정.
3. **Windows + Linux + macOS 일관** — win32 네이티브 경로와 vt100 경로 모두 커버.

### 3.2 수정 대상 — TUI 입력줄 (`tui/input_bar.py`) — **안 B 확정 (1순위)**

> **판단 근거 (agent-1):** 설계 v2.5 §3.5는 이미 "Enter=제출 + Shift/Esc+Enter
> 개행(멀티라인 유지)"로 **사용자 확정**된 상태. 설계 정합성상 **안 B(멀티라인
> 유지 + 명시적 enter 바인딩)가 1순위**. 안 A는 사용자가 멀티라인 포기(§6.1 2a)를
> 선택할 때만 채택.

```python
# tui/input_bar.py — 안 B (4인 합의 + v1.1 정정)
def _build_bindings(self) -> KeyBindings:
    kb = KeyBindings()

    # ★ 핵심: Enter = 제출 (multiline 기본 newline보다 컨트롤 kb 우선 + eager로 계약 고정)
    @kb.add("enter", eager=True)
    @kb.add("c-j", eager=True)            # 일부 터미널이 Enter를 \n으로 보내는 경우 대응
    def _submit(event: Any) -> None:
        event.current_buffer.validate_and_handle()   # accept_handler 경유 (기존 라우팅 재사용)

    # 개행 ①: Esc+Enter / Shift+Enter — Shift+Enter는 key_aliases가 (Escape, ControlM)으로
    #         정규화하므로 별도 바인딩 불필요 (v1.1: "shift" 단독 키는 prompt_toolkit에 없어
    #         직접 바인딩하면 _parse_key ValueError — 기존 주석 "do not bind s-enter" 근거)
    @kb.add("escape", "enter")
    def _nl1(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    # 개행 ②: Ctrl+Enter (win32는 (Escape, ControlJ)로 도착 — c-j를 제출로 바꿨으므로
    #         명시 보호 필요, v1.1 추가)
    @kb.add("escape", "c-j")
    def _nl2(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    @kb.add("c-d")
    def _quit(event: Any) -> None:
        if self._on_quit is not None:
            self._on_quit()
        event.app.exit()

    @kb.add("c-c")
    def _clear(event: Any) -> None:
        event.current_buffer.reset()
    return kb

def _accept(buff: Buffer) -> bool:
    text = buff.text
    self._dispatch(text)
    return False            # ★ reset/append_to_history는 validate_and_handle이 처리 (순서 보존)
```

- **`enter`(=`c-m`)와 `c-j`를 함께 eager 바인딩** — Windows 일반 Enter는 `c-m`,
  일부 터미널은 `c-j`로 도착. 둘 다 `validate_and_handle()` 경유.
- **개행 2종:** `escape+enter`(=Esc+Enter, Shift+Enter 정규화 결과) /
  `escape+c-j`(win32 Ctrl+Enter). `c-j`를 제출로 바꿨으므로 Ctrl+Enter가 제출로
  오염되지 않도록 `escape+c-j`를 명시 (v1.1 핵심).
- **`validate_and_handle()` 체인:** `validate()` → `accept_handler(_accept)` →
  `_dispatch(text)` (async → `app.create_background_task`) → `append_to_history()`
  → `reset()`. **순서 보존 → history 저장 버그(결함 ②) 해소.**
- **`_accept` return False:** keep_text=False → `reset()` (버퍼 비움). 의미 명확.
- eager=True: 기본 newline과의 경합을 명시적 계약으로 고정 (agent-3/4 권장).
  agent-1 확인: enter는 더 긴 매치의 접두사가 아니므로 eager 없이도 동작하지만,
  **eager=True가 무해하고 회귀 방지에 명확** — 4인 합의로 채택.
- `TextArea(multiline=True, accept_handler=_accept, ...)` — **multiline 유지**,
  `control.key_bindings = self._kb` 부착 유지 (TextArea 생성자에 key_bindings
  파라미터가 없으므로).
- **paste_mode=True**(app.py)와의 정합: BracketedPaste로 붙여넣은 `\n`은 버퍼에
  삽입 → 멀티라인 유지(안 B)라 **붙여넣기 멀티라인이 안전** (안 A 선택 시
  잘림/깨짐 리스크 — agent-1 보강).

#### 안 A — 단일 라인 + accept_handler (사용자가 멀티라인 포기 시에만)

```python
# tui/input_bar.py — 안 A
self.widget = TextArea(
    multiline=False,              # ← Enter=제출이 기본 accept-line으로 보장
    accept_handler=_accept,       # 기존 그대로 (단, _accept는 return False로)
    height=height,
    prompt=prompt,
    history=hist,
)
self.widget.control.key_bindings = self._kb
```

- `multiline=False` → emacs.py의 `accept-line`(filter: `is_returnable & ~is_multiline`)
  이 활성 → Enter → `validate_and_handle()` → `_accept`.
- **주의 (agent-1):** `paste_mode=True`(app.py 설정)가 BracketedPaste로 `\n`을
  버퍼에 삽입 — 단일 라인이면 붙여넣기 멀티라인이 잘리는 동작 확인 필요.
- 개행 키(shift/escape+enter)는 무효화됨 — `/help`에서 "개행 미지원" 안내.
- 안 A에서는 `enter`/`c-j` 바인딩이 없어도 기본 accept-line이 제출을 보장하므로
  §3.2의 개행 보호 바인딩도 불필요 (단일 라인에는 Ctrl+Enter 개행 의미 없음).

#### 안 A vs 안 B 요약

| 기준 | 안 A (단일 라인) | 안 B (멀티라인) — **1순위** |
|------|-----------------|------------------------------|
| v2.5 사용자 확정 | 위반 (개행 불가) | **부합** (Enter=제출 + Shift/Esc+Enter 개행) |
| 변경량 | 최소 (multiline=False 1줄) | 중 (enter/c-j/escape+c-j 바인딩 + _accept 수정) |
| Enter=제출 보장 | 구조적 (기본 accept-line) | 명시적 바인딩 (eager=True) |
| 개행 | 미지원 | Shift/Esc+Enter, Ctrl+Enter(win32) |
| 붙여넣기 멀티라인 | 리스크 (paste_mode와 상충 가능) | 안전 |
| 복잡도/회귀 위험 | 낮음 | 중간 (IME 실측 필요) |
| UX | 채팅형 단일 메시지 | 에디터형 멀티라인 (hermes 표준) |

**【사용자 의견 필요 — §6.1】** 멀티라인 입력이 필요한지 최종 확인 (기본: 안 B).

### 3.3 수정 대상 — 위저드 "Initial Task" (`cli.py::_prompt_multiline`)

현재: `multiline=True` + `escape+enter` 제출 + 안내 "(Esc+Enter로 제출)".

**【사용자 의견 필요 — §6.2】** TUI와 동일하게 **Enter=제출**로 통일할지 결정.
- 통일 시 (agent-1: **(a) 통일 권장** — 같은 프로그램에서 키맵이 섞이면 혼란):
  ```python
  def _prompt_multiline(prompt: str) -> str:
      kb = KeyBindings()
      @kb.add("enter", eager=True)
      @kb.add("c-j", eager=True)
      def _submit(event):
          buff = event.current_buffer
          buff.validate_and_handle()
      @kb.add("escape", "enter")     # 개행 (Esc+Enter) — 기존 제출 의미는 Enter로 이동
      @kb.add("escape", "c-j")       # Ctrl+Enter(win32) 개행 (v1.1 정정 반영)
      def _nl(event):
          buff = event.current_buffer
          buff.insert_text("\n")
      ...
      hint = "  (Enter로 제출, Shift+Enter로 줄바꿈)"
  ```
- 미통일 시: 현행 유지 (Esc+Enter) — 단, 안내 문구를 더 명확히.

> `PromptSession`은 `patch_stdout()`과 함께 쓰여 위저드 단계의 기존 stdout
> 출력(로그)과 공존한다. Enter=제출로 바꿔도 이 구조는 그대로 유지.

### 3.4 수정 대상 — 전송 피드백 / 상태바 (정합 점검)

- `app._send`의 피드백 `✓ human → thread-1 (broadcast): ...`는 **이미 구현되어
  있다** (R10). Enter 제출이 복구되면 이 피드백이 사용자에게 "전송됨" 확인을 준다.
- `status_bar`의 `👤 waiting`/`running` 힌트 유지.
- 추가 제안: `/help`에 키맵(Enter=제출, Shift/Esc+Enter 개행, Ctrl+C 클리어,
  Ctrl+D 종료, 빈 줄 무시) 명확히 표기.

### 3.5 수정 대상 — `/help` 안내 (tui/commands.py)

```python
def _help_text(...):
    lines = [
        ...
        "Keys:",
        "  Enter               - submit",
        "  Shift+Enter / Esc+Enter - newline",
        "  Ctrl+Enter          - newline (Windows)",
        "  Ctrl+C              - clear draft",
        "  Ctrl+D              - quit",
        "",
        "빈 줄 Enter는 무시됩니다. 종료는 /quit 또는 Ctrl+D.",
        "평문 quit/exit는 에이전트에게 전송됩니다 (종료 아님).",
    ]
```

---

## 4. 검증 방법 (수정 후)

### 4.1 헤드리스 (PipeInput — 회귀 방지)

| # | 시나리오 | 기대 |
|---|----------|------|
| V1 | `PipeInput`으로 "hello" + Enter(`KeyPress(Keys.ControlM)`) 전송 → `_dispatch` 호출 → `human_send` 실행 + 로그 `✓ human → ...` | Enter=제출 |
| V2 | 빈 줄 + Enter → router `ignored` → 아무 일도 없음 | 빈 입력 안전 (D-A20) |
| V3 | ask_user 활성 + "2" + Enter → 옵션 치환 → 질문 스레드 전송 | 선택지 응답 |
| V4 | Ctrl+D → `on_quit()` + `app.exit()` → REPL 루프 정상 종료 (P0 회귀 방지 — **수정 완료 확인**) | 종료 |
| V5 | Ctrl+C → 드래프트만 클리어, 앱/세션 유지 | 클리어 |
| V6 | "a\nb" + Shift+Enter(개행) 후 Enter → 멀티라인 제출 | 멀티라인 |
| V6b | "a\nb" + `(Escape, ControlJ)`(win32 Ctrl+Enter) → 개행, 제출 아님 (v1.1: `escape+c-j` 바인딩 검증) | Ctrl+Enter 개행 보호 |
| V7 | "hello" + Enter → 히스토리에 저장 확인 (`FileHistory`에 "hello" 포함) | **결함 ② 회귀 방지** |

> `test_human_tui.py`/`test_tui_*` 신규 케이스 추가. PipeInput으로 Enter를
> `KeyPress(Keys.ControlM)`으로 보내는 헬퍼를 공용화.

### 4.2 실측 (Windows Terminal + cmd — 사용자 환경)

| # | 시나리오 |
|---|----------|
| V8 | **(필수) 한글 IME 조합 중 Enter** → 조합 확정 / 확정 후 별도 Enter가 제출로 동작하는지 실측. win32 `ReadConsoleInputW`는 조합 상태를 앱 레벨에서 추적하지 않으므로 **환경 의존적** (agent-1/4) — Windows Terminal과 ConHost 둘 다 확인 |
| V9 | cmd에서 `agent-augury` → 위저드 → TUI 전환 → Enter 제출 → 다음 턴 진행 |
| V10 | Ctrl+Enter(개행) → 멀티라인 입력 → Enter 제출 (안 B, v1.1 `escape+c-j` 포함) |
| V11 | 긴 텍스트 붙여넣기(멀티라인) → Enter 제출 (paste_mode 정합) |

---

## 5. 변경 파일 요약

| 파일 | 변경 | 비고 |
|------|------|------|
| `src/agent_augury/tui/input_bar.py` | **핵심 수정** — `enter`+`c-j` eager 바인딩(validate_and_handle 경유) + `escape+c-j` 개행(win32 Ctrl+Enter 보호) + `_accept` return False(순서 보존) | Enter=제출 복구 + history 버그 수정 |
| `src/agent_augury/cli.py` | `_prompt_multiline` — Enter=제출 통일 (사용자 결정 시, §6.2) | 위저드 키맵 |
| `src/agent_augury/tui/commands.py` | `/help` 키 안내 갱신 (Ctrl+Enter 개행 포함) | 문서 정합 |
| `docs/tui/INPUT_ROUTING_SLASH_UX_DESIGN.md` §5.4 | 구현 컬럼 갱신 (안 확정 후) | 상위 문서 정합 |
| `docs/tui/SESSION_TUI_REDESIGN.md` §3.5 | 구현 확정 반영 | 상위 문서 정합 |
| `tests/` | `test_human_tui.py`/`test_tui_input_bar.py`(신규) — V1~V7, V6b | 회귀 방지 |

> **SSOT 불변:** `server.py` / `session.py` / `agent/loop.py` / `tools.py` /
> `system_prompt.py` / `config.py` — 변경 없음.

---

## 6. 【사용자 의견】 Decisions — **확정**

> 사용자 답변 (2026-09): **6-1 안 B** · **6-2 Enter 제출 통일** · **6-3 안내 문구 영어**.

### 6.1 TUI 입력줄 — 멀티라인 필요 여부 (안 A vs 안 B) — **확정: 안 B**

- **안 B (멀티라인 유지):** Enter=제출 + Shift/Esc+Enter / Ctrl+Enter 개행.
- 안 A는 채택하지 않음.

### 6.2 위저드 "Initial Task" — 키맵 통일 여부 — **확정: Enter=제출 통일**

- 위저드 `_prompt_multiline`도 TUI와 동일: Enter/C-j=제출, Esc+Enter / Esc+C-j=개행.
- 안내: `(Enter to submit, Shift+Enter for newline)`.

### 6.3 안내 문구 언어 — **확정: 영어**

- `/help` Keys 섹션 및 위저드 hint는 영어.

---

## 7. 근거 자료 (prompt_toolkit 소스 발췌 — 4인 교차 검증 + v1.1)

```python
# buffer.py — multiline 파라미터 문서
":param multiline: ... When not set, pressing `Enter` will call the
 accept_handler. Otherwise, pressing `Esc-Enter` is required."

# buffer.py — append_to_history (결함 ② 근거)
def append_to_history(self) -> None:
    if self.text:                     # ← reset 후 호출되면 빈 문자열 → 저장 안 됨
        ...

# buffer.py — validate_and_handle (정상 순서: accept → append_to_history → reset)
def validate_and_handle(self) -> None:
    valid = self.validate(set_cursor=True)
    if valid:
        if self.accept_handler:
            keep_text = self.accept_handler(self)
        else:
            keep_text = False
        self.append_to_history()
        if not keep_text:
            self.reset()

# keys.py — Enter = ControlM alias
Enter = ControlM
KEY_ALIASES = {..., "enter": "c-m", ...}
# keys.py — "shift" 단독 키는 존재하지 않음 (ShiftLeft 등 조합 키만 존재)
# key_binding/key_bindings.py — _parse_key("shift") → Keys("shift") ValueError
#   → len("shift") != 1 → ValueError("Invalid key: shift")  ← v1.1: shift 단독 바인딩 불가 근거

# bindings/basic.py — multiline Enter = newline
@handle("enter", filter=insert_mode & is_multiline)
def _newline(event: E) -> None:
    event.current_buffer.newline(copy_margin=not in_paste_mode())

# bindings/emacs.py — 단일 라인 Enter = accept-line (accept_handler 경유)
handle("enter", filter=insert_mode & is_returnable & ~is_multiline)(
    get_by_name("accept-line")
)

# application/application.py — 컨트롤 kb가 기본 kb보다 우선
key_bindings.append(self.app._default_bindings)
key_bindings = key_bindings[::-1]

# key_processor.py — eager 매칭 우선 + matches[-1] 호출
eager_matches = [m for m in matches if m.eager()]
if eager_matches:
    matches = eager_matches
...
self._call_handler(matches[-1], key_sequence=buffer[:])

# input/win32.py — Windows 일반 Enter → ControlM(enter), Ctrl+Enter → [Escape, ControlJ]
#   (v1.1 정정: ControlM이 아님 — §2.3 참조)
# widgets/base.py — TextArea 생성자에 key_bindings 파라미터 없음 (3.0.x)
```

---

## 8. 참고

- 사용자 재현: Windows 11 cmd, `agent-augury` (console_scripts), saved model config
- 관련 기존 이슈: `TUI_IMPLEMENTATION_REVIEW.md` P0(Ctrl+D 데드락) — **이미 수정
  완료 확인** (`input_bar.py` c-d 바인딩에 `on_quit()` 호출 존재, agent-3 확인).
  V4로 회귀 방지 테스트 추가.
- 검증: agent-1 · agent-2 · agent-3 · agent-4 4인 독립 정적 분석 완전 수렴
  (thread-1). v1.1은 agent-3의 구현 디테일 재검증(shift 키 부재 / win32 Ctrl+Enter
  매핑) 반영.
- agent-4 보조 문서 `TUI_ENTER_SUBMIT_FIX_DESIGN_agent4.md` — 통합본 하위 참조.
- §6 사용자 확정 반영 · 구현 진행.
