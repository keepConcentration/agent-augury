# Gateway · Surface · Channel 바인딩 설계 (B4 / B6)

> **Status:** **G1–G6 landed** (bind · host bootstrap · register · attach_* · tests · gap docs)  
> **Date:** 2026-09-15  
> **Parent:** `docs/architecture/MULTI_FRONT_DESIGN.md` §7–§9, `IMPLEMENTATION_GAP_CONSOLIDATED.md` B4/B6  
> **Code:** `gateway/bridge.py` (`bind_session`), `gateway/host.py`, `gateway/register.py`, `channels/**`  
> **Tests:** `tests/test_gateway_bind.py`, `tests/test_gateway_register.py`

---

## 0. 한 줄

**Wire fan-out은 `SessionGateway` + `SurfaceSubscription`만 SSOT**로 두고,  
**Core → Wire egress는 `SessionBridge.bind_session` 한 경로**로 통일하며,  
**채팅 플랫폼은 `channels/*/observe.py`의 `attach_*` + 공통 `register_chat_surface`만 추가**하면 된다.

---

## 1. 배경 (해결된 격차)

### 1.1 B6 — Core 관측 훅 배선 (이전: 이중화)

| 경로 | 이전 | 현재 |
|------|------|------|
| Server 이벤트 | `_on_server_event` → `publish_core_event` | 동일 |
| Agent **step** | runner마다 `_publish_step` | **`bind_session` + `bootstrap_gateway_host`** |
| `attach_session_callbacks` | 데드 코드 | **삭제** |

Step만 Bridge를 타지 않고 runner마다 `_publish_step`이 복제되어, **M8 Desktop/Web runner를 추가할 때마다 같은 코드를 또 쓸 위험**이 있다.

`attach_session_callbacks`는 `on_tool_event`까지 Wire에 올리도록 되어 있으나, tool은 이미 `_on_server_event`에서 Wire로 나간다. 큐 쪽 tool payload(프로토콜 violation 태깅 등)는 **별 트랙**이므로 v1 bind에서는 **step만** Bridge가 담당한다.

### 1.2 B4 — `ChannelAdapter` vs 실제 등록 (해결)

- **이전:** Protocol만 있고 Mirror가 implement하지 않음; `SurfaceMode` 중복; `attach_*`가 `SurfaceSubscription` 직접 조립
- **현재:** `register_chat_surface` / `register_ui_surface` SSOT; `SurfaceMode` = `gateway/bus.py`만;
  Mirror/BotManager = **send sink**; adapter = attach_* closure + subscription

### 1.3 왜 정리했는가

| 미정리 시 | M8+에서 반복되는 비용 |
|-----------|------------------------|
| runner마다 `_publish_step` | Desktop/Web/CLI variant마다 복붙 |
| attach_*마다 subscription 수동 조립 | family/mode/예외 규칙 drift |

**SSOT 고정 후** 새 채널은 `attach_foo` + sink + YAML 수준으로 추가한다.

---

## 2. 목표 · 비목표

### 2.1 목표

1. **Egress SSOT:** Core에서 Wire로 나가는 관측 경로를 문서·코드 모두 **두 갈래**로 고정 (§4).
2. **`SessionBridge.bind_session`:** step → Wire + (선택) 기존 `on_step` 체인 — **모든 Gateway host가 동일 API 호출**.
3. **`register_chat_surface`:** chat family surface 등록의 **유일한 조립점** (observe 예외 삼키기, `event_types`, `family="chat"`).
4. **`SurfaceMode` SSOT:** `gateway/bus.py`만 정의; `channels`는 re-export 또는 import only.
5. **타입 계약:** `ChannelAdapter`는 “테스트 fake / 문서용 이름”이 아니라 **`register_chat_surface`가 받는 최소 shape**로 재정의.
6. **부트스트랩 한 함수:** `bootstrap_gateway_host(session, opts)` — stdio·headless·향후 runner가 **반드시** 호출.

### 2.2 비목표 (본 설계 범위 밖)

| 제외 | 이유 |
|------|------|
| B5 per-agent Gateway 필터 | 별도 항목; `SurfaceSubscription.agent_ids`는 §7 확장 슬롯만 예약 |
| A7 publish backpressure / surface별 예외 격리 | bus 동작 변경; binding 설계와 분리 |
| Mirror/BotManager를 Protocol 구현 클래스로 대규모 OOP화 | closure 패턴 유지; thin registration만 SSOT |
| Core run loop에서 step을 server event와 merge | 동작 변경 큼; Bridge bind로 충분 |

### 2.3 성공 기준

- `session_stdio` / `headless`에 **`_publish_step` 없음** — bind + `suppress_agent_steps`만.
- 새 가짜 host 테스트가 **`bootstrap_gateway_host` 한 줄**로 Ink와 동일 Wire step 수신.
- Discord/Slack `attach_*`가 **`register_chat_surface`만** 호출 (직접 `SurfaceSubscription(...)` 조립 금지 — lint 또는 review convention).
- `attach_session_callbacks` **삭제** (이름·동작을 `bind_session`으로 대체).
- `channels/base.py`에 **로컬 `SurfaceMode` 정의 없음**.

---

## 3. 레이어 책임 (고정)

```text
┌─────────────────────────────────────────────────────────────┐
│ Core (session, server, agent, protocol)                      │
│  · run loop, HITL, protocol                                  │
│  · _on_server_event → bridge.publish_core_event (유지)     │
│  · step → output queue → on_step (유지)                      │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ gateway/                                                     │
│  translate.py     Core dict → WireEvent                      │
│  bridge.py        bind_session, publish_core_event, cmd route│
│  bus.py           SessionGateway, SurfaceSubscription (SSOT) │
│  register.py NEW  register_chat_surface, register_ui_surface │
│  host.py NEW      bootstrap_gateway_host                     │
│  stdio.py         JsonlStdioBridge → register_ui_surface     │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│ channels/{discord,slack}/                                    │
│  mirror, bot, inbound — platform I/O sinks                   │
│  observe.py, inbound.py — attach_* → register_chat_surface   │
│  base.py          ChatSurfaceHandler 타입 + re-export only   │
└─────────────────────────────────────────────────────────────┘
```

| 레이어 | 책임 | 하지 않는 것 |
|--------|------|----------------|
| Core | 에이전트·프로토콜·server publish | Discord API, JSONL |
| SessionBridge | Core↔Wire 번역, cmd, pending HITL | surface 등록 |
| SessionGateway | fan-out, interact/observe 규칙 | 플랫폼 포맷 |
| `register_*` | subscription 필드·관례 SSOT | 이벤트 포맷 |
| channels `attach_*` | platform sink + handler closure | bus 규칙 재정의 |

---

## 4. Core → Wire egress (두 갈래 — 변경 금지)

| 갈래 | 트리거 | Wire 경로 | 소비자 |
|------|--------|-----------|--------|
| **A. Server observer** | `server` callback | `_on_server_event` → `publish_core_event` | Discord observe, Ink JSONL, … |
| **B. Step observer** | agent `step()` 완료 | `_output_queue` → `on_step` → **`bind_session`이 Bridge에 연결** | `agent.step` 이벤트 |

**C. Session lifecycle** (resumed, ended, log, approval, …)은 Core가 **`gateway.publish` 직접** — Bridge를 거치지 않음 (현状 유지).

### 4.1 `on_tool_event` (큐 전용)

- Server tool 이벤트: 갈래 **A**로 Wire `tool` 발행됨.
- `_output_queue`의 enriched tool: **`on_tool_event` 콜백** — REPL/테스트용; **bind_session이 Wire에 중복 publish 하지 않음**.
- 향후 “enriched tool을 Wire에도” 필요하면 **translate 레이어에 단일 훅** 추가 (bind 이중화 금지).

---

## 5. B6 — `SessionBridge.bind_session`

### 5.1 API (제안)

```python
@dataclass(frozen=True)
class BridgeBindOptions:
    wire_agent_steps: bool = True
    suppress_agent_steps: bool = False  # headless --quiet


def bind_session(
    self,
    session: SessionLike & HasObserverHooks,
    *,
    options: BridgeBindOptions | None = None,
) -> None:
    ...
```

**동작:**

1. `self.session = session` (기존 `attach_session_callbacks`와 동일).
2. `options.wire_agent_steps`가 True이면 `on_step`을 체인:
   - `suppress_agent_steps`면 publish 생략, `prev_step`만 호출.
   - 아니면 `publish_core_event({"type":"step", ...})` 후 `prev_step`.
3. **`on_tool_event`는 건드리지 않음.**
4. idempotent: 두 번 호출 시 **바깥 prev만 한 번 더 감싸지 않도록** `_step_bound` 플래그 또는 기존 wrapper identity check (구현 시 택1).

### 5.2 Host 부트스트랩 SSOT

**파일:** `gateway/host.py`

```python
def bootstrap_gateway_host(
    session: Session,
    bridge: SessionBridge,
    *,
    bind: BridgeBindOptions | None = None,
) -> None:
    bridge.bind_session(session, options=bind)
    bridge.install()  # on_command — idempotent if already installed
```

**호출자 (유일하게 허용되는 bind 진입점):**

| Host | `BridgeBindOptions` |
|------|---------------------|
| `session_stdio.InkGatewayRunner.run` | `suppress_agent_steps=quiet` |
| `headless.HeadlessRunner.run` | `suppress_agent_steps=quiet` |
| 향후 Desktop/Web child | 동일 |
| 단위 테스트 (Wire step 불필요) | `wire_agent_steps=False` |

**금지:** runner가 `session.on_step = ...` 로 `publish_core_event` 직접 호출.

### 5.3 Session 생성과의 관계

- `Session.__init__`에서 **`bind_session` 호출하지 않음** — quiet·wire_steps off 테스트가 깨지지 않게.
- `Session.open_from_config`도 host runner가 뜬 뒤 bootstrap — **Core는 Gateway host 정책을 모름** (관심사 분리 유지).

### 5.4 삭제

- `SessionBridge.attach_session_callbacks` — **제거** (git grep 0).

---

## 6. B4 — Surface 등록 SSOT

### 6.1 `SurfaceSubscription` (유지 · SSOT)

정의 위치: **`gateway/bus.py` only.**

필드 (현状 + §7 확장):

| 필드 | 의미 |
|------|------|
| `name` | surface id (`ink`, `discord-mirror`, …) |
| `mode` | `interact` \| `observe` |
| `family` | `ui` \| `chat` |
| `on_event` | Wire event handler (None = cmd-only shell, inbound placeholder) |
| `event_types` | allowlist; `None` = all |

### 6.2 `gateway/register.py` (신규)

```python
def register_chat_surface(
    gateway: SessionGateway,
    *,
    name: str,
    mode: SurfaceMode,
    on_event: Callable[[WireEvent], None] | None,
    event_types: frozenset[str] | None,
) -> None:
    """Observe-only chat: on_event must swallow exceptions (documented)."""
    gateway.attach(
        SurfaceSubscription(
            name=name,
            mode=mode,
            family="chat",
            on_event=_guarded(on_event) if on_event else None,
            event_types=event_types,
        )
    )


def register_ui_surface(
    gateway: SessionGateway,
    *,
    name: str,
    mode: SurfaceMode,
    on_event: Callable[[WireEvent], None],
    event_types: frozenset[str] | None = None,
) -> None:
    gateway.attach(
        SurfaceSubscription(
            name=name,
            mode=mode,
            family="ui",
            on_event=on_event,
            event_types=event_types,
        )
    )
```

**`_guarded`:** chat observe 필수 — handler 내부 try/except는 sink가 errors에 넣는 패턴 유지; register는 **마지막 방어막** (D5와 별개, double-safe).

**Ink:** `JsonlStdioBridge.attach()` → `register_ui_surface(..., mode="interact")`.

### 6.3 `ChannelAdapter` 재정의 (`channels/base.py`)

Protocol을 “Mirror 클래스”가 아니라 **등록 가능한 chat handler**로 좁힌다:

```python
class ChannelAdapter(Protocol):
    """Chat surface: consumed by register_chat_surface via attach_* helpers."""

    @property
    def surface_name(self) -> str: ...

    @property
    def mode(self) -> SurfaceMode: ...

    def on_wire_event(self, event: WireEvent) -> None: ...
```

**런타임에 Mirror가 Protocol을 implement할 필요 없음.**  
`attach_discord_mirror(gw, mirror)`는 내부에서 closure가 `on_wire_event` 역할을 하고 `register_chat_surface(name="discord-mirror", ...)` 호출.

향후 선택: `DiscordMirrorSurface` dataclass가 `ChannelAdapter`를 구현하고 attach가 3줄 — **G4 이후 optional**.

**삭제:** `base.py`의 standalone `SurfaceMode = Literal[...]` → `from agent_augury.gateway.bus import SurfaceMode` re-export.

### 6.4 새 채팅 채널 체크리스트 (M8+)

1. `channels/<platform>/mirror.py` (또는 bot) — **send sink only**
2. `channels/<platform>/observe.py` — `attach_<platform>_mirror(gateway, sink, *, name=...)`
3. handler closure: Wire → format (`chat_surface_format`) → sink
4. **반드시** `register_chat_surface` — 직접 `SurfaceSubscription` 생성 금지
5. interact inbound: `register_chat_surface(..., mode="interact", on_event=None)` + `gateway.dispatch(..., surface=name)` (Discord inbound 패턴 복제)
6. `Session.open_from_config` (또는 surfaces YAML) 한 줄 attach — **Session은 attach 함수 import만**

---

## 7. 확장 슬롯 (지금 필드만 예약 · 구현은 B5)

`SurfaceSubscription`에 optional 추가 (G6 또는 B5):

```python
agent_ids: frozenset[str] | None = None  # None = all agents
```

Gateway `publish`에서 `event.get("agent_id")` 필터 — **지금은 구현하지 않음.**  
Until then, per-agent routing remains in `BotManager.route_event` (현状).

---

## 8. 구현 단계 (G1–G6)

| ID | 작업 | 파일 (주) |
|----|------|-----------|
| **G1** | `BridgeBindOptions`, `bind_session`; `attach_session_callbacks` 삭제 | `gateway/bridge.py` |
| **G2** | `bootstrap_gateway_host`; stdio/headless에서 `_publish_step` 제거 | `gateway/host.py`, `session_stdio.py`, `headless.py` |
| **G3** | `register_chat_surface`, `register_ui_surface`; stdio 전환 | `gateway/register.py`, `stdio.py` |
| **G4** | discord/slack `attach_*` → register 경유; `base.py` SurfaceMode 정리 | `channels/**` |
| **G5** | 테스트: bind idempotent, quiet suppress, attach still works; grep gate | `tests/test_gateway_bind.py` (신규), 기존 discord/slack/headless |
| **G6** | 문서: MULTI_FRONT §9, IMPLEMENTATION_GAP B4/B6 → landed | docs ✅ |

**권장 순서:** G1 → G2 → G3 → G4 → G5 → G6 (한 PR 또는 G1–G2 / G3–G4 분할).

---

## 9. 테스트 계획

1. **bind_session:** fake session `on_step` 호출 시 `gateway.publish`에 `agent.step` 1건.
2. **chain:** 기존 `on_step` mock이 bind 후에도 호출됨.
3. **suppress_agent_steps:** publish 0, prev 호출 유지.
4. **wire_agent_steps=False:** publish 0.
5. **register_chat_surface:** handler 예외가 `publish`를 깨지 않음 (guarded).
6. **회귀:** `test_discord_observe`, `test_slack_observe`, `test_headless`, Ink integration smoke.

---

## 10. 문서 · 갭 추적

- `MULTI_FRONT_DESIGN.md` §9: “ChannelAdapter = `register_chat_surface` + attach_*”; Mirror/BotManager = sink.
- `IMPLEMENTATION_GAP_CONSOLIDATED.md` B4/B6: 본 문서 링크, G* 완료 시 ✅.
- README: 변경 없음 (내부 아키텍처).

---

## 11. 결정 로그

| 결정 | 대안 | 이유 |
|------|------|------|
| step만 bind | tool도 bind | Wire 중복; enriched tool은 별도 트랙 |
| bind는 host bootstrap | Session.__init__ | quiet / test without Wire |
| `register.py` 분리 | bus에 함수 추가 | bus는 자료구조; registration 관례는 별 파일 |
| Protocol 유지 | Protocol 삭제 | M8에서 fake adapter·typing 가치 |
| guarded chat handler | channels만 try/except | 신규 attach_* 실수 방지 |

---

## 12. 참고 코드 (현状)

- Server egress: `core/session.py` — `_on_server_event` → `bridge.publish_core_event`
- Step egress (중복): `gateway/session_stdio.py`, `headless.py` — `_publish_step`
- Chat attach: `channels/discord/observe.py`, `channels/slack/observe.py`
- UI attach: `gateway/stdio.py` — `JsonlStdioBridge.attach`
