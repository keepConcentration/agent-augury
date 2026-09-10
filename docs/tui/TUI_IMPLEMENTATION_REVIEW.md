# agent-augury TUI 구현 리뷰 — 최종 통합본

> **대상:** `docs/tui/` 설계문서 8종에 따른 구현 전수 대조
> **검토자:** agent-2 · agent-3 · agent-4 (3인 완전 수렴) · 사용자 확정
> **날짜:** 2026-09
> **설계 기준:** `SESSION_TUI_REDESIGN.md` v2.5(통합 상위) + `INPUT_ROUTING_SLASH_UX_DESIGN.md` v0.5.1
> **구현 범위:** `src/agent_augury/tui/*`(10모듈) · `cli.py` · `session.py` · `server.py` · `config.py` · `agent/loop.py` · `agent/tools.py` · `channel/human_tui.py` · 관련 테스트

---

## 0. 한 줄 판정

> **✅ 잘 구현됨.** 설계 v2.5의 핵심 구조(단일 full-screen Application, render_event 순수 함수, router/commands/choice_panel/key_aliases 순수 분리, accept_handler Enter 제출, 안 A resolve args, REPL 단일 루프, 빈 입력 ignored, 평문 quit=plain)가 모두 반영됨.
> 단, **Ctrl+D 종료 데드락(P0) 1건**을 포함한 런타임 버그 1건과 문서/테스트 정합성 이슈 6건이 발견됨.

---

## 1. 설계 부합 확인 (잘된 점)

| 항목 | 설계 요구 (v2.5) | 구현 | 판정 |
|------|------------------|------|------|
| 렌더러 독점 | rich print 제거 → 단일 full-screen `Application` (alternate screen) | `tui/app.py` full_screen=True, LogBuffer → FormattedTextControl 단일 렌더 | ✅ |
| 로그 렌더링 | `render_event` 순수 함수 (rich record=True → ANSI 1블록) | `tui/renderer.py` 구현, `_log_step/_log_tool_event` 이관 | ✅ |
| 로그 버퍼 | deque(maxlen) + dirty 캐시 + `control()` | `tui/log_buffer.py` 일치 | ✅ |
| 입력줄 | `TextArea(multiline=True)` + `accept_handler` (Enter=제출), Shift/Esc+Enter=개행, Ctrl+C=드래프트 클리어, Ctrl+D=종료 | `tui/input_bar.py` 일치 (단, Ctrl+D 데드락 — §3) | ⚠️ |
| 선택지 패널 | `ConditionalContainer` + FIFO 큐 + 번호/원문 응답 + `/skip` + 대기 N개 배지 | `tui/choice_panel.py` 일치 (FIFO 확정) | ✅ |
| 상태바 | 4분할 Layout, 1초 폴링 + 이벤트 invalidate | `tui/status_bar.py` 일치 | ✅ |
| 라우팅 | 순수 함수, 빈 줄=ignored, `/`=슬래시, 질문 활성 시 질문 스레드+에이전트 회신, 평문=plain | `tui/router.py` v0.5.1 그대로 | ✅ |
| 슬래시 명령 | 레지스트리 5종+quit (/help /status /threads /clear /skip) | `tui/commands.py` 일치 | ✅ |
| 키 별칭 | hermes `pt_input_extras` 이식 (Shift/Ctrl+Enter, modifyOtherKeys, focus 시퀀스) | `tui/key_aliases.py` 일치 (setdefault 멱등) | ✅ |
| thread ref | 안 A — `loop.py` 1줄로 resolve된 args 전달 | `agent/loop.py` 적용 확인 (검증만) | ✅ |
| SSOT 불변 | server/session/tools/system_prompt 변경 없음 | 변경 없음 확인 | ✅ |
| 실행 모드 | REPL 단일 (--repl 플래그·1회 _run 제거) | `_run_repl` 유일 진입 + `--repl` SUPPRESS + `_run` 별칭 (하위호환) | ✅ (완화) |
| 빈 입력 | ignored (기존 "빈 줄=quit" 폐기) | router + `_run_repl_plain` 모두 ignored | ✅ |
| 비-TTY fallback | `isatty()` 분기 → render_event print 1줄 | `_want_fullscreen_tui()` + `_run_repl_plain` | ✅ |
| 전송 피드백 | 성공/실패 1줄 (마스킹 적용) | `app._send` → `ok human -> ...` (표기만 다름 — §5) | ✅ |
| 마스킹 | renderer 경로 유지 | `tui/renderer.mask_sensitive` + cli alias | ✅ |
| 테스트 | 순수 함수 단위 + PipeInput E2E + shim 호환 | `test_tui_router/commands` + `test_human_tui`(shim) + `test_repl` | ⚠️ 갭 (§6) |

---

## 2. 🔴 P0 — 런타임 버그 1건 (수정 필수)

### Ctrl+D 종료 데드락

- **위치:** `tui/input_bar.py` c-d 바인딩
- **원인:** `event.app.exit()`만 호출 → `cli._run_repl_tui`에 주입된 `on_quit` 콜백이 실행되지 않음. `quit_flag` 미설정 + `next_turn`에 None 미주입.
- **결과:** `session_loop()`가 `await next_turn.get()`에서 **영원히 블록** → 프로세스가 종료되지 않음 (hang).
- **대조:** `/quit`은 `handle_input` → `on_quit()` → `quit_flag.set()` + `next_turn.put_nowait(None)` → 정상 종료. **Ctrl+D만 경로가 다름.**
- **설계 위반:** `SESSION_TUI_REDESIGN.md` §6 키바인딩 표 "Ctrl+D = 종료" 미충족. v0.5 P10도 Ctrl+D를 명시적 종료 수단으로 규정.
- **검증:** agent-2(정적 분석)와 agent-4(독립 정적 분석) 일치. TTY 실측은 아직 미수행.

#### 수정안 (3인 합의)

**안 ① (권장):** `InputBar`에 `on_quit` 콜백을 주입하고, c-d 바인딩에서 `on_quit()` 호출 후 `event.app.exit()`.

```python
# tui/input_bar.py — c-d 바인딩 수정 예시
@kb.add("c-d")
def _quit(event: Any) -> None:
    if self._on_quit is not None:
        self._on_quit()      # quit_flag.set() + next_turn.put_nowait(None)
    event.app.exit()
```

→ `session_loop`가 None을 받아 정상 break → `tui.shutdown()` → `session.close()` 순서 보장.

**안 ②:** `_run_repl_tui`에서 `tui_task.done()` 감지 시 `next_turn`에 None 주입 (app 쪽 수정 없이 cli 쪽 보완).

---

## 3. 🟡 결정/문서 정합 필요 (6건)

| # | 항목 | 상세 | 권고 |
|---|------|------|------|
| 1 | **평문 quit/exit 불일치** | TUI router: 평문 `quit`/`exit` → `plain`(에이전트 전송) = v0.5.1 준수. 그러나 비TTY `_run_repl_plain`은 `_is_quit_token`으로 **종료** 처리 → 경로 간 동작 불일치. **근본 원인은 설계문서 자체 상충**: `SESSION_TUI_REDESIGN.md` §2.3("비-TTY는 quit/exit 텍스트로 종료") vs `INPUT_ROUTING_SLASH_UX_DESIGN.md` v0.5.1("평문 quit/exit=plain") | 설계문서부터 정리 후 구현 통일 (v0.5.1 준수 권장) |
| 2 | **Windows check_tty 미적용** | `--config` 직행 시 `_want_fullscreen_tui()`가 순수 `isatty()`만 체크 — console_scripts 래퍼에서 full-screen 진입 실패 가능. `check_tty()`(AttachConsole)는 위저드 경로에서만 호출. 설계 §7 "check_tty()/AttachConsole을 TUI 시작 전에 호출" 위반 | `_want_fullscreen_tui()` 내부에서 `wizard.check_tty()` 재사용 (위저드/--config 경로 통일) |
| 3 | **config human.tui 키 무시** | `config.py`가 `human` 섹션을 통째로 pass(무시). v2.5 §5 변경표는 "키 검증" 명시. REPL 기본화로 always-on이 의도된 것으로 보임 | 의도된 변경이면 설계 문서에 명시 (옵트인 개념 폐기 선언) |
| 4 | **README `--repl` 행** | CLI 옵션 표에 "REPL mode — keep conversation context..." 명시. 실제는 `argparse.SUPPRESS`(no-op). v2.5 "플래그 제거" 의도와 달리 하위호환 유지 | 행 제거 또는 "(deprecated, no-op)" 표기 |
| 5 | **README `--quiet` 설명 오류** | "only show final summary"라지만 `_run_repl_tui`는 quiet 시 summary도 출력 안 함(`if quiet: return`) | 설명 수정 (이벤트·summary 모두 생략) |
| 6 | **demo yaml 주석 오류** | `examples/human_tui_demo.yaml` 4번째 줄 `--interactive` — cli.py에 없는 플래그(`unrecognized arguments`로 실패). 5번째 문단 `human.interface: tui` 언급도 config.py가 human 섹션을 무시하는 구현과 불일치 | 주석을 `--demo`만 쓰도록 수정 + human.interface 언급 제거/갱신 |

---

## 4. 🟢 클린업 (사소)

- **dead code:** `cli._make_tui_adapter()` — full-screen 경로는 `SessionTUIApplication` 직접 사용. `test_wiring.py` 2건이 이걸 mock 중이므로 테스트도 함께 갱신.
- **renderer Console 재생성:** `render_event` 호출마다 `Console(record=True, file=io.StringIO())` 새 생성. 설계 §3.3은 모듈 레벨 `_style_console` 싱글턴 지정 → 성능상 사소하나 캐시 권장.
- **`_output_consumer` 가독성** (session.py): `and > or` 연산자 우선순위 의존 조건문. on_tool_event None 시 `read_resource`가 조용히 드롭되는 비대칭 존재 → 명시적 분기 권장.
- **전송 피드백 표기:** 구현 `ok human -> ...` vs 설계 `✓ human → ...` — 표기 통일.
- **`paste_mode=True` 미설정:** 설계 §3.4 명시 항목 — 브래킷 페이스트 UX 개선 위해 설정 권장.
- **renderer width=100 고정:** 비TTY에서 기존 rich 출력과 줄바꿈 미세 차이 가능 (agent-2).

---

## 5. 테스트 갭

| 갭 | 상세 | 권고 |
|----|------|------|
| **app.py 헤드리스 E2E 부재** | `test_human_tui.py`는 전부 레거시 `HumanTUIAdapter` 기준 — `SessionTUIApplication` 자체 테스트 없음. 설계 §8 T1~T15 미반영 | `PipeInput` + `handle_input`/`create_background_task` stub으로 app.py 단위에서 Ctrl+D·/quit·일반 입력 경로 검증 → **Ctrl+D 데드락 같은 회귀를 `_run_repl_tui` 전체 없이 조기 포착** |
| `tui/__main__.py` 부재 | 설계 §3.1 신규 패키지 목록에 명시 | 추가 (테스트/데모용 PipeInput 헤드리스 실행) |
| `test_tui_status_bar.py` 부재 | 설계 §8.1 단위 테스트 목록에 명시 | 추가 (snapshot fake 주입) |

---

## 6. 긍정적 차이 (유지 권장)

- **renderer ask_user 분기** (agent-4): 설계 골격은 `tool==ask_user → None`(D11)이지만, 구현은 renderer에서 로그 출력을 유지하고 cli의 `on_tool_event`가 TUI 모드에서 ask_user를 패널로 우회 → **TUI=패널 1회, 비TTY=로그 1회**로 설계보다 정확.
- **`[ask-user]` prefix 스킵이 비TUI에도 적용** (agent-3): 중복 출력 감소 — 실용적. (VERIFICATION §5 표와 미세 차이지만 오히려 개선)
- **`--repl` SUPPRESS + `_run=_run_repl` 별칭 유지** (agent-2): 설계("제거")보다 하위호환 안전한 선택. 구 스크립트가 깨지지 않음.

---

## 7. 권고 우선순위 (3인 합의)

| 순위 | 작업 | 분류 |
|------|------|------|
| **P0** | Ctrl+D 데드락 수정 (InputBar on_quit 주입 — 안 ①) | 버그 |
| **P1** | app.py 헤드리스 E2E 테스트 추가 (Ctrl+D·/quit·일반 입력) | 테스트 |
| **P2** | 평문 quit/exit 정책 통일 + 설계문서 상충 정리 (§2.3 vs v0.5.1) | 문서+코드 |
| **P3** | Windows check_tty 재사용 + README(--repl/--quiet) + demo yaml 주석 + config human.tui 정책 명시 | 문서 |
| **P4** | 클린업: dead code 제거, renderer Console 싱글턴, `_output_consumer` 리팩터, paste_mode, 피드백 표기 | 코드 품질 |

---

## 8. 참고

- `SESSION_TUI_REDESIGN.md` v2.5 — 통합 상위 설계
- `INPUT_ROUTING_SLASH_UX_DESIGN.md` v0.5.1 — 라우팅/슬래시/선택지/키바인딩
- `TUI_ALWAYS_ON_INPUT_DESIGN.md` v0.6 — **구식(B레벨)** — 참고용
- `VERIFICATION_TUI_INTEGRATION.md` — 안 A 적용 검증
- `LIBRARY_RESEARCH_prompt_toolkit.md` / `TUI_INPUT_BAR_DESIGN.md` — 라이브러리 조사
- 구현: `src/agent_augury/tui/*`, `cli.py`, `session.py`, `config.py`, `channel/human_tui.py`
