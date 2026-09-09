# agent-augury — 사용자 개입(Human-in-the-Loop) 설계안

> **Task:** agent-augury 프로젝트 분석 + 사용자 개입 / 에이전트→사용자 의견 요청 창구 부재 문제의 구현 방향 설계
> **Date:** 2026-09 · agent-1 (설계) · 검토: agent-2, agent-3, agent-4 (대기/지원)
> **Scope:** 설계 문서 (구현 코드 아님). DESIGN.md §3.5.2(A 모델, inbox push → step() drain) 철학을 유지하며 사람을 참가자로 모델링.
> **Rev:** 2026-09 · v1.1 — `human` 예약어 충돌 시나리오(S1~S4) 대응 설계 추가 (§3.2)

---

## 1. 배경: 무엇이 부재한가

agent-augury는 **에이전트 간(agent↔agent)** 통신만 지원한다. 사용자는 세션 **시작 전**(위저드/초기 task 입력)에만 개입할 수 있고, 세션 **도중**에는 어떤 경로로도 개입할 수 없다. 에이전트가 사용자에게 질문하거나, 중요한 결정(제출, 파일 쓰기, 방향 전환) 전에 사용자 확인을 받을 **창구가 전혀 없다.**

### 1.1 현재 사용자 접점 (코드 근거)

| 접점 | 위치 | 세션 중 개입 가능? |
|------|------|--------------------|
| 인터랙티브 위저드 (백엔드/에이전트 구성) | `cli.py` → `_run_wizard_flow()` / `wizard.py` | ❌ (시작 전에만) |
| 초기 task 입력 | `_run_wizard_flow()`의 `input("What would you like to do?")` | ❌ (1회, 세션 시작 시) |
| `--config` 모드 | `cli.py` → `_run()` — `session.run()` 후 종료 | ❌ (도중 입력 경로 없음) |
| `--repl` 모드 | `_run_repl()` — 세션 *간* 질문 수용 | ⚠️ (세션 도중 아님, 라운드 사이) |
| Discord 봇 / 웹훅 미러 | `channel/discord_bot.py`, `discord_mirror.py` | ❌ (발신 전용, 수신 없음) |

### 1.2 근본 원인 (아키텍처 레벨)

1. **MessageServer는 `agent`만 참가자로 인식** — `register_agent()`가 inbox를 만들고, `send_message()`의 `author`는 반드시 참가자(에이전트)여야 한다. 사람(`human`)을 표현하는 스키마/API가 없다. (`server.py` §`send_message`, `register_agent`)
2. **수신 경로가 inbox push + `step()` drain 하나뿐** (DESIGN.md §3.5.2, A 모델). 사용자 메시지가 push될 inbox가 없다.
3. **에이전트→사용자 역방향 도구 부재** — `tools.py`에는 `create_thread`/`send_message`/`read_resource`/파일 도구만 있다. "질문하기" 프리미티브가 없다.
4. **P1~P5 게이트가 에이전트 간 합의 전용** — `ConsensusGate`의 `participants`는 에이전트 ID로 채워진다. human 승인 지점이 없다. (`approval.py`, `collaboration.py`, `session.py` `from_config`)
5. **채널은 읽기 전용 미러** — DESIGN.md §3.3 "SSOT는 내부 서버, 채널은 뷰". Discord 봇은 `on_message` 핸들러를 **의도적으로 등록하지 않는다** (`discord_bot.py` docstring: "No on_message handler is registered"). 양방향이 되려면 이 설계 결정의 부분 수정이 필요하다.
6. **(신규) `human`이라는 id가 예약어가 아니어서 충돌 가능** — 지금은 에이전트가 `id: human`으로 등록될 수 있고, 추후 `register_human("human")`과 부딪히거나 author 문자열이 오인될 여지가 있다. (상세: §3.2)

### 1.3 왜 필요한가

- **안전성:** 에이전트가 자율적으로 파일을 쓰거나, 최종 답을 제출하거나, 비용이 드는 작업(실제 API 호출)을 하기 전 사용자 확인.
- **품질:** 모호한 task를 에이전트가 추측으로 진행하지 않고 사용자에게 명확화.
- **방향 제어:** 사용자가 세션 도중 "그 방향 말고 이쪽으로" 개입.
- **신뢰:** 멀티에이전트가 블랙박스처럼 돌고 나서 결과만 주는 UX → 중간 확인으로 신뢰 확보.

---

## 2. 설계 목표와 원칙

### 2.1 목표

1. 에이전트가 **언제든 사용자에게 질문/확인을 요청**할 수 있다.
2. 사용자가 **세션 도중** 메시지를 보내 개입할 수 있다.
3. **중요한 게이트(제출/파일 쓰기/방향 전환)에 사람 승인**을 끼울 수 있다.
4. 기존 **패시브 어웨어니스(L3) 철학을 깨지 않는다** — 사용자 메시지도 inbox push → `step()` drain으로 흡수.
5. **(신규) `human` 예약어 충돌 없이** 기존 에이전트 등록/메시지 라우팅을 안전하게 유지한다.

### 2.2 원칙

| # | 원칙 | 근거 |
|---|------|------|
| P1 | **사람을 "특별한 에이전트"(HumanParticipant)로 모델링**하되, **에이전트와 별도 네임스페이스/레지스트리(`_humans`)로 관리** | 참가자 추상화 재사용 + id 문자열 충돌 원천 차단 |
| P2 | **사용자 메시지도 기존 inbox push → [radio] 경로로 주입** | L3 패시브 어웨어니스 그대로 |
| P3 | **`ask_user`는 기본 fire-and-forget** (blocking은 옵션) | `send_message`의 즉시 반환 철학과 일치 |
| P4 | **사람 승인은 기존 `ConsensusGate` 패턴 재사용** | P1~P5 게이트 인프라를 그대로 활용 |
| P5 | **기본은 옵트인** — `human:` 섹션 없으면 기존 동작 100% 호환 | 마이그레이션 비용 0 |
| P6 | **채널(Discord)은 여전히 뷰** — 단, 양방향이 필요한 경우에만 수신 경로를 명시적으로 추가 | SSOT 원칙 유지 |
| P7 | **(신규) `human`은 예약어 — 에이전트 id로 사용 불가** (`register_agent`/config 로드 시점에 거부) | author 문자열 오인/중복 등록 원천 차단 |

---

## 3. 권장 아키텍처

```
                          ┌───────────────────────────────┐
                          │           사용자 (human)       │
                          │  CLI(--interactive) / Discord  │
                          └──────────────┬────────────────┘
                                         │ human_send() / 응답
                                         ▼
   ┌─────────────────────────────────────────────────────────┐
   │              MessageServer (SSOT)                       │
   │  · _agents: {agent-1..N}   (에이전트 전용 레지스트리)    │
   │  · _humans: {human}        (사람 전용 레지스트리, 분리)  │
   │  · inboxes: agent별 asyncio.Queue + human용 Queue       │
   │  · send_message: 기존 로직 (author ∈ _agents 전용)       │
   │  · human_send(): human을 대신해 메시지 추가 → 에이전트   │
   │    inbox push (기존 fan-out 규칙 §3.5.3 그대로)          │
   └──────────────┬──────────────────────────┬──────────────┘
                  │                          │
        ┌─────────▼─────────┐      ┌─────────▼─────────┐
        │  AgentLoop (N개)   │      │  HumanAdapter     │
        │  step()이 inbox    │      │  (CLI/Discord)    │
        │  drain → [radio]   │      │  · 입력 태스크     │
        │  · ask_user 도구   │      │  · human inbox     │
        └───────────────────┘      │    drain → 표시    │
                                   └───────────────────┘
```

### 3.1 핵심 통찰

기존 시스템에서 **에이전트 간 통신은 "참가자 id + inbox push"로 완전히 추상화**되어 있다. 사람을 `"human"`이라는 id의 참가자로 등록하면:

- 에이전트가 `send_message(thread, ..., mentions=["human"])`로 질문을 보낼 수 있고, 서버가 human inbox로 push.
- 사용자가 응답하면 `human_send()`가 에이전트 inbox로 push → 다음 `step()`에서 `[radio]` 블록으로 흡수.
- **새 통신 메커니즘이 필요 없다.** 기존 프리미티브의 참가자 집합만 확장하면 된다.
- 단, **"참가자 집합 확장"은 에이전트 집합에 human을 섞는 것이 아니라 별도 레지스트리(`_humans`)를 두는 방식**으로 한다 — 그래야 `human`이라는 id 문자열이 에이전트 id와 충돌하거나 오인될 여지가 없다. (§3.2)

---

### 3.2 예약어 `human`과 이름 충돌 방지 (신규 — 핵심 설계 결정)

#### 3.2.1 충돌 시나리오 (인식된 위험)

| # | 시나리오 | 현행 설계에서의 결과 |
|---|----------|----------------------|
| S1 | 사용자가 에이전트에 `id: human`을 정함 (지금은 허용됨) → `register_agent("human")`이 먼저 `_agents`에 등록 | `_agents = {..., "human"}` — 사람 전용 id가 에이전트에 선점됨 |
| S2 | 이후 `register_human("human")` 시도 | 이미 존재(중복) 예외 **또는** 참가자 집합이 꼬임 (같은 id가 에이전트/사람 양쪽 의미) |
| S3 | 에이전트가 `send_message(..., author="human")`으로 전송 | `"human"`이라는 author 문자열 때문에 **사용자 메시지로 오인** (표시/라우팅/게이트 참여 혼선) |
| S4 | 반대로 사용자 메시지(`human_send` author="human")가 에이전트 취급 | 팬아웃 대상·게이트 `participants`에 에이전트로 잘못 포함 |

#### 3.2.2 설계 결정: 이중 방어 (defense in depth)

1. **예약어 차단 (등록/로드 시점 거부)**
   - `RESERVED_NAMES = {"human"}` — **대소문자 무시**(`human`, `Human`, `HUMAN` 모두 거부).
   - `register_agent(id)`: `id.lower() in RESERVED_NAMES`이면 `ReservedNameError` raise.
   - `config.py` 로더에서도 동일 검증 → `id: human` 에이전트는 **늦은 등록이 아니라 config 로드 시점에 실패** (fail fast).
2. **네임스페이스 분리 (별도 레지스트리)**
   - `_agents: set[str]` — 에이전트 전용.
   - `_humans: set[str]` — 사람 전용 (기본 `{"human"}`).
   - 참가자 해석은 "문자열이 `_humans`에 있으면 사람"이라는 **등록 레지스트리 기준**으로 결정. 에이전트 id로 `human`이 불가능하므로 author 문자열만으로 오인할 여지 자체가 없음.
3. **발신 경로 강제 (위조 차단)**
   - `send_message()`: `author`가 `_agents`에 속한 에이전트일 때만 허용 → 에이전트가 `author="human"`으로 **위조 불가** (S3 차단).
   - `human_send()`: `author`가 `_humans`에 속할 때만 허용 → 사용자 메시지가 에이전트로 취급될 수 없음 (S4 차단).
4. **(선택, 심화) 메시지 역할 필드**
   - 스키마 변경을 허용한다면 `Message.sender_kind ∈ {"agent", "human"}`을 저장해 표시/라우팅 시 문자열 추론을 아예 금지. 방어 1~3이 이미 차단하므로 **필수는 아님** — 기본 설계는 기존 스키마 그대로 유지.

#### 3.2.3 마이그레이션/호환 (breaking change 명시)

- 기존 config에 `id: human` 에이전트가 있다면(현재 허용), 신규 로더는 **거부 + 명확한 에러 메시지** 출력:
  `agent id 'human' is reserved for the human participant; rename the agent (e.g. 'human-relay')`.
- human 기능을 쓰지 않는 기존 세션에도 예약어 검증은 적용된다. 이는 **의도된 breaking change**이며 v1.0 릴리스 노트에 명시한다 (S1~S4를 막는 대가).
- 대소문자 변형(`Human`/`HUMAN`)으로 우회하는 config도 동일하게 거부 — 테스트로 고정 (§6).

---

## 4. 컴포넌트별 설계

### 4.1 MessageServer 확장 (`server.py`)

```python
RESERVED_NAMES = frozenset({"human"})   # 대소문자 무시 비교

class ReservedNameError(ValueError):
    """에이전트/사람 예약어를 id로 사용하려 할 때."""

# 기존: 에이전트 전용 레지스트리 — 예약어/사람 id 중복 차단 추가
def register_agent(self, agent_id: str) -> None:
    if agent_id.lower() in RESERVED_NAMES:
        raise ReservedNameError(
            f"'{agent_id}' is a reserved name for the human participant; "
            f"choose another agent id.")
    if agent_id in self._humans:
        raise ValueError(f"agent id '{agent_id}' collides with a registered human id")
    self._agents.add(agent_id)
    self._inboxes[agent_id] = asyncio.Queue()

# 신규: 사람 전용 레지스트리 — _agents와 분리
def register_human(self, human_id: str = "human") -> None:
    """사람을 참가자로 등록 (inbox 생성). _agents와 분리된 _humans 사용.

    - human_id는 v1.0에서 예약어 'human' 고정 (대소문자 무시).
    - _agents와 충돌 불가: register_agent가 이미 예약어를 거부하므로
      이 시점에 'human'이 에이전트로 등록돼 있을 수 없다.
    """
    if human_id.lower() != "human":
        raise ValueError("human id must be 'human' in v1.0 (reserved namespace)")
    self._humans.add(human_id)
    self._inboxes[human_id] = asyncio.Queue()

# 기존 send_message: 에이전트 author만 허용 (human 위조 차단 — S3)
async def send_message(self, thread_id: str, *, author: str, content: str,
                       mentions: list[str] | None = None) -> str:
    if author not in self._agents:
        raise ValueError(f"author '{author}' is not a registered agent")
    ...

# 신규 human_send: human author만 허용 (에이전트 위조 차단 — S4)
async def human_send(self, thread_id: str, *, author: str, content: str,
                     mentions: list[str] | None = None) -> str:
    """사용자(또는 채널 어댑터)가 보낸 메시지를 서버에 주입.

    - author는 _humans에 등록된 id만 허용 (v1.0에서는 "human").
    - mentions가 비면 스레드 participants(에이전트 전원)로 fan-out
      (기존 §3.5.3 규칙 재사용).
    """
    if author not in self._humans:
        raise ValueError(f"author '{author}' is not a registered human")
    ...
```

- **mentions/참가자 해석:** `"human"`은 `_humans` 레지스트리로, 나머지는 `_agents`로 해석. 둘 다 없으면 `UnknownParticipantError`.
- 스냅샷/`read_resource`에 human이 자연 포함됨 (스키마 변경 없음). `author="human"`은 예약어 덕분에 **유일하고 모호하지 않다**.
- 영속화(aiosqlite)도 기존 스키마 그대로 — human 메시지는 `messages` 테이블에 `author="human"`으로 저장.

### 4.2 에이전트 도구: `ask_user` (`tools.py`, `loop.py`)

```python
# ToolBox.specs()에 추가
{
    "name": "ask_user",
    "description": (
        "Ask the human user a question or request confirmation. "
        "Fire-and-forget: returns immediately; the user's reply arrives "
        "later as a [radio] message from 'human'. Use options to give "
        "clear choices. Prefix important requests with REQUEST_APPROVAL: "
        "when a human gate is configured."
    ),
    "schema": {
        "type": "object",
        "properties": {
            "thread": {"type": "string", "description": "thread id"},
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": "optional answer choices"},
        },
        "required": ["thread", "question"],
    },
}

# ToolBox.execute()에 추가
if name == "ask_user":
    content = f"[ask-user] {args['question']}"
    if args.get("options"):
        content += "  (옵션: " + " / ".join(args["options"]) + ")"
    mid = await self.server.send_message(
        args["thread"], author=agent_id,
        content=content, mentions=["human"],
    )
    return _json({"message_id": mid, "status": "question_delivered"})
```

- **기본 동작:** fire-and-forget. 에이전트는 반환 직후 계속 일한다 (L3 철학).
- **응답 흡수:** 사용자 답변이 `human_send()`로 에이전트 inbox에 push → 다음 `step()`의 `[radio]` 블록에 `from human: ...`로 도착.
- `mentions=["human"]`은 `_humans` 레지스트리로 해석되어 human inbox로 push된다 (에이전트 id와 혼동 불가).
- **blocking 옵션 (선택, v1.1):** `ask_user_wait(thread, question, timeout)` — `asyncio.Event`로 응답 대기. **기본값은 비블로킹**이며, "이 응답 없이는 진행 불가"인 경우에만 사용. (L3의 패시브 원칙과의 충돌을 최소화하려면 비블로킹을 강력 권장.)

### 4.3 시스템 프롬프트 확장 (`system_prompt.py`)

human 참가자가 config에 있으면 시스템 프롬프트에 다음 블록 추가:

```
Human-in-the-loop rules:
- `human` (the user) is a member of this team.
- Use `ask_user` to ask questions or request confirmation. It is
  fire-and-forget — keep working; the reply arrives later as [radio].
- If the task is ambiguous or an important decision (submission, file
  write, direction change) is coming, ask the user FIRST.
- User replies appear as `from human: ...` in [radio] blocks.
- Match the user's language when asking.
```

### 4.4 사람 승인 게이트 (Human Approval Gate) — `protocol/` 재사용

기존 `ConsensusGate`를 그대로 재사용한다. 핵심: **participants에 human을 포함**시키면 된다.

```yaml
# config 예시 (v1.1)
protocol:
  participants: [agent-1, agent-2, agent-3, human]
  assembler_id: agent-1
  gates:
    P2_SPLIT: plan
    P3_EXECUTE: execution
    P4_REVIEW: review
    P5_SUBMIT: submission
  human_approval:
    P5_SUBMIT: true          # 제출 전 사람 승인 필수
```

- 에이전트가 `REQUEST_APPROVAL: <최종안 요약>` 전송 → human이 `APPROVE:` / `REJECT:` 응답.
- `ConsensusGate.bind_prefixes`에 `["REQUEST_APPROVAL:"]` 사용, `require_proposal=True`.
- `REJECT:` 시 기존처럼 승인 리셋 → 수정 후 재요청.
- **P1~P5 게이트 코드 변경 없음** — participants에 human이 들어갈 뿐. participants 해석 시 `"human"`은 `_humans` 레지스트리 기준으로 검증한다 (에이전트 id와의 혼동 방지, §3.2).

### 4.5 사용자 인터페이스 (HumanAdapter)

#### v1: CLI `--interactive` 모드 (권장 첫 구현)

```
agent-augury --config session.yaml --interactive
```

- 세션 실행 중 **별도 asyncio 태스크**가 `input()`을 받는다 (`loop.run_in_executor`로 블로킹 회피).
- 사용자 입력 → `server.human_send(thread_id, author="human", content=..., mentions=[에이전트들])`.
- 에이전트의 `ask_user` 메시지는 CLI에 `👤 질문: ...` 형태로 즉시 표시 (기존 `_log_tool_event` 확장).
- 사용자가 입력하지 않으면 세션은 **계속 진행** (패시브 — 개입은 옵션).
- `--interactive` 없으면 기존 자동 모드와 100% 동일.

#### v1.1: Discord 봇 수신 (선택)

- `DiscordBotAdapter`에 `on_message` 핸들러 추가 (멘션/채널 라우팅).
- DESIGN.md §3.3의 "채널은 뷰" 결정을 **양방향 옵션으로 부분 완화**: 기본은 발신 전용 유지, `bots[].inbound: true`일 때만 수신 활성화.
- Discord 메시지 → `server.human_send()`로 주입. **SSOT는 여전히 MessageServer** (채널은 입출력 어댑터일 뿐).
- Discord 사용자 id와 `"human"` 예약어 간 매핑은 HumanAdapter가 담당 (서버는 `_humans` 레지스트리만 봄).

#### v1.2: 파일 드롭 (선택)

- `human_inbox/` 디렉토리에 사용자가 `.md`/`.txt` 파일을 넣으면 서버가 읽어 `human_send()`.
- 비동기 환경·CI에서 사람 개입이 어려운 경우 유용.

---

## 5. config 스키마 확장

```yaml
# 기존 키는 그대로, 신규 섹션만 추가
human:
  id: human                 # v1.0 고정(예약어) — 변경 불가 (§3.2)
  display_name: "사용자"     # 선택, 로그/프롬프트 표시용
  interface: cli             # cli | discord | file (v1: cli만)
  auto_reply: null           # 선택: 자동 응답 (테스트/데모용)

protocol:
  ...
  human_approval:
    P5_SUBMIT: true

# v1.1 (Discord 양방향)
bots:
  - agent_id: agent-1
    token_env: BOT_TOKEN_AGENT_1
    channel_id: 123456789012345678
    inbound: true            # ← 신규: 사용자 메시지 수신 허용
```

- **검증 규칙 (`config.py`):**
  1. 에이전트 id는 `RESERVED_NAMES`(`human`, 대소문자 무시) 사용 불가 → 위반 시 로드 실패 + rename 안내 에러.
  2. 에이전트 id와 human id 간 중복 불가 (레지스트리 분리로 구조적으로 불가하나, config 레벨에서도 1차 검증).
  3. `human:` 섹션 **없으면** 기존 동작 그대로 (호환성 100%) — 단, 예약어 검증은 human 기능 유무와 무관하게 항상 적용 (§3.2.3).
- `human.id`는 v1.0에서 `"human"` 고정. 추후 다중 사용자를 지원하려면 `human.<n>.id` 형태로 확장하되, 그때도 에이전트/사람 레지스트리 분리와 충돌 검증은 동일하게 유지.
- `Session.from_config`에서 `human` 등록 → 시스템 프롬프트에 HITL 블록 주입 → `ask_user` 도구 노출.

---

## 6. 구현 로드맵

| 단계 | 범위 | 산출물 | 통과 기준 |
|------|------|--------|-----------|
| **v1.0** | 서버 `register_human`/`human_send`, `ask_user` 도구, CLI `--interactive`, 프롬프트 HITL 블록, **예약어/중복 id 검증** | `server.py`, `tools.py`, `loop.py`, `system_prompt.py`, `cli.py`, `config.py` | Fake 백엔드 E2E: 에이전트가 `ask_user` → human 응답 주입 → `[radio]` 흡수 → 최종 결과 반영 + **예약어 충돌 시나리오(S1~S4) 전부 거부** |
| **v1.1** | human 승인 게이트 (`human_approval`), Discord 수신(옵트인) | `session.py`, `collaboration.py`, `discord_bot.py` | P5 제출 전 human `APPROVE:` 없이는 게이트 미개방 |
| **v1.2** | 파일 드롭, `ask_user_wait`(blocking 옵션), 응답 타임아웃/정책 | `channel/file_adapter.py` 등 | 타임아웃 시 에이전트가 우아하게 기본 경로 진행 |

### v1.0 통과 기준 (자동 검증)

```
시나리오 A: 에이전트-1이 task를 수행하다가 모호한 지점에서 ask_user.
- Fake ModelBackend로 에이전트-1의 tool 시퀀스 고정: ask_user 호출.
- 테스트에서 human_send()로 "옵션 B로 진행" 응답 주입.
- 에이전트-1의 다음 step()이 [radio]로 응답을 흡수하고,
  최종 답에 "옵션 B"가 반영됨.

단언:
  assert ask_user 반환 status == "question_delivered"
  assert human 메시지가 에이전트-1 inbox에 push됨
  assert 에이전트-1의 최종 텍스트에 응답 내용 포함

시나리오 C (예약어 충돌 방지 — S1~S4):
  assert register_agent("human")      raises ReservedNameError   # S1 차단
  assert register_agent("Human")      raises ReservedNameError   # 대소문자 무시
  assert config 로드(agent id: human) 실패 + rename 안내 메시지   # S1 (fail fast)
  assert register_human("human")      정상 (S2: 중복 불가능, _humans 분리)
  assert send_message(author="human") raises ValueError          # S3 차단 (위조 불가)
  assert human_send(author="human")   정상 push                  # 정상 경로
  assert human_send(author="agent-1") raises ValueError          # S4 차단 (위조 불가)
```

---

## 7. 리스크와 대가

| 리스크 | 완화 |
|--------|------|
| blocking `ask_user`가 L3 패시브 철학 훼손 | 기본은 fire-and-forget. blocking은 명시적 옵션 + 타임아웃 |
| 사용자 응답 대기 중 에이전트가 완료 상태로 빠짐 | 프롬프트 지침("질문 후에도 계속 작업, 응답은 [radio]로 옴"), 테스트로 검증 |
| `max_steps` 예산이 응답 대기 중 소진 | (선택) ask_user 후 대기 중인 에이전트의 step 예산 보호 로직 |
| Discord 수신이 SSOT 원칙 위반으로 보임 | 기본 발신 전용 유지, `inbound: true` 명시 시에만 수신 — 채널은 여전히 어댑터 |
| 사람이 응답 안 함 → 세션 교착 | 타임아웃/기본 경로 정책(에이전트가 기본 가정으로 진행 후 이력 보고) |
| human이 모든 메시지에 노출 → 컨텍스트 노이즈 | `ask_user` 질문/응답만 human 메시지로 취급, 일반 방송은 기존대로 에이전트 전용 |
| **(신규) 기존 config의 agent id `human` → 로드 실패** | v1.0 릴리스 노트에 breaking change 명시 + 명확한 에러 메시지(rename 안내) |
| **(신규) 대소문자 변형(`Human`/`HUMAN`)으로 예약어 우회** | 예약어 검증은 `lower()` 기준, 시나리오 C 테스트로 고정 |
| **(신규) 추후 다중 사용자 확장 시 id 충돌** | `human.<n>` 네임스페이스 도입 + `_agents`/`_humans` 레지스트리 분리 유지 |

---

## 8. 변경 파일 요약

```
src/agent_augury/
  server.py               # _humans 레지스트리 분리, register_human(), human_send(),
                          # ReservedNameError, send_message/human_send 발신 경로 강제
  agent/tools.py          # ask_user 도구 (spec + execute)
  agent/system_prompt.py  # HITL 규칙 블록 (human 존재 시)
  agent/loop.py           # (변경 최소) [radio] 포맷에 from human 자연 포함
  session.py              # human 등록, human_approval 게이트 바인딩
  protocol/collaboration.py # human_approval 설정 파싱 (게이트 재사용)
  cli.py                  # --interactive 플래그, human 입력 태스크, ask_user 로그 표시
  config.py               # human:, protocol.human_approval, bots[].inbound 검증
                          # + 예약어(RESERVED_NAMES)/중복 id 검증 (신규)
  channel/discord_bot.py  # (v1.1) inbound 옵션, on_message 핸들러
examples/
  human_in_the_loop.yaml  # v1.0 데모 (fake 백엔드)
tests/
  test_human_in_the_loop.py  # v1.0 통과 기준 자동 검증 (시나리오 A)
  test_reserved_names.py     # 예약어/발신 경로 경계 검증 (시나리오 C, S1~S4)
```

---

## 9. 결론

사용자 개입/의견 요청 창구 부재는 **새 통신 메커니즘을 만드는 문제가 아니라, 기존 "참가자 id + inbox push" 추상화에 사람을 포함시키는 문제**다. 이때 **`human` 예약어 + `_agents`/`_humans` 레지스트리 분리 + 발신 경로 강제**라는 이중 방어를 함께 도입하면:

- 아키텍처 파괴 없이 (SSOT/L3 패시브 어웨어니스 유지)
- 기존 config 호환 (옵트인) — 단, `id: human` 에이전트만 breaking change (명확한 에러로 안내)
- 코드 변경 최소화 (server/tools/prompt/session/cli/config 중심)
- **S1~S4 충돌 시나리오(중복 등록·author 오인·위조)를 원천 차단**

로 사용자 개입 창구를 구현할 수 있다. **v1.0(CLI 인터랙티브 + ask_user + human_send + 예약어 검증)을 먼저 검증**하고, v1.1(사람 승인 게이트 + Discord 수신), v1.2(파일 드롭)로 확장하는 것을 권장한다.
