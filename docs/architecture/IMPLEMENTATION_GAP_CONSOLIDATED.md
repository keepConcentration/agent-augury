# agent-augury 설계 vs 구현 — 통합 격차 정리 (CONSOLIDATED)

> **역할:** Multi-front / protocol 설계 대비 **구현 격차 정본**.
> 형제 문서: `MULTI_FRONT_DESIGN.md`, `PROTOCOL_GATE_WAIT_PARK_DESIGN.md`.
>
> **검증 방식:** 실제 소스(`src/agent_augury/**`, `fronts/ink/**`, `schemas/wire/**`,
> `tests/**`) 기준. 판정은 **코드 존재 여부**.
>
> **작성 시점:** M0–M7 landed · 패키지 `0.7.0` (`pyproject.toml`).
> **갱신 (2026-09-15):** A2/A4/A5/A7/A10, B1–B4/B6, C1–C4, D1–D7 landed.
> **P3 landed:** AGENT_RELEVANCE_BUDGET V1 (V1a–V1f) — `core/attention.py` · `loop.py` · `session.py` · `config.py` · `tests/test_attention_budget.py` · `examples/attention_budget_demo.yaml`.
> 잔여: **A1(M8), A8(Slack inbound), A9(다중 human), B5(Gateway agent 필터)**;  
> A3=`log.summary`는 A6로 대체·deferred. V1.1+ (`max_tokens`/tools 캡)는 별 트랙.
---

## 0. 한 줄 결론

**M0–M7 골격 + Ink/Discord 운영 품질(P0·대부분 P2)은 코드로 확인 가능**하다.
핵심 원칙(설계 D1–D3, 관찰 실패 시 Core 무중단, 채널=뷰, `human` 예약어)도 지켜진다.

**남은 격차**는 주로 표면 확장이다: M8 Desktop/Web(A1), Slack inbound(A8),
다중 human(A9), Gateway per-agent 필터(B5).

즉 **"Ink+Discord 제품 경로와 Wire/Gateway 계약은 정리됐고, 남은 것은 후순위 front·Slack HITL"**이 현재 요약이다.

---

## 1. 로드맵(M0–M8) 이행 현황

| 단계 | 설계 명세 | 판정 | 구현 근거 |
|------|-----------|------|-----------|
| M0 | Wire JSON Schema (UI+Chat 공통) | ✅ | `schemas/wire/{envelope,events,commands}.schema.json` + `gateway/types.py` |
| M1 | Gateway in-proc bus + fan-out | ✅ | `gateway/bus.py` `publish`/`dispatch`, observe-only `human.*` 거부 |
| M2 | Ink hello + JSONL stdio | ✅ | `gateway/stdio.py`, `hello_demo.py`, `fronts/ink`, `--ink-hello` |
| M3 | Ink ask_user/interrupt 패리티 + `SessionBridge` | ✅ | `gateway/bridge.py` `human.question` 변환, 옵션 인덱스 해석 |
| M4 | Discord adapter → Gateway 구독 | ✅ | `channels/discord/observe.py` |
| M5 | Discord inbound(HITL) | ✅ | `channels/discord/inbound.py` `bots[].inbound` |
| M6 | Slack observe 스파이크 | ✅ | `channels/slack/observe.py` |
| M7 | CLI Ink 실세션 연결 | ✅ | `gateway/session_stdio.py`, `cli.py` `--ink --config` |
| M8 | Desktop 또는 Web 스파이크 | ❌ | `fronts/`에 `ink/`만 존재, `desktop/`·`web/` 없음 |

> 별도 설계 `PROTOCOL_GATE_WAIT_PARK_DESIGN.md`(gate-wait idle park)는 **구현 완료** 상태로
> `session.py`의 `_is_gate_waiting()` / `_wait_for_gate_wakeup()`이 확인되고, 테스트
> `tests/test_gate_wait_park.py`도 존재한다.

---

## 2. 설계 원칙(D1–D3) 및 핵심 계약 이행

| 설계 요구 | 구현 위치 | 판정 |
|-----------|-----------|------|
| D1 Primary = Interactive UI(Ink) | `cli.py::_want_ink_surface()` 기본 `ink` | ✅ |
| D2 Chat observe-only 기본, inbound opt-in | `bus.py` observe surface의 `human.*` 거부 + `bots[].inbound`만 interact | ✅ |
| D3 Core Python 유지(전면 Node화 아님) | Core가 `src/agent_augury/` Python | ✅ |
| 채널은 뷰 / 관찰 실패 시 Core 무중단 | `discord_mirror.py`·`slack_mirror.py` `errors` 수집 + `flush()` swallow | ✅ |
| `human.question`(ask_user 승격) | `translate.py::_ask_user_to_question` | ✅ |
| `human` 예약어 / `_agents`·`_humans` 분리 | `server.py` `RESERVED_NAMES`, `register_human()` | ✅ |
| 옵션 인덱스→텍스트 해석 | `bridge.py::_resolve_option_content` | ✅ |
| 토큰은 env만, YAML엔 이름만 | `config.py` `${VAR}` 확장, `cli.py` `_missing_api_key_envs` | ✅ (D1: Ink spawn env scrub + secrets file) |
| rate limit 절단 | Discord `_MAX_CONTENT=1800`, Slack `_MAX_CONTENT=3000` | ✅ 분할 (`chunk.split_chat_content`) |

---

## 3. 격차 종합표 (통합·재번호)

기존 문서마다 G1~G17 번호가 제각각이라, 아래처럼 **A(기능·표면) / B(배선·계약) / C(스키마) /
D(안정·강제)** 로 통일했다. 표의 ❌/⚠️만 미결.

| # | 항목 | 상태 | 설계 근거 |
|---|------|------|-----------|
| A1 | M8 Desktop/Web + WS·HTTP/SSE transport | ❌ 미구현 | §6.2/§6.3/§10 |
| A2 | `surfaces:` 통합 YAML | ✅ `config.normalize_surfaces` → legacy `mirror`/`bots`/`slack` | §4/§12 |
| A3 | `log.summary` 이벤트 | ⏸️ deferred (A6로 대체) | [`SURFACE_DISPLAY_DESIGN.md`](./SURFACE_DISPLAY_DESIGN.md) |
| A4 | chat-only 헤드리스 CLI (`--headless`) | ✅ landed | §7.2 → `--headless` |
| A5 | `external_binding` 매핑 영속화 | ✅ v1 | `bindings.json`, `EXTERNAL_BINDING_DESIGN.md` |
| A6 | 채팅 display 밀도 (`full`\|`summary`\|`quiet`) | ✅ V1 | `channels/display.py`, observe attach |
| A7 | Gateway backpressure | ✅ mailbox + drop-oldest | `GATEWAY_BACKPRESSURE_DESIGN.md` |
| A8 | Slack inbound / Block Kit | ❌ 미구현(후속) | §6.3/§12 |
| A9 | 다중 human id 매핑 | ❌ 미구현(후속) | §3.2 |
| A10 | `core/`·`channels/` 레포 재구성 | ✅ landed (호환 깨기) | §8 |
| B1 | `session.phase` 이벤트 발행 | ✅ `session._publish_session_phase` | §5.1 |
| B2 | `error` 이벤트 발행 | ✅ `session._publish_session_error` | `events.schema.json` |
| B3 | `human.*`의 `source` Core 기록 | ✅ bridge → `server.human_send` + Wire `message.source` | §5.2 |
| B4 | `ChannelAdapter` / surface 등록 SSOT | ✅ G3–G4 | `gateway/register.py`, attach_* |
| B5 | Gateway 수준 per-agent 구독 필터 | ⚠️ 어댑터 내부 우회 (`surfaces.discord.agents`는 bots 필터만) | §4 |
| B6 | Core step → Wire bind SSOT | ✅ G1–G2 | `bind_session`, `gateway/host.py` |
| C1 | `human.answer`/`human.skip` 스키마 `question_id`·`mentions` | ✅ 스키마 정합 | 런타임 |
| C2 | `human.question` 스키마 `question_id` | ✅ 스키마 정합 | 런타임 |
| C3 | `session.started` `agents` | ✅ `session_stdio` 발행 | `events.schema.json` |
| C4 | `session.ended` `reason` | ✅ 스키마에 `reason` 반영 | — |
| D1 | "토큰이 Surface로 미전달" 불변식 | ✅ `gateway/secrets.py` scrub + secrets file | §11 |
| D2 | 채팅 메시지 청크 분할 | ✅ Hermes식 | `channels/chunk.py` |
| D3 | `_recent_thread` 갱신 조건식 | ✅ `thread.created`만 | `bridge.py` |
| D4 | `DiscordBotAdapter.enqueue` 스레드 안전성 | ✅ `call_soon_threadsafe` | `channels/discord/bot.py` |
| D5 | `publish` 예외 격리 | ✅ surface별 try/except | `bus.py` |
| D6 | Discord webhook mirror 길이 | ✅ D2 청크로 전문 전달 | `discord/mirror.py` |
| D7 | `ask_user` Wire 이중 표현 | ✅ Discord/Slack observe가 `[ask-user]` message 스킵 | observe |

---

## 4. 격차 상세

### 4.1 기능·표면 (A*) — 미결만 상세

> A2/A4/A5/A7/A10은 ✅ (표 §3). 아래는 **아직 열린** 항목.

**A1 — M8 Desktop/Web + WS·HTTP/SSE transport.** `fronts/`에 `ink/`만 존재. Gateway 전송은
`stdio.py`(JSONL)와 in-proc `bus.py` 두 가지만 있고 설계 §6.2가 명시한 WS(Desktop)·
HTTP/SSE(Web-BFF, 인증 포함) transport가 없다. §6.3의 `web-bff`/`desktop-adapter` 전무.
따라서 실제 Interactive Surface는 **Ink 하나**뿐이며 §7.3 "Desktop + Slack" 토폴로지는 실행 불가.
(`auth/oauth.py`·`auth/token_store.py`는 **모델 제공자 OAuth**용이지 Surface/Web 인증이 아니다.)

**A3 — `log.summary` 이벤트.** ⏸️ **deferred.** V1은 Adapter `display` 정책(A6)으로 밀도 조절.
Wire 공통 요약 타입은 여러 surface가 동일 문장을 공유할 필요가 있을 때만 (설계 V3).
→ [`SURFACE_DISPLAY_DESIGN.md`](./SURFACE_DISPLAY_DESIGN.md)

**A6 — 채팅 display 밀도.** ✅ **V1 landed.**
[`SURFACE_DISPLAY_DESIGN.md`](./SURFACE_DISPLAY_DESIGN.md) — `ChatDisplayPolicy`, `display.chat` /
`surfaces.<plat>.display`, Discord·Slack observe 필터·축약. Ink/headless는 무시(설계 §4.3).

**A8 — Slack inbound / Block Kit.** `config.py`·Slack mirror가 `slack.mode`를 `observe`만 허용하고
`interact`를 명시 거부. Block Kit·슬래시/앱 멘션 → `human.*` 변환이 없다.

**A9 — 다중 human id 매핑.** `server.register_human()`은 `"human"` 외 id를 거부하고, Discord inbound는
모든 Discord 사용자를 예약어 `"human"` 하나로 매핑한다.

> **Landed (요약):** A2 `surfaces:` · A4 `--headless` · A5 `bindings.json` · A7 mailbox ·
> A10 `core/`+`channels/` — 표 §3 및 각 설계 문서 참조.
### 4.2 선언·배선 (대부분 ✅; B5만 잔여)

**B1 — `session.phase`.** ✅ `session._publish_session_phase` → Wire.

**B2 — `error` 이벤트.** ✅ `session._publish_session_error` → Wire.

**B3 — `human.*` `source` Core 기록.** ✅ bridge → `server.human_send` + Wire `message.source`.

**B4 — surface 등록 SSOT.** ✅ `gateway/register.py`; attach_* / stdio / headless 경유.
→ [`GATEWAY_SURFACE_BINDING_DESIGN.md`](./GATEWAY_SURFACE_BINDING_DESIGN.md)

**B5 — Gateway 수준 per-agent 구독 필터.** ⚠️ `SurfaceSubscription`에 `agent_ids` 미구현.
`surfaces.discord.agents`는 bots 목록 필터만; 이벤트 fan-out 필터는 여전히
`BotManager.route_event` 내부. ([`GATEWAY_SURFACE_BINDING_DESIGN.md`](./GATEWAY_SURFACE_BINDING_DESIGN.md) §7 슬롯 예약.)

**B6 — Core step → Wire bind.** ✅ `SessionBridge.bind_session` + `bootstrap_gateway_host`;
`attach_session_callbacks` / runner `_publish_step` 제거.
→ 동 설계 G1–G2.

### 4.3 스키마·런타임 (✅)

**C1–C4.** ✅ `question_id`/`mentions` 스키마 정합; `session.started.agents`;
`session.ended.reason` 스키마 반영.

### 4.4 계약·안정성 (✅; 잔여 없음 in D*)

**D1 — 토큰 Surface 미전달.** ✅ `gateway/secrets.py` scrub + secrets file.

**D2 — 청크 분할.** ✅ Hermes식 `channels/chunk.py::split_chat_content`;
bot/mirror/slack enqueue가 분할 (절단만 하던 경로 제거).

**D3 — `_recent_thread`.** ✅ `thread.created`만 갱신.

**D4 — Discord enqueue 스레드 안전.** ✅ `_put_outbox` + `call_soon_threadsafe`.

**D5 — publish 예외 격리.** ✅ `_deliver_safe` (+ A7 chat mailbox).
→ [`GATEWAY_BACKPRESSURE_DESIGN.md`](./GATEWAY_BACKPRESSURE_DESIGN.md)

**D6 — Discord webhook mirror 길이.** ✅ D2 청크 분할로 전문 전달 (과거 단일 절단 대체).

**D7 — ask_user 이중 표현.** ✅ Discord/Slack observe가 `[ask-user]` message 스킵.

---

## 5. 교차 설계 재검증 (요약)

기존 `IMPLEMENTATION_GAP_REPORT.md`가 다룬 나머지 설계 두 종의 결론도 코드로 재확인한 결과를 병기한다.

### 5.1 `AGENT_TOOLS_EXPANSION_DESIGN.md` (v4.1)

- **신규 도구 5종 구현 완료**: `run_command`(`tools.py::_run_command`), `fetch_url`(`web.py::fetch_url_safe`),
  `edit_file`/`append_file`(`tools.py`), `web_search`(LocalTool 트랙 B로 `session.py` 주입). 총 12종 노출.
- **보안 원칙 충족**: `create_subprocess_exec`+`shlex`(P3), SSRF 방어(P4 — CGNAT/IPv4-mapped/정수·hex IP/리다이렉트 재검증),
  `allowed_roots` 배선(P9), `Path.resolve()+relative_to()`(P11), 정확 도메인 서픽스(P10).
- **남은 갭(의도된 deferred)**: SearXNG provider 미구현(v0.9), DNS 재조회·connect-time 검증 미구현(v1.1),
  `docs/TOOLS_OPERATION_GUIDE.md` 미작성, 단위 테스트 일부(`test_tool_policy.py`, `test_run_command.py`,
  `test_file_edit.py`, `test_path_security.py`, `test_cli_roots.py`) 미작성.

### 5.2 `USER_INTERVENTION_DESIGN.md` (v1.1)

- **v1.0 핵심 구현 완료**: `register_human`/`human_send`, `ask_user`, `_agents`/`_humans` 분리,
  예약어 `human` 방어, Discord inbound(M5).
- **v1.1 사람 승인 게이트(`human_approval`)**: ✅ **landed**
  [`HUMAN_APPROVAL_GATE_DESIGN.md`](./HUMAN_APPROVAL_GATE_DESIGN.md)
  — YAML · 모드 **`after_agents`** (에이전트 만장일치 후 사람). 기본 P2~P5 전부 false.
  도구 승인(`TOOL_HUMAN_APPROVAL_DESIGN`)과 별 트랙.
- **v1.2**(파일 드롭, blocking `ask_user_wait`)는 의도된 후순위로 미구현.

---

## 5.3 `AGENT_RELEVANCE_BUDGET_DESIGN.md` (V1 — landed)

> **설계문서:** [`AGENT_RELEVANCE_BUDGET_DESIGN.md`](./AGENT_RELEVANCE_BUDGET_DESIGN.md)  
> **리뷰:** [`AGENT_RELEVANCE_BUDGET_REVIEW.md`](./AGENT_RELEVANCE_BUDGET_REVIEW.md) (G1~G9 / P0 반영)

**V1 구현 단계:**

| ID | 파일 | 내용 | 판정 |
|----|------|------|------|
| V1a | `core/attention.py` | `RelevancePolicy`, `BudgetDecision`, human/URGENT boost, F3 제거 | ✅ |
| V1b | `config.py` | `attention:` 검증 + per-agent floor + `mention_boost >= tiers.skim` | ✅ |
| V1c | `core/agent/loop.py` | T0/T1/T2 분기, `StepResult.skipped`, `format_radio_block_skim` (seq) | ✅ |
| V1d | `core/session.py` | `phase_floor` (near_gate≥1승인 / P1 READY), skipped→continue | ✅ |
| V1e | `tests/test_attention_budget.py` | T0 drain, batch max, human, near_gate, 시나리오 A | ✅ |
| V1f | docs + `examples/attention_budget_demo.yaml` | 갭 문서·예시 YAML | ✅ |

**핵심 계약:**
- V1c+V1d는 같은 변경 집합 (floor 없이 T0만 켜면 합의/READY 회귀)
- 기본 `attention.enabled: false` — 옵트인, 기존 데모·테스트 불변
- T0 = drain 필수 + `backend.complete` 스킵 (`step()` 호출 생략 단독 금지)
- 배치 r = `max(r(m))` (멘션 하나라도 있으면 engage 이상)
- V1 측정 축: `complete` 호출 횟수 + 주입 context chars (max_tokens 합 비교는 V1.1 이월)
---

## 6. 우선순위별 권장 조치

### P0 — 동작·무결성·감사·보안 (대부분 landed)

1. ~~`source` Core 기록 (B3)~~ ✅
2. ~~`question_id` 스키마 정합 (C1/C2)~~ ✅
3. ~~`_recent_thread` 조건식 (D3)~~ ✅
4. ~~Ink spawn env 스크러빙 (D1)~~ ✅
5. ~~`session.phase` / `error` (B1/B2)~~ ✅
6. ~~ask_user 이중 표현 필터 (D7) + mirror 길이 (D6)~~ ✅

### P1 — 설계에 명시된 기능 완성

5. ~~`surfaces:` 통합 YAML (A2)~~ ✅ (본 갱신과 함께)
6. ~~**chat-only 헤드리스 CLI** (A4)~~ ✅ ``--headless`` (채널은 YAML `bots`/`slack`/`surfaces`)
7. **Slack inbound/Block Kit** (A8): `slack.mode=interact` + 슬래시/앱 멘션 → `human.*`.
8. ~~**`human_approval` 게이트**~~ ✅
   [`HUMAN_APPROVAL_GATE_DESIGN.md`](./HUMAN_APPROVAL_GATE_DESIGN.md)
   — `protocol.human_approval` (after_agents, 기본 전부 false).

### P2 — 확장·운영 품질

11. ~~**Gateway backpressure + 예외 격리** (A7/D5)~~ ✅
    [`GATEWAY_BACKPRESSURE_DESIGN.md`](./GATEWAY_BACKPRESSURE_DESIGN.md).
12. **per-agent 구독 필터** (B5): `SurfaceSubscription`에 agent allowlist 추가.
13. ~~**Gateway surface · Core bind SSOT** (B4/B6)~~ ✅ G1–G6 landed
    ([`GATEWAY_SURFACE_BINDING_DESIGN.md`](./GATEWAY_SURFACE_BINDING_DESIGN.md)).
14. ~~**매핑 영속화(`external_binding`)** (A5)~~ ✅ v1 `bindings.json`.
15. ~~**채팅 청크 분할** (D2)~~ ✅ `channels/chunk.py` (Hermes-style).
16. ~~`ask_user` 이중 표현 정리 (D7)~~ ✅
17. ~~**채팅 display 밀도** (A6)~~ ✅ V1 `channels/display.py`.
    A3 `log.summary`는 deferred.
18. **M8 스파이크** (A1): Desktop(WS) 또는 Web(HTTP/SSE + BFF auth) 중 하나 착수.
19. ~~**레포 구조 정리** (A10)~~ ✅ `core/` + `channels/{discord,slack}` (옛 import 경로 제거).

### 빠른 승리 (Quick wins)

전부 landed (D3, C3, C1, D2/D6 청크, A6, …). 잔여 우선순위는 §6 P1–P2의 A8 / B5 / A1.

---

## 7. 결론

`MULTI_FRONT_DESIGN.md` **M0–M7**과 Ink/Discord 운영 품질(P0, A5/A7/A10, B4/B6, D2/D4/D5 등)은
**구현됨**.

**남은 것:**

- **P1:** Slack inbound / Block Kit (**A8**)
- **P2:** Gateway per-agent 필터 (**B5**)
- **후순위 front:** M8 Desktop/Web (**A1**); 다중 human (**A9**)
- A5 v1 이후(Slack 상관 id·TTL 등)는 필요 시 확장

---

## 부록 — 원본 문서별 정밀도 비교 (통합 근거)

| 문서 | 성격 | 정밀도 | 비고 |
|------|------|--------|------|
| `MULTI_FRONT_DESIGN.md` | 핵심 설계(원본) | — | §10 로드맵, §13 결정(D1–D3) 기준 |
| `PROTOCOL_GATE_WAIT_PARK_DESIGN.md` | 구현 설계 | — | **구현 완료** (`session.py`, `test_gate_wait_park.py`) |
| `IMPLEMENTATION_GAP_REPORT.md` | 격차(가장 포괄) | 높음 | MULTI_FRONT + AGENT_TOOLS + USER_INTERVENTION 3종 전수 |
| `ARCHITECTURE_DOCS_VS_IMPLEMENTATION_ANALYSIS.md` | 격차 + 재검증 | 높음 | 스키마 드리프트(G17~G20) 신규 발굴 |
| `DESIGN_VS_IMPLEMENTATION_GAP_VERIFIED.md` | 격차 + 재검증 | 높음 | `attach_session_callbacks` 데드코드 발굴 |
| `ARCHITECTURE_VS_IMPLEMENTATION.md` | 격차 | 중~높음 | 우선순위 P0/P1/P2 정리 양호 |
| `DESIGN_IMPL_AUDIT.md` | 격차 | 중 | G1~G17 + B1~B2 |
| `DESIGN_IMPLEMENTATION_ANALYSIS.md` | 격차 | 중 | G1~G16 + 추가 발견(§6) |
| `MULTI_FRONT_DESIGN_IMPLEMENTATION_GAP.md` | 격차 | 중 | G1~G16 (초기 격차 목록) |
| `implementation_gap_analysis.md` | 격차 | 중 | 명확 미구현/선언만/구조차이 분류 |
| `implement_comparison_analysis.md` | 초기 요약 | 낮음 | 상위 수준, 정밀도 낮음 |

---

## 10. 문서 구성 (정리 후)

| 파일 | 용도 |
|------|------|
| `MULTI_FRONT_DESIGN.md` | Multi-front 기준 설계 |
| `PROTOCOL_GATE_WAIT_PARK_DESIGN.md` | Gate-wait park 설계 (구현됨) |
| `HUMAN_APPROVAL_GATE_DESIGN.md` | 프로토콜 human 승인 (구현됨) |
| `GATEWAY_SURFACE_BINDING_DESIGN.md` | B4/B6 surface·bind SSOT (구현됨) |
| `GATEWAY_BACKPRESSURE_DESIGN.md` | A7/D5 mailbox·예외 격리 (구현됨) |
| `EXTERNAL_BINDING_DESIGN.md` | A5 bindings.json (v1 구현됨) |
| `SURFACE_DISPLAY_DESIGN.md` | 채팅 display/delivery 노브 (A6 V1 ✅; A3 deferred) |
| `AGENT_RELEVANCE_BUDGET_DESIGN.md` | 에이전트 인지 예산 배분 · reasoning budget 설계 (V1 구현 중 — P3) |
| `IMPLEMENTATION_GAP_CONSOLIDATED.md` | 통합 격차 분석 정본 (본 문서) |

중복 gap 분석·pt TUI 아카이브·루트 비교 문서는 삭제됨.
