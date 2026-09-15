# agent-augury 설계 vs 구현 — 통합 격차 정리 (CONSOLIDATED)

> **역할:** Multi-front / protocol 설계 대비 **구현 격차 정본**.
> 형제 문서: `MULTI_FRONT_DESIGN.md`, `PROTOCOL_GATE_WAIT_PARK_DESIGN.md`.
>
> **검증 방식:** 실제 소스(`src/agent_augury/**`, `fronts/ink/**`, `schemas/wire/**`,
> `tests/**`) 기준. 판정은 **코드 존재 여부**.
>
> **작성 시점:** M0–M7 landed · 패키지 `0.6.8` (`pyproject.toml`).
> **갱신:** B1/B2/B3, C1–C4, D1/D3/D6/D7, DESIGN D5(aiosqlite) landed.
> A2 (`surfaces:` YAML) 본 문서와 함께 착수.

---

## 0. 한 줄 결론

**M0–M7 골격(Wire 스키마, Gateway fan-out, Ink, Discord observe/inbound, Slack observe)은
실제 코드로 확인 가능**하고, 핵심 원칙(D1/D2/D3, 관찰 실패 시 Core 무중단, 채널=뷰, `human` 예약어)도
충실히 지켜진다.

반면 ① 설계가 **"후속/선택/옵션/deferred"로 미뤄둔 항목 다수**(A1/A4/A5/…)가 남아 있고,
② Wire 계약 강제·안정화(backpressure, ChannelAdapter, 청크 분할 등)가 남았다.
P0급 스키마/감사/스크러빙 항목(B1–B3, C1–C4, D1/D3/D6/D7)과 aiosqlite 영속화는 **구현됨**.

즉 **"골격 + 핵심 Wire 계약은 정리됐고, 남은 것은 주로 표면 확장(A*)과 운영 품질(P2)"**이 현재 요약이다.

---

## 1. 로드맵(M0–M8) 이행 현황

| 단계 | 설계 명세 | 판정 | 구현 근거 |
|------|-----------|------|-----------|
| M0 | Wire JSON Schema (UI+Chat 공통) | ✅ | `schemas/wire/{envelope,events,commands}.schema.json` + `gateway/types.py` |
| M1 | Gateway in-proc bus + fan-out | ✅ | `gateway/bus.py` `publish`/`dispatch`, observe-only `human.*` 거부 |
| M2 | Ink hello + JSONL stdio | ✅ | `gateway/stdio.py`, `hello_demo.py`, `fronts/ink`, `--ink-hello` |
| M3 | Ink ask_user/interrupt 패리티 + `SessionBridge` | ✅ | `gateway/bridge.py` `human.question` 변환, 옵션 인덱스 해석 |
| M4 | Discord adapter → Gateway 구독 | ✅ | `channel/discord_observe.py` observe surface 등록 |
| M5 | Discord inbound(HITL) | ✅ | `channel/discord_inbound.py` `bots[].inbound` opt-in |
| M6 | Slack observe 스파이크 | ✅ | `channel/slack_observe.py` Incoming Webhook |
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
| rate limit 절단 | Discord `_MAX_CONTENT=1800`, Slack `_MAX_CONTENT=3000` | ⚠️ (절단만, 분할 없음 → D2) |

---

## 3. 격차 종합표 (통합·재번호)

기존 문서마다 G1~G17 번호가 제각각이라, 아래처럼 **A(미구현) / B(선언만·미사용) / C(스키마 드리프트) /
D(계약 미강제·버그)** 로 통일했다.

| # | 항목 | 상태 | 설계 근거 |
|---|------|------|-----------|
| A1 | M8 Desktop/Web + WS·HTTP/SSE transport | ❌ 미구현 | §6.2/§6.3/§10 |
| A2 | `surfaces:` 통합 YAML | ✅ `config.normalize_surfaces` → legacy `mirror`/`bots`/`slack` | §4/§12 |
| A3 | `log.summary` 이벤트 | ❌ 미정의 | §5.1 (옵션) |
| A4 | chat-only 헤드리스 CLI (`--surface none --channels`) | ❌ 미구현 | §7.2 |
| A5 | `external_binding` 매핑 영속화 | ❌ 미구현 | §3.3/§11 |
| A6 | `verbosity: full\|summary` | ❌ 미구현 | §11 |
| A7 | Gateway backpressure | ❌ 미구현 | §6.2 |
| A8 | Slack inbound / Block Kit | ❌ 미구현(후속) | §6.3/§12 |
| A9 | 다중 human id 매핑 | ❌ 미구현(후속) | §3.2 |
| A10 | `core/`·`channels/` 레포 재구성 | ❌ 미이행 | §8 |
| B1 | `session.phase` 이벤트 발행 | ✅ `session._publish_session_phase` | §5.1 |
| B2 | `error` 이벤트 발행 | ✅ `session._publish_session_error` | `events.schema.json` |
| B3 | `human.*`의 `source` Core 기록 | ✅ bridge → `server.human_send` + Wire `message.source` | §5.2 |
| B4 | `ChannelAdapter` 프로토콜 실제 채택 | ⚠️ 형해화 | §3.3/§8 |
| B5 | Gateway 수준 per-agent 구독 필터 | ⚠️ 어댑터 내부 우회 (`surfaces.discord.agents`는 bots 필터만) | §4 |
| B6 | `attach_session_callbacks` 데드 코드 | ⚠️ 미호출 | M3 계약 |
| C1 | `human.answer`/`human.skip` 스키마 `question_id`·`mentions` | ✅ 스키마 정합 | 런타임 |
| C2 | `human.question` 스키마 `question_id` | ✅ 스키마 정합 | 런타임 |
| C3 | `session.started` `agents` | ✅ `session_stdio` 발행 | `events.schema.json` |
| C4 | `session.ended` `reason` | ✅ 스키마에 `reason` 반영 | — |
| D1 | "토큰이 Surface로 미전달" 불변식 | ✅ `gateway/secrets.py` scrub + secrets file | §11 |
| D2 | 채팅 메시지 청크 = 절단만, 분할 없음 | ⚠️ | §11 |
| D3 | `_recent_thread` 갱신 조건식 | ✅ `thread.created`만 | `bridge.py` |
| D4 | `DiscordBotAdapter.enqueue` 스레드 안전성 | ⚠️ 잠재 결함 | `discord_bot.py` |
| D5 | `publish`가 `on_event` 예외 미격리 | ⚠️ 잠재 결함 | `bus.py` |
| D6 | Discord webhook mirror 길이 제한 | ✅ `format_line` 절단 | `discord_mirror.py` |
| D7 | `ask_user` Wire 이중 표현 | ✅ Discord/Slack observe가 `[ask-user]` message 스킵 | observe |

---

## 4. 격차 상세

### 4.1 완전 미구현 (❌)

**A1 — M8 Desktop/Web + WS·HTTP/SSE transport.** `fronts/`에 `ink/`만 존재. Gateway 전송은
`stdio.py`(JSONL)와 in-proc `bus.py` 두 가지만 있고 설계 §6.2가 명시한 WS(Desktop)·
HTTP/SSE(Web-BFF, 인증 포함) transport가 없다. §6.3의 `web-bff`/`desktop-adapter` 전무.
따라서 실제 Interactive Surface는 **Ink 하나**뿐이며 §7.3 "Desktop + Slack" 토폴로지는 실행 불가.
(`auth/oauth.py`·`auth/token_store.py`는 **모델 제공자 OAuth**용이지 Surface/Web 인증이 아니다.)

**A2 — `surfaces:` 통합 YAML.** ✅ `config.load_config`가 `surfaces:`를 검증한 뒤 legacy
`mirror` / `bots` / `slack` 키로 정규화한다. 기존 top-level 키와 동시 지정 시 `ConfigError`.
`surfaces.discord.agents`는 bots 목록 필터에 사용(Gateway event-level filter는 B5로 잔여).

**A3 — `log.summary` 이벤트.** `gateway/types.py` `EVENT_TYPES`와 `events.schema.json` 어디에도
존재하지 않는다. 설계상 옵션이지만 요약 로그를 Wire로 내리려면 스키마·타입 확장부터 필요.

**A4 — chat-only 헤드리스 CLI.** 설계 §7.2의 `agent-augury --surface none --channels discord,slack`에
해당하는 `--surface`/`--channels` 옵션이 `cli.py`에 없다. 채널은 세션 config(`bots:`/`slack:`/`mirror:`)로만
켜지고, Interactive Surface를 끄고 **봇만 띄우는 CI·서버 배포 경로가 없다.**

**A5 — `external_binding`(매핑 영속화).** PlatformRef↔thread_id, PlatformUser↔human, question id 매핑은
전부 런타임 메모리(`BotManager`, `SessionBridge._pending`/`_recent_thread`)에만 있고 저장·복원 코드가 없다.
Adapter/프로세스 재시작 시 Discord 채널↔thread 매핑과 진행 중 질문이 **유실**된다.

**A6 — `verbosity: full|summary`.** `config.py`에 키 검증이 없고, Discord/Slack 어댑터도 이벤트별
full/summary 포맷 전환이 없다.

**A7 — Gateway backpressure.** `bus.py::SessionGateway.publish`는 각 surface의 `on_event`를
**동기 `for` 루프로 즉시 호출**할 뿐 큐/배압/드롭/스로틀 정책이 없다. 느린 surface(Discord outbox 밀림 등)가
있어도 fan-out이 이를 조절하지 못한다.

**A8 — Slack inbound / Block Kit.** `config.py`·`slack_mirror.py`가 `slack.mode`를 `observe`만 허용하고
`interact`를 명시 거부. `slack_mirror.py`는 `{"text": content}` 텍스트만 전송하며 Block Kit·슬래시/앱 멘션 →
`human.*` 변환이 없다.

**A9 — 다중 human id 매핑.** `server.register_human()`은 `"human"` 외 id를 거부하고, `discord_inbound.py`는
모든 Discord 사용자를 예약어 `"human"` 하나로 매핑한다. Discord user id → 개별 human principal 매핑 없음.

**A10 — `core/`·`channels/` 레포 재구성.** 설계 §8의 `src/agent_augury/core/`(session/server/agent 정리)와
`channels/{discord,slack}/base.py`가 미이행. 실제는 `core/` 없이 최상위 `server.py`/`session.py`,
`channel/`(단수) 유지 + `discord_*`/`slack_*` 평면 배치. 기능 회귀는 없으나 "승격·정리" 미수행.

### 4.2 선언만 있고 런타임 미사용 (⚠️ 부분 구현)

**B1 — `session.phase` 정의만 있고 미발행.** `gateway/types.py` `EVENT_TYPES`와 `events.schema.json`에
존재하지만 `make_event("session.phase", …)` 호출이 소스 전역에 **없다**(grep 확인). `protocol/collaboration.py`의
`PhaseManager`/`on_phase_change`는 P1→P5 전환을 추적하지만 `session.py`가 이를 `SessionBridge.publish_core_event`와
연결하지 않는다. → surface는 페이즈 전환을 Wire로 관찰 불가.

**B2 — `error` 이벤트 정의만 있고 미발행.** 오류는 전부 커맨드 응답 `result{ok:false, error:…}`나
`print`로만 전달되고, 서버→surface 방향의 구조화된 오류 이벤트가 없다. surface(채팅)가 세션 오류를
이벤트 스트림에서 관찰할 수 없다.

**B3 — `human.*`의 `source`가 Core에 기록되지 않음.** `discord_inbound.py`는
`source={surface, mode, user, channel}`를 만들어 커맨드에 싣지만, `gateway/bridge.py::_handle_human_message`는
`content`/`thread_id`/`mentions`/`question_id`만 읽고 **`cmd.get("source")`를 버린다.** `server.py`도 `source`를
수용·저장하지 않는다. → Discord 사용자/채널 출처가 **감사·추적에 남지 않는다** (채팅 inbound 감사의 최소 요건 미충족).

**B4 — `ChannelAdapter` 프로토콜 형해화.** `channel/base.py`에 `ChannelAdapter(Protocol)`(`on_wire_event`)가
정의·export되어 있으나 이를 구현하는 클래스가 없다. 실제 Discord/Slack은 `attach_discord_mirror()`/
`attach_discord_bots()`/`attach_slack_mirror()` 자유 함수가 `SurfaceSubscription`을 직접 등록한다.
계약을 강제하려면 구현체가 프로토콜을 채택하거나 프로토콜을 제거해야 한다.
(부차: `base.py`의 `SurfaceMode`와 `bus.py`의 `SurfaceMode`가 중복 정의.)

**B5 — Gateway 수준 per-agent 구독 필터 미지원.** `SurfaceSubscription`은 `event_types` allowlist만 있고
agent 필터가 없다. `surfaces.discord.agents: [agent-1]` 같은 필터는 Gateway 계약에 없고,
`attach_discord_bots`가 `BotManager.route_event(agent_id, …)`로 **어댑터 내부에서** 우회 라우팅한다.

**B6 — `attach_session_callbacks` 데드 코드.** `bridge.py`에 정의만 있고 소스·테스트 어디에서도 호출되지 않는다.
실제 M7은 `session_stdio.py`가 `session.on_step`을 직접 덮어쓰는 방식이라, 콜백 연결 메커니즘이 두 갈래로 중복되고
그중 하나는 미사용이다.

### 4.3 스키마·런타임 드리프트 (⚠️)

**C1 — `human.answer`/`human.skip` 스키마에 `question_id`(및 `human.answer`의 `mentions`) 누락.**
`commands.schema.json`은 `human.answer`에 `content`/`thread_id`/`source`만, `human.skip`에 `source`만 정의한다.
그러나 런타임은 `question_id`에 의존한다(`App.tsx`·`discord_inbound.py`가 전송, `bridge.py::_pop_pending`이 읽음).
`mentions`도 bridge가 `pq.agent_id`로 기본 채우는데 스키마에 없다. → **즉시 정합할 가치가 있는 실제 드리프트.**

**C2 — `human.question` 스키마에 `question_id` 누락.** `events.schema.json`의 `human.question`은
`agent_id`/`thread_id`/`question`/`options`만 정의. `translate.py::_ask_user_to_question`이 생성·발행하고
Ink가 매칭에 사용하는 핵심 상관 키가 스키마에 문서화되지 않음.

**C3 — `session.started`에 `agents` 미발행.** 스키마는 `agents: string[]`를 정의(required 아님)하지만
`session_stdio.py::_session_loop`는 `surface`/`note`만 넣는다. surface가 참가자 목록을 초기 이벤트에서 얻는 경로가 없다.

**C4 — `session.ended` 스키마(`steps`) vs 런타임(`reason`) 불일치.** 스키마는 `steps`(integer)를 문서화하지만
런타임은 `bridge.py`(`reason="quit"`)와 `session_stdio.py`(`reason="eof"`)에서 `reason`을 발행한다.
`steps`는 실제 미발행, `reason`은 스키마에 없다(`additionalProperties`로만 허용).

### 4.4 계약 미강제 / 잠재 버그 (⚠️)

**D1 — 토큰 env 스크러빙 부재.** `cli.py::_run_ink_surface()`는 `env = os.environ.copy()`로 **전체 환경변수를
`npm start`(Node Ink)에 전달**하고, `fronts/ink/src/gateway.ts`도 `env: {...process.env, ...}`로 Python Gateway 자식에
전체 env를 전달한다. 따라서 Ink surface는 `OPENROUTER_API_KEY`, `DISCORD_BOT_TOKEN` 등 **모든 토큰을 상속**받는다.
(로컬 신뢰 프로세스라 치명은 아니나 §11 "토큰을 Surface에 안 넘김" 불변식이 강제되지 않음.)

**D2 — 청크 분할 없음(절단만).** `discord_bot.py` `_MAX_CONTENT=1800`, `slack_mirror.py` `_MAX_CONTENT=3000` 초과 시
`content[:N] + "…"` 단일 절단만 한다. 설계 §11의 "여러 메시지로 나눠 보내기"(청크)는 없어 긴 로그가 잘려 전달된다.

**D3 — `_recent_thread` 갱신 조건식 버그.** `bridge.py`:
```python
elif wire["type"] == "thread.created" and wire.get("thread_id") or wire.get("thread_id"):
    self._recent_thread = str(wire["thread_id"])
```
파이썬 우선순위상 `(A and B) or B`로 해석되어 **`thread.created`가 아니어도 `thread_id`만 있으면** 갱신된다.
현재는 대부분 이벤트가 thread_id를 가져 "마지막으로 본 thread_id"로 동작해 실질 피해는 작지만,
의도("thread.created에서만 갱신")와 다르므로 명시적 괄호/수정 필요.

**D4 — `DiscordBotAdapter.enqueue` 스레드 안전성.** "Thread-safe-ish" 주석이지만 `asyncio.Queue.put_nowait`는
**다른 스레드에서 호출 시 안전하지 않다.** 현재는 동일 루프 호출이라 동작하나, outbox가 별도 스레드(웹훅 수신 등)에서
채워지는 확장 시 `loop.call_soon_threadsafe` 전환이 필요하다. (`session_stdio.py`는 이미 `call_soon_threadsafe` 사용 — 대조적.)

**D5 — `publish`가 `on_event` 예외 미격리.** `bus.py::publish`는 surface의 `on_event`를 try/except 없이 호출한다.
`channel/*`은 내부 try/except로 보호되지만, `stdio.py`의 `on_event`는 stdout에 바로 쓰므로 파이프가 닫히면
`BrokenPipeError`가 나고 **이후 surface로의 fan-out이 중단**될 수 있다. (Core 쪽은 `session._on_server_event`가
try/except로 감싸 보호되지만 fan-out 무결성 자체는 보강 필요.)

**D6 — Discord webhook mirror 길이 제한 없음.** `discord_bot.py`는 `_MAX_CONTENT=1800`으로 절단하지만,
`discord_mirror.py::format_line()`은 절단 없이 `` `[thread_id]` **author**: content `` 를 그대로 만든다.
긴 `message` content는 Discord 2000자 제한 초과로 전송 실패(swallow되어 `errors`에만 남음)할 수 있다.

**D7 — `ask_user` Wire 이중 표현.** `tools.py::ask_user`가 ① `server.send_message(content="[ask-user] …",
mentions=["human"])`로 `message` 이벤트를, ② `on_tool_call`→`tool=ask_user`→`translate.py`가 `human.question`을 발행.
Ink(`wire.ts`)는 `[ask-user]` prefix message를 스킵해 중복이 없지만, Discord observe(`discord_observe.py`)는
`message`를 그대로 포맷하므로 채널에 **`💬 agent: [ask-user] 질문…` 원문 prefix가 노출**된다.
`human.question`으로만 승격하고 `message` 쪽은 채널 observe에서 필터하는 정리 필요.

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
- **v1.1 사람 승인 게이트(`human_approval`) 미구현**: `config.py`에 `human_approval:` 키 검증·배선이 없고
  `REQUEST_APPROVAL:` 패턴 게이트도 없다. 다만 `ConsensusGate`가 `bind_prefixes`를 받으므로
  `participants`에 `"human"`을 포함하면 **비교적 저비용으로 구현 가능한 인프라**는 존재.
- **v1.2**(파일 드롭, blocking `ask_user_wait`)는 의도된 후순위로 미구현.

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
6. **chat-only 헤드리스 CLI** (A4): `--surface none --channels discord,slack` 경로 추가.
7. **Slack inbound/Block Kit** (A8): `slack.mode=interact` + 슬래시/앱 멘션 → `human.*`.
8. **`human_approval` 게이트** (§5.2): `ConsensusGate`에 human 포함 + `REQUEST_APPROVAL:` prefix 지원.

### P2 — 확장·운영 품질

11. **Gateway backpressure + 예외 격리** (A7/D5): `publish`를 큐/비동기 기반으로, 느린 surface 드롭 정책 명시 +
    `on_event` 예외를 surface별로 격리.
12. **per-agent 구독 필터** (B5): `SurfaceSubscription`에 agent allowlist 추가.
13. **`ChannelAdapter` 계약 정비 + 데드 코드 정리** (B4/B6): 구현체가 프로토콜을 채택하거나 프로토콜 제거,
    `attach_session_callbacks` 제거 또는 실제 배선으로 통일.
14. **매핑 영속화(`external_binding`)** (A5): 최소 스텁(DB 컬럼/API)이라도 추가해 재시작 정책 문서화.
15. **채팅 청크 분할** (D2): 긴 로그를 여러 메시지로 분할. (D6 mirror 길이 ✅)
16. ~~`ask_user` 이중 표현 정리 (D7)~~ ✅
17. **`verbosity`·`log.summary`** (A6/A3, 선택·낮은 우선순위).
18. **M8 스파이크** (A1): Desktop(WS) 또는 Web(HTTP/SSE + BFF auth) 중 하나 착수.
19. **레포 구조 정리** (A10): `channel/` → `channels/{discord,slack}` 승격(선택), `core/` 분리(선택).

### 빠른 승리 (Quick wins)

- `bridge.py::_recent_thread` 조건식 괄호 수정 (D3)
- `session.started`에 `agents` 필드 추가 (C3)
- `human.answer`/`human.skip` 스키마 `question_id` 정합 (C1)
- `discord_mirror.py::format_line`에 길이 절단 추가 (D6)
- 버전 표기 통일 (설계 "v0.2.7" vs `pyproject.toml` "0.6.5")

---

## 7. 결론

`MULTI_FRONT_DESIGN.md`가 주장하는 **M0–M7은 실제 코드로 확인 가능**하고, 핵심 원칙(D1/D2/D3,
observe 실패 무중단, 채널=뷰, `human` 예약어)도 충실히 지켜진다.

반면:

- **"후속/선택/옵션" 확장**이 남았다 (헤드리스 CLI, Slack inbound, M8, `external_binding`, `verbosity`).
- **운영 품질** (backpressure, ChannelAdapter, 청크 분할)이 남았다.
- Wire P0(스키마·phase/error/source·env scrub·ask_user 필터)와 **`surfaces:` YAML(A2)** 는 정리됨.

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
| `IMPLEMENTATION_GAP_CONSOLIDATED.md` | 통합 격차 분석 정본 (본 문서) |

중복 gap 분석·pt TUI 아카이브·루트 비교 문서는 삭제됨.
