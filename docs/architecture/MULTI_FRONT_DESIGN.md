# Multi-front architecture — Python Core + UI & Chat surfaces

> **Status:** **M0–M6 landed** · v0.2.6 (primary=UI, chat=observe-only default)  
> **Date:** 2026-09-11  
> **Goal:** Python **core 유지**. Surface = Ink/Desktop/Web **+** Discord/Slack/…  
> **Not:** 전면 Node화(엔진 TS 재작성).  
> **Related:** `USER_INTERVENTION_DESIGN.md`, `channel/discord_*`, `channel/slack_*`  
> **Schemas:** `schemas/wire/` · **Code:** `src/agent_augury/gateway/`  
> **Ink hello+HITL:** `fronts/ink/` (`agent-augury --ink-hello`)  
> **Discord:** observe (M4); inbound opt-in `bots[].inbound` (M5)  
> **Slack:** Incoming Webhook observe `slack.url_env` (M6 spike)  
> **Archived pt-TUI designs:** `docs/archive/tui/`

---

## 0. 한 줄 결론

UI 프론트와 채팅 봇은 **같은 Core / 같은 Wire**에 붙는 **Surface 패밀리 두 종류**다.

| 패밀리 | 예 | 특징 |
|--------|-----|------|
| **Interactive UI** | Ink CLI, Desktop, Web | 동기식 크롬(입력창·패널), 보통 세션당 1 조작자 |
| **Chat Platform** | Discord, Slack, Teams… | 비동기 푸시, 채널/스레드 매핑, rate limit, **다인·장기 봇** |

BFF는 웹에만 두껍게; 채팅은 **Channel Adapter**(대부분 Python in-proc)가
플랫폼 API ↔ Wire/커맨드를 변환한다.

---

## 1. 사용자 3계층 + 채팅을 넣으면

제안:

```text
1. core
2. 프론트별 변환 (BFF)
3. front (웹 / CLI / 데스크탑 / …)
```

채팅까지 넣으면 3번은 **“사람이 세션을 보는 모든 창구”**로 넓어진다.
2번은 여전히 **두꺼운 BFF × N이 아니라**:

```text
Gateway (공통 Wire)
  + Interactive Adapter (ink / desktop / web-bff)
  + Channel Adapter (discord / slack / …)
```

```text
                    ┌──────────────────────────────────────┐
                    │ Interactive Surfaces                 │
                    │  Ink · Desktop · Web                 │
                    └──────────────▲───────────────────────┘
                                   │ UI view models
                    ┌──────────────┴───────────────────────┐
                    │ Interactive Adapters                 │
                    │  ink-adapter · desktop · web-bff     │
                    └──────────────▲───────────────────────┘
                                   │
┌──────────────┐    Augury Wire    │    ┌──────────────────┐
│ Chat nets    │◄───(events/cmds)──┼───►│ Session Gateway  │
│ Discord      │                   │    │  fan-out · life  │
│ Slack        │    Channel        │    └────────▲─────────┘
│ …            │◄───Adapters ──────┘             │ in-proc
└──────────────┘  (discord/slack/…)              │
                                        ┌────────┴─────────┐
                                        │ Core (Python)    │
                                        │ Session · Server │
                                        │ AgentLoop · tools│
                                        └──────────────────┘
```

**핵심 변화 (v0.1 → v0.2):**

1. Gateway는 세션당 Surface **1개**가 아니라 **N개 fan-out**이 기본 가정.
2. Chat은 “나중 특수 케이스”가 아니라 **처음부터 Wire 소비자**.
3. outbound(관찰) / inbound(개입)를 Adapter 계약으로 명시.
4. Discord 现状(`channel/`)은 Channel Adapter의 **초기 구현**으로 재분류.

---

## 2. 두 패밀리의 계약 차이

| 차원 | Interactive UI | Chat Platform |
|------|----------------|---------------|
| 전송 | stdio / WS / HTTP | 플랫폼 Gateway API + webhook/events API |
| 수명 | 세션 ≈ UI 프로세스 | 봇은 세션보다 길 수 있음 |
| 신원 | 보통 로컬 1 human | 플랫폼 user id → `human` principal 매핑 |
| 스레드 | UI가 thread_id 선택/표시 | **channel/thread/ts ↔ augury thread** 매핑 테이블 |
| ask_user | 고정 패널 + 버튼 | 메시지 + reactions / buttons / modal |
| 로그 밀도 | Static 전문 가능 | **요약·청크·2000자 제한** (Adapter 포맷) |
| 실패 | UI에 에러 표시 | 관찰 경로는 **절대 세션을 죽이면 안 됨** (현 mirror 원칙) |
| 구현 언어 | Ink=Node, Desktop=?, Web=BFF | **Python SDK 우선** (이미 py-cord; Slack도 보통 Python) |

→ Chat Adapter는 Node Ink와 **같은 Wire 이벤트**를 먹되,
프로세스는 대개 **Gateway/Core 옆 Python**에 산다.
(원하면 별도 worker 프로세스 + Wire도 가능.)

---

## 3. Channel Adapter 책임

### 3.1 Outbound (observation / mirror)

Core 이벤트 → 플랫폼 메시지.

- 현재: `DiscordWebhookMirror`, `DiscordBotAdapter` (send-only)
- 원칙 유지: **채널은 뷰** — SSOT는 MessageServer  
  (`discord_mirror.py` 주석, DESIGN §3.3)
- Adapter가 실패해도 Core는 계속 (`errors` 수집, swallow)

### 3.2 Inbound (intervention / HITL)

플랫폼 메시지 → `human.send` / `human.answer` 커맨드.

- `USER_INTERVENTION_DESIGN.md` v1.1 방향 (`bots[].inbound`)
- Adapter만 Discord/Slack user → 예약어 `"human"`(또는 다중 human id) 매핑
- Core는 플랫폼 id를 모름

### 3.3 매핑 상태 (Adapter 로컬)

```text
PlatformRef (guild/channel/thread/ts)
    ↔ augury thread_id
PlatformUser
    ↔ human principal
ask_user message id / slack view id
    ↔ pending question id
```

이 표는 **Core SSOT가 아님**. Adapter 재시작 시 유실 가능 →
필요하면 Core에 `external_binding` 저장을 후속으로 올림.

---

## 4. Gateway fan-out

```text
Core event
    │
    ▼
Gateway.broadcast(event)
    ├─► ink-adapter (stdio)
    ├─► desktop (ws)
    ├─► discord-adapter (inproc)
    └─► slack-adapter (inproc)
```

구독 필터 (설정):

```yaml
surfaces:
  ink: { enabled: true }          # primary — 입력 허용
  discord:
    enabled: true
    mode: observe                 # 기본: observe | interact(opt-in)
    agents: [agent-1]
  slack:
    enabled: false
    mode: observe
```

- Interactive UI는 보통 **전체 이벤트** + **human.* 커맨드 허용**
- Chat 기본은 **outbound만** (요약/봇 채널). `mode: interact`일 때만 inbound

---

## 5. Wire Protocol 확장 (채팅 친화)

v0.1 이벤트/커맨드에 더해:

### 5.1 이벤트 (Server → Surfaces)

| `type` | 용도 |
|--------|------|
| (기존) `agent.step`, `tool`, `message`, … | 공통 |
| `human.question` | ask_user 승격(권장) — Chat이 버튼 UI 만들기 쉬움 |
| `log.summary` | (옵션) Adapter/Gateway가 만든 짧은 요약 힌트 — 없어도 Adapter가 step에서 자체 요약 가능 |

### 5.2 커맨드 (Surfaces → Server)

| `type` | 용도 |
|--------|------|
| (기존) `human.send`, `human.answer`, `human.skip` | |
| `human.send` + `source` | `{ source: { surface: "slack", user: "U123", channel: "C…" } }` — Core는 감사/정책용으로만, 라우팅은 내용 기준 |

Core는 `source`를 **신뢰 경계 메타**로 기록할 수 있으나,
권한 판단은 Gateway/정책 모듈이 담당(후속).

### 5.3 Chat이 Wire를 안 타고 Core를 직접 찌르는 경우

과도기(지금 Discord): `server.subscribe(mirror)` / BotManager가 Session에 직접 붙음.  
목표: **같은 subscribe/dispatch API**를 쓰되 transport만 in-proc.
즉 “Wire 객체”는 JSON일 수도, in-proc Python dataclass일 수도 있음.
**스키마는 하나.**

---

## 6. 계층 책임 (갱신)

### 6.1 Core

동일: 세션·에이전트·SSOT·canonical events/commands.  
**모름:** Discord channel id, Slack mrkdwn, Ink `<Static>`.

### 6.2 Gateway

- Surface 등록/해제, fan-out, backpressure
- stdio / WS / in-proc bus
- (나중) auth, 세션 멀티플렉스

### 6.3 Adapters

| Adapter | 패밀리 | 변환 |
|---------|--------|------|
| ink-adapter | UI | DomainEvent → Static/Ink; key UI → Command |
| desktop-adapter | UI | DomainEvent → window state |
| web-bff | UI | HTTP/SSE + auth (**두꺼운 BFF 허용**) |
| discord-adapter | Chat | Event → Discord embed/chunk; message → Command |
| slack-adapter | Chat | Event → Block Kit; slash/app mention → Command |

### 6.4 Surfaces

플랫폼 자체(Discord/Slack 클라이언트) 또는 Ink/Desktop/Web 앱.

---

## 7. 프로세스 토폴로지

### 7.1 Ink + Discord 동시 (전형)

```text
agent-augury (Python)
  Gateway
    ├─ Core Session
    ├─ discord-adapter (inproc, py-cord / webhook)
    ├─ slack-adapter (inproc, 선택)
    └─ stdio ──► augury-ink (Node)
```

사용자는 터미널(Ink)과 Discord를 **동시에** 봄.
입력이 양쪽에서 오면 둘 다 `human.send` — Core는 순서대로 처리
(충돌 정책은 후속: 마지막 승 / 동일 human 병합).

### 7.2 Chat-only (헤드리스 서버)

```text
agent-augury --surface none --channels discord,slack
```

Interactive UI 없이 봇만. CI·서버 배포용.

### 7.3 Desktop + Slack

```text
Desktop ──WS──► Gateway ◄── slack-adapter
                   └─ Core
```

---

## 8. 레포 구조 (갱신)

```text
agent-augury/
  src/agent_augury/
    core/                 # session, server, agent… (점진 정리)
    gateway/              # wire + fan-out + transports
    channels/             # ← 기존 channel/ 승격·정리
      discord/            # bot + mirror (현 discord_bot/mirror)
      slack/              # 신규
      base.py             # ChannelAdapter protocol
    # tui/                # legacy pt (과도기)
  fronts/
    ink/
    desktop/              # 나중
    web/                  # 나중
  schemas/wire/           # JSON Schema (UI·Chat 공유)
  docs/architecture/
    MULTI_FRONT_DESIGN.md
```

---

## 9. 기존 Discord 코드 재분류

| 현재 | v0.2에서의 위치 |
|------|-----------------|
| `DiscordWebhookMirror` | ChannelAdapter outbound (observe) |
| `DiscordBotAdapter` + `BotManager` | ChannelAdapter outbound (per-agent bot) |
| `bots[].inbound` (설계만) | ChannelAdapter inbound → `human.*` |
| `mirror:` config | `surfaces.discord` / `channels.discord` 로 통합 예정 |

Slack은 Discord와 **동일 Protocol**, 다른 API 클라이언트.

---

## 10. 로드맵 (갱신)

| 단계 | 내용 |
|------|------|
| **M0** | Wire JSON Schema (UI+Chat 공통 이벤트/커맨드) — **done** |
| **M1** | Gateway in-proc bus + fan-out 테스트 (fake UI + fake chat) — **done** |
| **M2** | Ink hello Surface + JSONL stdio bridge — **done** (`fronts/ink`, `--ink-hello`) |
| **M3** | Ink ask_user / interrupt 패리티 · Core↔Gateway `SessionBridge` — **done** |
| **M4** | Discord adapter를 Gateway 구독으로 이전 (동작 동등) — **done** |
| **M5** | Discord inbound (HITL) — **done** (`bots[].inbound: true` → `human.*`) |
| **M6** | Slack observe 스파이크 — **done** (`slack.url_env` Incoming Webhook) |
| **M7** | Desktop 또는 Web 스파이크 · 또는 CLI Ink 실세션 연결 |
| **M7** | Desktop 또는 Web 스파이크 |

pt TUI는 M3까지 legacy 유지 가능.

---

## 11. 리스크 (채팅 추가분)

| 리스크 | 완화 |
|--------|------|
| UI+Chat 이중 입력 충돌 | 단일 큐; 문서화; 필요 시 “primary surface” 설정 |
| 플랫폼 rate limit | Adapter outbox + 청크 (현 Discord 1800자) |
| 매핑 유실 | 재시작 정책; 후속 Core binding store |
| 요약 과도/누락 | Adapter 설정 `verbosity: full\|summary` |
| 보안 (토큰) | 계속 env; Gateway가 토큰을 Surface에 안 넘김 |

---

## 12. 결정 요약

1. Discord/Slack은 특수 사이드카가 아니라 **Chat Surface + Channel Adapter**.  
2. Interactive UI와 **동일 Wire/Gateway**; fan-out이 기본.  
3. 두꺼운 BFF는 **Web만**; Chat은 Python Channel Adapter가 본진.  
4. **Primary = Interactive UI(Ink 등). Chat 기본 = observe-only** (inbound는 opt-in).  
5. ~~다음 착수: M0…M6~~ → **다음: M7** (Desktop/Web 스파이크) 또는 **CLI Ink 실세션** 연결.
   Slack은 observe-only Incoming Webhook 스파이크; Block Kit / inbound는 후속.

---

## 13. 합의된 결정

| # | 결정 | 내용 |
|---|------|------|
| D1 | **Primary surface = Interactive UI** | v1 기본: Ink(또는 pt legacy)만 입력·선택·interrupt·quit |
| D2 | **Chat = observe-only 기본** | Discord/Slack은 outbound 관찰(미러/봇 send)만. inbound HITL은 **명시 opt-in** |
| D3 | 전면 Node화 아님 | Core Python 유지 |

### D1–D2 동작

```text
사람 입력 경로 (기본)
  Ink / Desktop / Web  ──human.*──► Gateway ──► Core

관찰 경로 (기본)
  Core ──events──► Gateway ──► discord/slack adapters ──► 플랫폼 채널
```

- `surfaces.discord.mode` 기본값: `observe`
- `interact`(inbound)는 config에서 켠 세션만 (`bots[].inbound` / `channels.discord.inbound: true`)
- Chat에서 입력이 와도 **mode=observe면 Adapter가 drop** (Core에 도달하지 않음)

---

## 14. 열린 질문 (남은 것)

1. Slack 우선순위: observe-only 스파이크 먼저 vs 나중?  
2. Chat Adapter를 항상 in-proc로 둘지, 부하 시 worker 분리할지?  
3. Desktop 스택 후보 (Electron / Tauri)? → 해당 Surface 착수 시 결정.  
4. pt TUI 지원 기간? → Ink GA 후 1 메이저 동안 legacy 권장.
