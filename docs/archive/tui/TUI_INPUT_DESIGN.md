# agent-augury 상시 입력창(TUI) — 통합 설계 문서 (초안 → 최신 합의 갱신본)

> **Task:** agent-augury에 상시 입력창(TUI) 도입 — ① 세션 도중 사용자가 언제든
> inbox에 메시지를 넣을 수 있게, ② `ask_user` 선택지가 에이전트 대화 로그에 밀려
> 사라지지 않도록 고정(pin)하기.
> **Date:** 2026-09 · 작성: agent-2 (통합 초안) · 검토: agent-1, agent-3, agent-4
> **⚠️ 상위 문서:** `TUI_ALWAYS_ON_INPUT_DESIGN.md` (agent-3 통합본, v0.4)가 최종
> 통합 문서입니다. 이 문서는 agent-2 초안을 **D8 + 안 A 최신 합의로 갱신**한 것이며,
> 통합본의 보조/근거 문서로 사용하세요.
> **Rev:** v2.0 — D8(기존 tool 이벤트) + agent-4 보강 2건 + agent-1 thread ref 검증(안 A) 반영

---

## 0. 한 줄 결론

> **prompt_toolkit 기반 "상단 로그(rich 유지) + 하단 고정 선택지 패널(bottom_toolbar)
> + 하단 상시 입력줄(PromptSession)" 구조.**
> ask_user 식별은 **기존 `tool` 이벤트**(변경 0), thread ref는 **loop.py 1줄**(안 A),
> config는 `human.tui` 옵트인(기존 동작 100% 호환).

---

## 1. 배경과 목표

### 1.1 문제 정의

기존 HITL(USER_INTERVENTION_DESIGN.md v1.1)은 이미 구현됨:
- `server.register_human()/human_send()`, `agent/tools.py::ask_user`,
  `cli.py --interactive` + `_human_input_loop`(`run_in_executor`로 `input()`)

3가지 문제:
| # | 문제 | 원인 |
|---|------|------|
| P1 | 입력 프롬프트가 로그와 섞임 | `input()` 단일 라인 에디터, 하단 고정 없음 |
| P2 | `ask_user` 선택지가 로그에 밀려 사라짐 | `ask_user`는 `send_message(mentions=["human"])`으로 구현 — 별도 이벤트 아님 |
| P3 | 멀티라인/히스토리/한글 IME UX 부족 | `input()`의 한계 |

### 1.2 목표

1. 세션 도중 언제든 inbox에 메시지 주입 (상시 입력창)
2. `ask_user` 질문/선택지를 하단 고정 패널에 표시 (로그에 밀리지 않음)
3. 멀티라인/히스토리/한글 IME/브래킷 페이스트
4. 비침습 — `MessageServer`(SSOT)/`session.py`/`server.py`/`tools.py` 변경 최소화

---

## 2. 라이브러리 조사 요약 (상세: `LIBRARY_RESEARCH_prompt_toolkit.md`, `TUI_INPUT_BAR_DESIGN.md`)

| 차원 | prompt_toolkit | textual | rich-only |
|------|----------------|---------|-----------|
| 모델 | 인라인 프롬프트 + 레이아웃 | **full-screen** | 인라인 출력만 |
| 기존 rich 로그 공존 | ✅ patch_stdout/bottom_toolbar | ❌ full-screen 전환 시 로그 안 보임 | ✅ |
| 선택지 고정 | ✅ bottom_toolbar | ✅ OptionList (강력) | ❌ |
| 상시 입력창(편집/히스토리/멀티라인) | ✅ PromptSession | ✅ Input/TextArea | ❌ |
| asyncio | ✅ `prompt_async()` / `run_async()` | ✅ `App.run_async()` / `@work` | ⚠️ Live만 |
| Windows | ✅ win32 네이티브 (ConHost 포함) | ⚠️ **ConPTY 필수** (Win10 1809+) | ✅ |
| 한글 IME | ✅ 입력 이벤트 기반 | ⚠️ 터미널 의존적 | ⚠️ |
| 의존성 | 경량 (wcwidth/pygments) | rich 내장 (상대적으로 큼) | 0 |
| 적합 시나리오 | **로그 유지 + 입력창/패널 추가** | **전면 개편 (TUI 재설계)** | 입력 불필요 관측 |

**판정:** prompt_toolkit 1안 (v1.0) / textual 2안 (v2.0 전면 개편 옵션) / rich-only ❌

---

## 3. 핵심 설계 결정 (최신 합의)

### D1: 입력층 — `PromptSession.prompt_async()` (v1.0)
- `_human_input_loop`의 `run_in_executor(input())` 제거, asyncio 네이티브.
- `multiline=True` + `FileHistory` + `enable_open_in_editor` — 멀티라인/히스토리.

### D2: 로그 공존 — `patch_stdout()` (v1.0 보조)
- rich `Console` 출력과 공존. bottom_toolbar는 프롬프트 재렌더링과 분리되어
  깜빡임 제한적 (agent-2 검증).

### D3: 선택지 고정 — `bottom_toolbar` + PendingQuestions 큐
- `tool=="ask_user"` 이벤트 → pinned 상태 갱신 → toolbar에 고정 표시.
- 다중 질문은 큐로 순차 처리, "대기 N개" 배지.

### D4: ask_user 식별 — D8 확정 (기존 tool 이벤트, 변경 0)
- **철회:** 별도 `_emit_event({"type":"ask_user"})` C안 — 불필요.
- **확정:** `session.py`의 `on_tool_call` 람다가 이미
  `_emit_event({"type":"tool","tool":"ask_user","args":{question,options,thread}})` 발생.
- `cli._log_tool_event`에 `tool=="ask_user"` 분기 이미 존재.
- **tools.py/server.py 변경 0.**

### D5: thread ref 해석 — 안 A (loop.py 1줄, v1.0 필수)
- **블로커:** `loop.py` `on_tool_call(self.agent_id, call.name, call.arguments, result)`가
  resolve 전 원본 전달 → `$thread:0`이 cli에 도착 → `human_send` 시 KeyError.
- **해결 (안 A):** `on_tool_call(self.agent_id, call.name, args, result)` — resolve된 args.
- session.py lambda 파라미터명 `args` 그대로 → 시그니처 불변.
- 응답 라우팅: `human_send(thread_id=args["thread"], mentions=[질문 에이전트])` (정밀 회신).

### D6: config — `human.tui` 옵트인
```yaml
human:
  id: human
  interface: cli        # 기존: cli | discord | file
  tui: prompt_toolkit   # 신규(선택): prompt_toolkit | textual | none(기존 input)
  pin_options: true     # 신규(선택): ask_user 선택지 하단 고정 여부 (기본 true)
  history_file: ~/.agent-augury/human_history.txt
```
- `tui` 없으면 기존 `input()` 동작 100% 호환 (옵트인, 마이그레이션 0).

### D7: 로그 중복 방지 (agent-4 보강 ② + agent-1 검증)
- TUI 모드 한정: `send_message` 이벤트의 `[ask-user]` prefix → 로그 스킵.
- `tool(ask_user)` 이벤트 → `👤 asks:` 로그 대신 pinned 패널 표시 (로그 억제).
- `pin_options: false`면 기존 로그 유지. 비TUI 모드는 현행 그대로 (회귀 0).
- **이벤트 순서 (skip-then-show):** send_message 이벤트(스킵) → tool 이벤트(pinned
  표시) — 같은 루프 + FIFO 큐로 구조 보장.

---

## 4. 아키텍처 (v1.0)

```
┌────────────────────────────────────────────────────────────┐
│                      사용자 (터미널)                        │
│   ┌──────────────────────────────────────────────────┐     │
│   │  상단: 기존 rich 로그 스트림 (스크롤)              │     │
│   ├──────────────────────────────────────────────────┤     │
│   │  고정: ask_user 선택지 패널 (bottom_toolbar)      │     │
│   ├──────────────────────────────────────────────────┤     │
│   │  하단: 상시 입력창  ❯ _  (PromptSession)         │     │
│   └──────────────────────────────────────────────────┘     │
└──────────────────────────┬─────────────────────────────────┘
                           │ prompt_async() / human_send()
                           ▼
              ┌─────────────────────────┐
              │  cli (표시/입력 계층)    │
              │  HumanTUIAdapter        │
              └───────────┬─────────────┘
                          ▼
              ┌─────────────────────────┐
              │  MessageServer (SSOT)   │  ← 변경 없음
              │  · human_send           │
              │  · agent inbox push     │
              └───────────┬─────────────┘
                          │ [radio] 흡수
                          ▼
              ┌─────────────────────────┐
              │  AgentLoop (step)       │  ← loop.py 1줄 (안 A)
              │  · ask_user 도구         │  ← 변경 없음
              └─────────────────────────┘
```

**변경 파일 (최종 합의, 4개 + 신규 2개):**

| 파일 | 변경 |
|------|------|
| `agent/loop.py` | **1줄:** `on_tool_call(self.agent_id, call.name, args, result)` — resolve된 args (안 A) |
| `cli.py` | PromptSession 입력 + pinned 패널 + `[ask-user]` 로그 스킵 + ask_user 로그 억제 |
| `config.py` | `human.tui` / `pin_options` / `history_file` 검증 |
| `channel/human_tui.py` | **신규:** HumanTUIAdapter (입력 루프 + bottom_toolbar 선택지 패널) |
| `pyproject.toml` | prompt-toolkit 의존성 추가 |
| `examples/human_tui_demo.yaml` | **신규:** TUI 데모 (fake 백엔드, ask_user 포함) |
| `tests/test_human_tui.py` | **신규:** PipeInput 헤드리스 테스트 |
| `server.py` / `session.py` / `tools.py` / `system_prompt.py` | **변경 없음** |

---

## 5. v1.0 통과 기준 (자동 검증)

```python
# 1) tool 이벤트 경로 (D8) + thread ref 해석 (안 A)
assert event["type"] == "tool" and event["tool"] == "ask_user"
assert event["args"]["question"] == "DB는 뭘 쓸까?"
assert event["args"]["options"] == ["postgres", "mysql"]
assert event["args"]["thread"].startswith("thread-")   # resolve 후 실제 id

# 2) TUI 모드 중복 스킵
# send_message 이벤트(content "[ask-user]") → 로그 생략
# tool(ask_user) 이벤트 → pinned 패널 갱신 (로그 출력 억제)

# 3) 응답 라우팅: ask_user의 thread로 human_send(mentions=[질문 에이전트])
assert agent 대화에 "from human" 포함

# 4) 비TUI 회귀: human.tui 미설정 → 기존 input() 경로 + 기존 출력 그대로
#    (test_human_in_the_loop.py 등 전체 통과 유지)

# 5) $thread:0 ref E2E: ask_user(thread="$thread:0") → cli가 resolve된 thread로
#    human_send → KeyError 없음
```

---

## 6. 리스크

| 리스크 | 완화 |
|--------|------|
| patch_stdout 깜빡임 | bottom_toolbar 분리 갱신 → 제한적. 심하면 레벨 C |
| prompt_async 종료 지연 | `ps.app.exit()` + PendingQuestions/pinned 초기화 (agent-1) |
| ask_user 중복 표시 | `[ask-user]` prefix 스킵 + `👤 asks:` 로그 억제 — 1번만 표시 |
| **`$thread:0` ref → KeyError** | **안 A (loop.py 1줄)** — v1.0 필수. 폴백: 최근 활성 스레드 |
| on_tool_call args 변경 회귀 | `test_wiring.py`/`test_parallel.py` resolve 전/후 차이 확인 |
| 영속화 재생 시 ask_user 식별 불가 | v1.1 `Message.kind` + ALTER TABLE (보류) |

---

## 7. 참고 자료

- **상위 통합:** `TUI_ALWAYS_ON_INPUT_DESIGN.md` (agent-3, v0.4)
- **검증:** `VERIFICATION_TUI_INTEGRATION.md` (agent-1)
- **라이브러리:** `LIBRARY_RESEARCH_prompt_toolkit.md` (agent-2, v2.2),
  `TUI_INPUT_BAR_DESIGN.md` (agent-4)
- `IdeaProjects/agent-augury/src/agent_augury/{cli,session,config}.py`
- `IdeaProjects/agent-augury/src/agent_augury/agent/{loop,tools}.py`
