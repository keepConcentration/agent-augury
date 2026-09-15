# Surface display · delivery 노브 설계 (A6 / A3 재배치)

> **Status:** **V1 ✅ landed** (`channels/display.py`) — V2/V3 후순위  
> **Date:** 2026-09-15  
> **Parent:** `MULTI_FRONT_DESIGN.md` §5.1 / §11, `IMPLEMENTATION_GAP_CONSOLIDATED.md` A3/A6  
> **참고:** Hermes `display.tool_progress` / `display.platforms.*` / `truncate_message`  
> **전제:** Core · Wire · 멀티 에이전트 계약은 Augury SSOT 유지. Hermes를 **통째로** 베끼지 않는다.

---

## 0. 한 줄

진입점(Ink / Discord / Slack / 이후 Web·Desktop)이 늘수록  
**배달·표시 밀도만 Hermes식으로 성숙**시키고,  
**요약은 기본적으로 Chat Adapter 포맷터**에서 처리한다.  
공통 Wire `log.summary`(구 A3)는 **후순위·옵션**으로 격하한다.

---

## 1. 동기

| Augury (지금) | Hermes | 배울 점 |
|---------------|--------|---------|
| Wire fan-out + Core SSOT ✅ | Gateway + adapters | 유지 |
| 채팅 밀도 `display.chat` (A6 V1) | `display.tool_progress` + per-platform | V2+ 세분화 후보 |
| `log.summary` 설계만 (A3) | 타입 없이 adapter/display로 조절 | **A3보다 A6 우선** |
| 청크·mailbox·enqueue 안전 ✅ | `truncate_message`, outbox | 이미 근접 |

**원칙:** “Hermes가 된다”가 아니라  
**표면↑ → display/delivery 노브↑**, Core/Wire/프로토콜은 Augury.

---

## 2. 목표 · 비목표

### 2.1 목표

1. YAML로 **채팅 observe 밀도**를 고른다 (`full` / `summary` / `quiet`).  
2. **플랫폼(또는 surface)별 override** 가능 (Hermes `display.platforms` 축소판).  
3. Ink(UI)는 기본 **full Wire** — 채팅 노브와 분리.  
4. summary 모드는 **포맷터·event allowlist**로 구현 (Core는 동일 이벤트 발행).  
5. A3 `log.summary`는 V1에 **넣지 않음** — 필요해지면 V3 옵션.

### 2.2 비목표

| 제외 | 이유 |
|------|------|
| Hermes 전체 `display.*` / commentary / compression notices | 제품 범위·복잡도 |
| Core가 Discord용 요약 문자열 생성 | 채널=뷰; 포맷은 Adapter |
| UI·Chat 동일 verbosity 강제 | 패밀리 계약이 다름 |
| LLM 기반 step 요약 | 비용·비결정; V1은 규칙 기반 |

---

## 3. 개념 모델

```text
Core ──Wire events──► SessionGateway.publish
                           │
           ┌───────────────┼───────────────┐
           ▼               ▼               ▼
        Ink (ui)      Discord (chat)   Slack (chat)
        full JSONL    DisplayPolicy    DisplayPolicy
                      → format / skip  → format / skip
                      → chunk (D2)     → chunk (D2)
```

| 계층 | 책임 |
|------|------|
| Core | 항상 동일 Wire 발행 (verbosity 모름) |
| Gateway | fan-out, mailbox (A7); **필터하지 않음** (구독은 surface) |
| Chat Adapter / observe | `DisplayPolicy`로 **받을 타입·포맷 밀도** 결정 |
| UI Adapter | 기본 전 이벤트; 별도 quiet는 host(`--quiet`)만 |

Gateway에 agent/type 필터를 넣기보다( B5와 혼동),  
**chat observe의 `event_types` + 포맷터 분기**로 V1을 끝낸다.  
B5(`agent_ids`)는 별 트랙.

---

## 4. YAML 계약 (V1)

### 4.1 전역 기본

```yaml
# 세션 YAML (제안)
display:
  chat: summary          # full | summary | quiet
  # ui는 생략 = full (Ink JSONL)
```

또는 `surfaces:` 아래로 붙여 Hermes식 지역성 강화:

```yaml
surfaces:
  display:
    chat: summary
  discord:
    # … existing …
    display: full        # override: 이 플랫폼만 full
  slack:
    display: quiet
```

**동결 (V1):**

| 키 | 위치 | 기본 |
|----|------|------|
| `display.chat` | top-level 또는 `surfaces.display.chat` | `full` (오늘 동작) |
| `surfaces.<plat>.display` | discord / slack / mirror 계열 | 상속 |

legacy `bots:` / `slack:` / `mirror:`만 쓰는 설정 → `display.chat`만 적용.

### 4.2 모드 의미 (Chat)

| 모드 | Discord/Slack observe가 하는 일 |
|------|--------------------------------|
| **`full`** | 오늘과 동일: step/message/tool(봇 allowlist)·승인·log 등 기존 `_BOT_TYPES` / slack types |
| **`summary`** | 고밀도 억제: `tool` / `read_resource` / 장문 step 본문 축약(첫 N자 또는 첫 문단) / `log` 선택 스킵. `message`, `human.question`, `approval.*`, `agent.step`(축약) 유지 |
| **`quiet`** | HITL·사람 가시만: `human.question`, `approval.*`, `tool.denied`, (옵션) `message` with human author. step/tool/log 스킵 |

정확한 allowlist는 §6 표로 코드 SSOT.

### 4.3 Ink / headless

| Surface | V1 |
|---------|-----|
| Ink JSONL | **무시** `display.chat` (항상 full wire) |
| headless stderr | 기존 `--quiet` 유지; `display.chat`과 독립 |

---

## 5. 런타임 API

```python
# channels/display.py (신규)
@dataclass(frozen=True)
class ChatDisplayPolicy:
    mode: Literal["full", "summary", "quiet"] = "full"

    def allow(self, event_type: str) -> bool: ...
    def format_wire(
        self,
        event: WireEvent,
        *,
        recipient_agent_id: str | None = None,
    ) -> str | None:
        """None = skip. summary면 축약 본문."""
        ...
```

- `format_wire_for_chat_surface`는 **full 포맷터**로 유지.  
- summary 축약은 `ChatDisplayPolicy.format_wire`가 wrap하거나 내부 분기.  
- `attach_discord_*` / `attach_slack_*`가 Session/config에서 policy를 받아 on_event에 적용.

**Core·translate·schema 변경 없음 (V1).**

---

## 6. summary / quiet allowlist (초안 · 코드가 SSOT)

| Wire `type` | full | summary | quiet |
|-------------|------|---------|-------|
| `message` | ✓ | ✓ | human author만 (또는 ✓ 짧게) |
| `agent.step` | ✓ | ✓ 축약 | ✗ |
| `tool` | 봇 allowlist대로 | ✗ | ✗ |
| `read_resource` | slack만 등 | ✗ | ✗ |
| `human.question` | ✓ | ✓ | ✓ |
| `approval.*` / `tool.denied` | ✓ | ✓ | ✓ |
| `log` | ✓ | ✗ (또는 error-ish만) | ✗ |
| `thread.created` | ✓ | ✓ 한 줄 | ✗ |

축약 규칙 (summary `agent.step`): 본문 ≤ 280자 + `…` (청크 분할 이전).  
긴 전문이 필요하면 사용자가 `full`로 올림.

---

## 7. A3 `log.summary` 재배치

| | 구 갭 A3 | 본 설계 |
|--|----------|---------|
| 역할 | Wire에 요약 이벤트 | **V1 불필요** — summary 모드가 포맷터에서 처리 |
| V3 (옵션) | — | 여러 chat surface가 **동일 요약 문장**을 공유해야 할 때만  
| Gateway 또는 Core helper가 `log.summary` 1회 publish |

스키마에 타입을 지금 넣지 않는다. MULTI_FRONT §5.1은 “후순위 옵션”으로 문구 수정.

---

## 8. Hermes에서 **나중에** 가져올 후보 (V2+)

| Hermes | Augury V2+ | 비고 |
|--------|------------|------|
| inbound text batching (긴 붙여넣기) | Discord inbound 버퍼 | A8 전·후 모두 유용 |
| `progress_notices` | compact/checkpoint 알림 chat 노출 opt-in | M4 compact와 연동 |
| per-platform `tool_progress` 세분 | `summary`를 `tools: off\|on`으로 쪼개기 | 필요 시 |
| `[SILENT]` | headless/cron식 억제 | 제품 요구 시 |

V1 범위 밖.

---

## 9. 구현 단계

| ID | 작업 | 산출 |
|----|------|------|
| **V1a** | `ChatDisplayPolicy` + allow/format | `channels/display.py` |
| **V1b** | `config` 검증·normalize (`display.chat`, per-surface override) | `config.py` |
| **V1c** | discord/slack `attach_*`에 policy 배선 | `channels/**/observe.py`, session open |
| **V1d** | 테스트 + gap A6 ✅ | `tests/test_chat_display.py` |
| **V1e** | MULTI_FRONT §5.1/§11, gap A3 → deferred | docs |
| **V2** | inbound batching (선택) | discord inbound |
| **V3** | `log.summary` Wire (선택) | schema + translate |

**권장:** V1만으로 A6 닫기. A3는 V3까지 ❌/deferred 유지.

---

## 10. 테스트 계획 (V1)

1. `full` → 기존 observe 테스트와 동일 이벤트 수신.  
2. `summary` → `tool` 미전달, `agent.step` 짧은 본문.  
3. `quiet` → approval/human.question만.  
4. `surfaces.discord.display: full` + `display.chat: quiet` → discord만 full.  
5. Ink 경로 unchanged.

---

## 11. 결정 로그

| 결정 | 대안 | 이유 |
|------|------|------|
| 밀도는 Adapter | Gateway publish 필터 | Core/UI와 분리; B5와 역할 혼선 방지 |
| A6 먼저, A3 후순위 | 둘 다 V1 | Hermes도 요약 Wire 타입 없이 동작 |
| 모드 3단 full/summary/quiet | full\|summary만 | quiet가 shared Slack에 실질적 |
| 규칙 축약 | LLM 요약 | 결정적·무료·테스트 가능 |

---

## 12. 갭 추적

- A6 → 본 문서 V1  
- A3 → deferred (V3); 표기 “미정의” 유지하되 **우선순위 낮춤·대체 경로 = A6 summary**
