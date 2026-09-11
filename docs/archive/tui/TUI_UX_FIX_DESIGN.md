# agent-augury — TUI/CLI UX 수정 설계안

> **Task:** 사용자가 보고한 TUI/CLI UX 문제 3건의 원인 분석 및 수정 설계
> **Date:** 2026-09 · agent-1 (초안·원인 분석) · 검토/기여: agent-3 (원인 분석, 로그 백업 제안) · agent-4 (도구 측 연계)
> **Scope:** 설계 문서 (구현 코드 아님). 도구 확장 설계(`AGENT_TOOLS_EXPANSION_DESIGN.md`)와는 별개 트랙.
> **Status:** v1.1 초안 — agent-3 검토 대기
> **작성 이력:** agent-1/agent-3이 동시에 초안을 작성하는 상황이 발생 → 현재 디스크 버전은 agent-1 작성본이며, agent-3의 "로그 백업(즉효)" 아이디어를 §4.2에 반영해 통합함. 이후 수정은 agent-3 검토 후 반영.

---

## 1. 배경: 무엇이 문제인가

사용자가 세션 도중 TUI/CLI UX 문제 3건을 보고했다.

| # | 증상 | 심각도 |
|---|------|--------|
| ① | Nous OAuth 인증 완료 후에도 `Verification URL: https://portal.nousresearch.com/manage-subscription` 메시지가 **입력창에 잔존** | 높음 — 화면 오염 |
| ② | 작업 내역이 화면에 쌓일 때마다 **스크롤이 최신(아래)으로 강제 점프** → 가만히 놔두면 작업 내역이 **한 줄만 보임** | 높음 — 관측 불가 |
| ③ | ask_user로 **긴 선택지**(여러 옵션)를 제시하면 **잘려서 전체를 확인할 방법이 없음** | 중간 — 결정 불가 |

①은 인증 UX, ②는 로그 follow 정책, ③은 선택지 패널 렌더링의 결함이다. 모두 **full-screen TUI(`prompt_toolkit` Application)** 경로에서 발생하며, 원인이 코드에 명확히 존재한다.

---

## 2. 이슈 ① — OAuth 인증 후 "Verification URL" 잔존

### 2.1 재현 경로 (코드 근거)

사용자 증상은 **TUI 실행 중 첫 모델 호출 시점에 발생하는 OAuth 인증**이 원인이다. (위저드 단계 인증은 TUI 시작 전이라 alternate screen 전환 시 지워진다.)

```
cli.py _run_repl_tui
  → session.run(initial_prompt=task)
  → agent.step()                                   # agent/loop.py
  → backend.complete(...)                          # nous_portal_oauth.py
  → NousPortalOAuthBackend.get_access_token()
  → _authenticate()
  → DeviceCodeFlow(config, http_client_factory=...).authenticate(
        on_user_code=self._on_user_code, open_browser=True)
```

**핵심 결함:** `backends_factory.build_backend()`는 `NousPortalOAuthBackend(...)` 생성 시 **`on_user_code`를 전달하지 않는다** (`backends_factory.py`):

```python
if btype == "nous_oauth":
    return NousPortalOAuthBackend(
        model=spec["model"],
        base_url=spec.get("base_url", "https://inference-api.nousresearch.com/v1"),
        token_store=token_store,
    )
```

따라서 `NousPortalOAuthBackend._on_user_code = None`이 되고, `oauth.py::DeviceCodeFlow.authenticate()`의 **else 분기**가 실행된다 (`auth/oauth.py`):

```python
def authenticate(self, on_user_code=None, open_browser=True):
    device = self.request_device_code()
    if on_user_code:
        on_user_code(device.user_code, device.verification_uri)
    else:
        print(f"\nTo authenticate, enter code: {device.user_code}")
        print(f"Verification URL: {device.verification_uri}")   # ← full-screen TUI 위에 직접 flush
    ...
```

이 `print()`는 **full-screen TUI(alternate screen)가 이미 활성화된 상태에서 터미널에 직접 출력**되어 화면이 깨지고, 인증 완료 후에도 해당 줄이 잔존한다.

> 위저드 경로(`wizard.py::_run_nous_oauth_device_code`)는 `on_user_code` 콜백으로 print를 하고 TUI 시작 전에 실행되므로 **이 버그의 원인이 아니다.** TUI 실행 중 인증 경로만 고치면 된다.

### 2.2 수정 설계

**목표:** TUI 실행 중 OAuth 인증 안내가 full-screen 화면을 깨지 않게 한다.

#### 옵션 A (권장): `on_user_code` 콜백 배선 — TUI 오버레이/로그로 안내

1. `Session.from_config`에서 `NousPortalOAuthBackend` 생성 시 `on_user_code` 콜백을 주입한다.
2. 콜백은 TUI가 있으면 **선택지 패널(choice panel)과 같은 핀 고정 오버레이** 또는 **로그 버퍼**에 안내를 추가한다.
   - `SessionTUIApplication.append_text(f"To authenticate, enter code: {code}")`
   - `SessionTUIApplication.append_text(f"Verification URL: {uri}")`
3. TUI가 없으면(비-TTY/REPL plain) 기존 `print()` fallback 유지.

**연결 지점:**
- `backends_factory.build_backend(spec, token_store=..., on_user_code=...)` — 파라미터 추가
- `session.py::Session.from_config` — build_backend 호출 시 콜백 전달 (CLI에서 받은 `on_auth_code` hook 전달)
- `cli.py::_run_repl_tui` — `SessionTUIApplication.append_text`를 auth 안내 표시에 연결

#### 옵션 B (최소 수정): print → logging

- `oauth.py`의 else 분기를 `logger.info(...)`로 변경.
- 단점: TUI 화면에는 안내가 전혀 안 보여 사용자가 "코드 입력하러 어디 가야 하나"를 모름. 브라우저가 자동으로 열리므로(open_browser=True) 실사용은 가능하나 UX 열화.

#### 옵션 C (구조적): 인증 상태 전용 위젯

- `SessionTUIApplication`에 `auth_overlay`(일시적 오버레이) 추가 — ask_user 툴바와 유사하게 하단에 고정, 인증 완료 이벤트(`TokenStore` 갱신) 시 자동 제거.
- 가장 깔끔하지만 구현량 최대. **v1.1 후보**.

**권장 조합:** v1.0은 **옵션 A** (콜백 배선 + 로그/오버레이 표시), 옵션 B는 fallback, 옵션 C는 v1.1.

### 2.3 통과 기준

```
시나리오: nous_oauth 백엔드 + full-screen TUI + fake 모델 호출.
- build_backend에 on_user_code 콜백 전달 확인
- TUI 실행 중 인증 발생 시 print()가 호출되지 않음 (logging/오버레이 경로만)
- "Verification URL: ..."이 TUI 로그/오버레이에 표시되고 alternate screen이 깨지지 않음
- 인증 완료 후 잔존 텍스트 없음

단언:
  assert build_backend(nous_oauth, on_user_code=cb)._on_user_code is cb
  assert DeviceCodeFlow.authenticate 호출 시 else print 분기 미실행 (콜백 전달 시)
  assert TUI 로그에 auth 안내 포함, 화면 잔존 없음
```

---

## 3. 이슈 ② — 새 출력 시 스크롤이 최신으로 점프 (한 줄만 보임)

### 3.1 원인 (코드 근거) — 확정

`tui/app.py`의 로그 follow 정책이 원인이다.

1. `SessionTUIApplication.__init__`에서 `self._log_follow = True` **기본값** (`app.py`).
2. `append_event()` / `append_text()` → `_follow_log_tail()`이 follow 상태면 `vertical_scroll = line_count - 1`로 **강제 점프** (`app.py`):

```python
def _follow_log_tail(self) -> None:
    if not self._log_follow:
        return
    try:
        self.log_window.vertical_scroll = max(
            0, self.log_buffer.line_count() - 1
        )
    except Exception:
        pass
```

3. `_log_follow`를 `False`로 만드는 경로는 **PgUp / Alt+↑ 키바인딩뿐** (`_build_app_kb()`).
4. 그런데 TUI는 `mouse_support=True`(`app_kwargs`)인데 **마우스 휠 스크롤 이벤트 핸들러가 없다.** 사용자가 마우스로 올려봐도 `_log_follow`는 여전히 `True` → 다음 출력이 오면 **다시 맨 아래로 점프**한다. 사용자 증상("가만히 놔두면 한 줄만 보임")과 정확히 일치.

> `commands.py::/help`에는 "Mouse wheel - scroll log"라고 적혀 있지만, 실제로는 마우스 휠이 `_log_follow`를 해제하지 않아 follow 상태가 유지된다. 문서-구현 불일치.

### 3.2 수정 설계

**목표:** 사용자가 스크롤하면 follow가 해제되고, 새 출력이 와도 현재 위치를 유지한다. follow 상태를 시각적으로 알 수 있어야 한다.

1. **마우스 휠 스크롤 이벤트 핸들러 추가** (`prompt_toolkit.MouseEventType.SCROLL_UP/SCROLL_DOWN`):
   - 휠 업 → `vertical_scroll` 감소 + `_log_follow = False`
   - 휠 다운 → `vertical_scroll` 증가 + 끝 도달 시 `_log_follow = True` (PgDn과 동일 규칙)
   - `app.py`의 `_build_app_kb()`에 `@kb.add("scroll-up")` / `@kb.add("scroll-down")` 바인딩 추가 (prompt_toolkit은 `Keys.ScrollUp/ScrollDown` 지원)

2. **follow 토글 키 `F`** (권장):
   - `F` 키 → `_log_follow` 토글. 상태 표시줄에 표시.

3. **상태 표시줄에 follow 상태 표시** (`status_bar.py`):
   - `FOLLOW` / `SCROLL` 인디케이터: `threads=3 · msgs=12 · gate=OPEN · phase=P3 · FOLLOW` 등.

4. **(선택, v1.1) "새 출력 N줄" 배지**:
   - `_log_follow=False` 상태에서 새 출력이 쌓이면 로그 하단에 `(새 출력 N줄 — PgDn 또는 F로 따라가기)` 표시. 사용자가 스크롤 중일 때 새 정보가 왔음을 알림.

5. **(선택, v1.1) sticky header** — 로그 최상단에 고정 헤더(세션 상태 요약)를 두어 스크롤과 무관하게 항상 표시.

### 3.3 통과 기준

```
시나리오: 30줄 로그 + follow 상태에서 새 출력 추가.
- 마우스 휠 업 → vertical_scroll 감소, _log_follow = False
- 이후 append_text() → vertical_scroll 불변 (점프 없음)
- F 키 → follow 토글, 상태 표시줄에 FOLLOW/SCROLL 표시
- 휠 다운으로 끝 도달 → _log_follow = True 복귀

단언:
  assert wheel_up 후 _log_follow is False
  assert follow=False에서 append 후 vertical_scroll 유지
  assert F 토글 후 status_bar에 FOLLOW/SCROLL 표시
```

---

## 4. 이슈 ③ — 긴 선택지 잘림 / 전체 확인 불가

### 4.1 원인 (코드 근거)

`tui/choice_panel.py`의 렌더링 상한이 원인이다.

1. `ChoicePanel.line_count(max_lines=8)`이 옵션 수를 **최대 8줄로 캡** (`choice_panel.py`):

```python
MAX_PANEL_LINES = 8

def line_count(self, max_lines: int = MAX_PANEL_LINES) -> int:
    pq = self.active
    if pq is None:
        return 0
    return min(max_lines, 1 + len(pq.options) + (1 if len(self.queue) > 1 else 0))
```

2. `app.py::_choice_height()`가 `Dimension(min=1, max=8, preferred=line_count(8))` — 패널 높이 상한 8줄.
3. 옵션이 8개를 넘거나 **개별 옵션 텍스트가 길어 wrap되면** `wrap_lines=True`인 Window가 줄바꿈하지만, **상한(8줄) 초과분은 화면 밖으로 잘린다.**
4. 선택지 패널에는 **스크롤 핸들러가 없다** (로그 Window만 PgUp/PgDn/휠 지원). → 잘린 옵션을 확인할 방법 자체가 없다.

추가 구조적 요인: `agent/tools.py::ask_user`가 옵션을 content에 `(옵션: A / B / C)` 형태로 **단일 문자열**로 붙여 전송하므로, 옵션 수·길이가 늘면 메시지 자체도 길어진다.

### 4.2 수정 설계

**목표:** 긴 선택지에서도 모든 옵션을 확인하고 선택할 수 있어야 한다.

1. **로그 백업 (즉효 — agent-3 제안, 최우선 적용)**
   - `ChoicePanel.on_ask_user()` 시 질문+전체 옵션을 **로그 버퍼에도 append**한다 (`app.py::on_ask_user`에서 `append_text`).
   - 사용자는 패널이 잘려도 **로그를 스크롤(PgUp/휠 — 단, 이슈 ② 수정 후)하면 전체 옵션을 확인**할 수 있다.
   - 구현이 가장 단순하고 즉시 효과. v1.0-1에서 ②와 함께 적용.

2. **선택지 패널 스크롤 지원** (핵심):
   - `ChoicePanel`에 `scroll_offset` 상태 추가.
   - 패널이 8줄을 초과하는 옵션을 가질 때 PgUp/PgDn(또는 휠)로 옵션 영역을 스크롤.
   - 표시: `(옵션 12개 중 1~7 표시 — ↑↓로 더 보기)` 같은 인디케이터.
   - `app.py` 키바인딩: 패널 활성 시 PgUp/PgDn이 로그 대신 **선택지 패널**을 스크롤하도록 분기.

3. **접힘/펼침 토글 (선택, v1.1)**:
   - 긴 옵션 텍스트는 기본 `…` 접기, `Tab` 또는 `E` 키로 전체 펼침.

4. **ask_user 옵션 구조화 (장기, 도구 설계와 공동)**:
   - `tools.py::ask_user`가 옵션을 **별도 구조화 필드**로 전달 (현재는 content 단일 문자열). `AGENT_TOOLS_EXPANSION_DESIGN.md` 부록과 연계해 v1.1 후보.
   - 에이전트 프롬프트에 "옵션은 최대 5개, 각 옵션은 한 줄 이내로" 지침 추가로 **근본적으로 잘림 방지** (즉시 적용 가능).

5. **즉시 적용 가능한 완화**:
   - `MAX_PANEL_LINES`을 8→12로 상향 (임시).
   - `system_prompt.py`의 `ask_user` 설명에 "옵션은 5개 이하, 간결히" 지침 추가.

### 4.3 통과 기준

```
시나리오: 10개 옵션의 ask_user + TUI.
- (로그 백업) on_ask_user 시 질문+전체 옵션이 로그 버퍼에 기록됨 → PgUp으로 전체 확인 가능
- 패널에 옵션 1~7 표시, "옵션 10개 중 1~7" 인디케이터
- PgDn → 옵션 4~10 표시 (스크롤)
- 번호 입력으로 잘린 옵션(예: 10)도 정상 선택됨 (router는 이미 1-based 전체 인덱스 해석)
- 개별 옵션 길이가 길어도 wrap되어 전체 가시

단언:
  assert on_ask_user 후 log_buffer에 전체 옵션 포함
  assert panel.scroll_offset 증가/감소 동작
  assert "옵션 N개 중" 인디케이터 표시
  assert 10번 옵션 선택 시 content == options[9] (기존 router 동작 유지)
```

---

## 5. 구현 로드맵

| 단계 | 범위 | 산출물 | 통과 기준 |
|------|------|--------|-----------|
| **v1.0-1** | ② 마우스 휠 follow 해제 + F 토글 + 상태 표시 + **③ 로그 백업(즉효)** | `tui/app.py`, `tui/status_bar.py` | §3.3, §4.3(로그 백업) |
| **v1.0-2** | ① on_user_code 콜백 배선 (A) | `backends_factory.py`, `session.py`, `cli.py`, `auth/oauth.py`(fallback) | §2.3 |
| **v1.0-3** | ③ 선택지 패널 스크롤 + 인디케이터 | `tui/choice_panel.py`, `tui/app.py` | §4.3 |
| **v1.1** | ② 새 출력 N줄 배지 / sticky header, ③ 접힘·펼침, ① 인증 오버레이 위젯, ask_user 구조화 | `tui/`, `agent/tools.py` | UX 개선 확인 |

---

## 6. 테스트 계획

| 테스트 파일 | 범위 |
|-------------|------|
| `tests/test_tui_app.py` (수정) | 마우스 휠 follow 해제, F 토글, 선택지 패널 스크롤, 상태 표시줄 FOLLOW/SCROLL, 로그 백업 |
| `tests/test_tui_choice_panel.py` (신규) | scroll_offset, 인디케이터, 8줄 초과 옵션 렌더, 로그 백업 |
| `tests/test_status_bar.py` (수정) | FOLLOW/SCROLL 인디케이터 |
| `tests/test_backends.py` (수정) | build_backend on_user_code 콜백 전달 |
| `tests/test_oauth.py` (수정) | 콜백 전달 시 print 미실행 (capture) |
| `tests/test_human_tui.py` (수정) | ③ 옵션 10개 선택 E2E |

---

## 7. 리스크와 대가

| 리스크 | 완화 |
|--------|------|
| 마우스 휠 바인딩이 로그/선택지 패널 양쪽에서 충돌 | 패널 활성 여부(`choice_panel.has_pending`)에 따라 분기 — 로그 스크롤 vs 패널 스크롤 |
| OAuth 콜백이 여러 백엔드에서 중복 호출 | 기존 per-provider auth lock 재사용, 콜백은 1회만 표시 |
| 선택지 스크롤이 번호 입력과 혼동 | 번호 입력은 그대로 1-based 전체 인덱스 유지 (표시 순서와 무관) |
| print→logging 전환 시 안내 소실 | 옵션 A(콜백→TUI 표시)를 기본으로, logging은 fallback만 |
| 로그 백업으로 로그가 지저분해짐 | 옵션 원문을 1블록으로 append, `mask_sensitive` 적용, 다음 질문과 구분선 |

---

## 8. 변경 파일 요약

```
src/agent_augury/
  auth/oauth.py              # else 분기 print → logging fallback (옵션 B), 콜백 우선 유지
  backends_factory.py        # build_backend에 on_user_code 파라미터 추가 (①)
  session.py                 # build_backend 호출 시 on_user_code 전달 (①)
  cli.py                     # TUI auth 안내 hook 연결 (①)
  tui/app.py                 # 마우스 휠 follow 해제, F 토글, 선택지 패널 스크롤 분기, 로그 백업 (②③)
  tui/choice_panel.py        # scroll_offset, 인디케이터, MAX_PANEL_LINES 상향, 로그 백업 훅 (③)
  tui/status_bar.py          # FOLLOW/SCROLL 인디케이터 (②)
  agent/system_prompt.py     # ask_user 옵션 간결 지침 (③ 근본 완화)
tests/
  test_tui_app.py            # (수정)
  test_tui_choice_panel.py   # (신규)
  test_status_bar.py         # (수정)
  test_backends.py           # (수정)
  test_oauth.py              # (수정)
  test_human_tui.py          # (수정)
```

---

## 9. 결론

3건 모두 **full-screen TUI(`prompt_toolkit`)의 상태 관리 결함**에서 비롯된다:

- ①은 **OAuth 안내가 TUI 밖으로 직접 print**되는 배선 문제 → 콜백 배선으로 해결.
- ②는 **로그 follow 상태가 사용자 의도와 동기화되지 않는** 문제 → 마우스 휠/F 토글/상태 표시로 해결.
- ③은 **선택지 패널의 렌더링 상한(8줄) + 스크롤 부재** 문제 → 로그 백업(즉효) + 패널 스크롤/인디케이터로 해결.

**v1.0-1(②+③로그백업) → v1.0-2(①) → v1.0-3(③패널스크롤)** 순으로, 각각 독립적으로 검증 가능하며 도구 확장 설계(`AGENT_TOOLS_EXPANSION_DESIGN.md`)와 충돌하지 않는다. ask_user 옵션 구조화는 도구 설계와 공동으로 v1.1에 진행한다.
