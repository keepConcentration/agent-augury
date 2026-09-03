# agent-augury — 독립 오픈소스 설계 문서

> 상태: v0.2 구현 완료 · 2026-08-27 · 프로젝트명 `agent-augury` (D1 확정)
> 범위: "패시브 어웨어니스(Passive Awareness) 멀티에이전트" 개념을 담은, Hermes와 독립된 오픈소스 프로젝트
> 이 문서는 Coral-Protocol [AgentRadio](https://github.com/Coral-Protocol/AgentRadio)의 **개념을 계승**하되 독립 재구현한다. 원본 레포명 `AgentRadio`는 상단의 링크·참고에서만 그대로 쓰고, **이 프로젝트 자기 이름은 `agent-augury`다.**

---

## 1. 배경과 목표

### 1.1 이 프로젝트가 뭘 하는가

Coral-Protocol의 [AgentRadio](https://github.com/Coral-Protocol/AgentRadio) (논문: [arXiv:2607.28430](https://arxiv.org/abs/2607.28430))는 "네 명의 코딩 에이전트가 공유 라디오 채널을 쓰면서, 일하는 **동안** 듣는다(패시브 어웨어니스)"는 아이디어를 제안했다. 핵심 통찰은 다음 하나로 요약된다:

> 메시지를 듣는 걸 **전경(blocking)** 이 아니라 **배경(background) 태스크**로 돌리면, 에이전트가 일을 멈추지 않고도 동료의 발견을 다음 스텝 경계에서 자연스럽게 흡수한다. 통신이 더 이상 일을 방해하지 않는다.

원본(AgentRadio)은 이 아이디어를 **Claude Code CLI 4개 + Coral 메시지 서버(JAR) + Harbor/Modal 클라우드**로 재현하는 논문 실험 코드다. 즉, 아래 셋에 묶여 있다:

1. **모델 고정** — Claude Code(Anthropic Messages API)만 말함. 다른 모델은 LiteLLM 번역 프록시를 거쳐야 함.
2. **채널 고정** — 에이전트 간 통신이 전용 메시지 서버(JAR)로 닫혀 있음.
3. **런타임 고정** — Harbor/Modal/Docker 컨테이너 위에서만 돌도록 설계됨.

### 1.2 우리가 만드는 것

이 프로젝트(`agent-augury`)는 **AgentRadio의 "개념(패시브 어웨어니스 + 협업 프로토콜)"을 계승**하되, 모델·채널·런타임 어느 것에도 묶이지 않는 독립 오픈소스로 재구현한다. 협업 프로토콜 전체(P1~P5)는 장기 목표로 계승하고, **구현은 v0.2에서** 다룬다 (§6).

| 차원 | 원본 AgentRadio | 이 프로젝트 |
|------|----------------|-------------|
| 에이전트 모델 | Claude Code 전용 (프록시 경유 다른 모델) | **모델 무관** (OpenAI-compatible 1순위, Nous Portal 2순위) |
| 라디오 채널 | 전용 메시지 서버(JAR) | **내부 메시지 서버(경량 in-process)가 SSOT**, 채널은 읽기 전용 미러 |
| 런타임 | Harbor + Modal + Docker | **로컬 Python 프로세스** (self-host, asyncio) |
| 에이전트 생성 | 고정 4개 | **사용자가 동적으로 생성/구성** |
| 배포 | 논문 재현 아티팩트 | **pip 설치 가능한 오픈소스 패키지** |

### 1.3 비목표 (첫 릴리스에서 하지 않음)

- 코딩 도구(파일 읽기/쓰기/셸 실행)를 갖춘 완전한 코딩 에이전트 — **1단계는 협업/대화 중심**, 코딩 도구는 이후 플러그인.
- Harbor/Modal/Docker 연동.
- coral-server.jar 재사용 — 경량 **in-process** 서버(SQLite 또는 메모리)로 직접 재구현함 (D5, §3.3~§3.4 참고).
- multi-agent 자동 스케줄링·오케스트레이션 플랫폼(Hermes 칸반류).

---

## 2. 핵심 개념 (이 프로젝트가 계승하는 것)

원본에서 우리가 **가져오는 것**은 코드가 아니라 다음의 "프로토콜"이다.

### 2.1 세 가지 통신 프리미티브

| 프리미티브 | 동작 |
|------------|------|
| `create_thread(name, participants)` | 이름 있는 대화 스레드를 열고 식별자를 반환 |
| `send_message(thread, content, mentions)` | 스레드에 메시지를 붙이고 **즉시 반환** (듣는 사람 유무 무관, fire-and-forget) |

> (v0.3부터 `wait_for_mention`(L2 foreground blocking)은 완전히 제거되었다. inbox push + step() drain이 유일한 수신 경로이며, 이 프로젝트의 목표인 패시브 어웨어니스(L3)만 제공한다.)

### 2.2 패시브 어웨어니스 (핵심 차별점)

- **전경(blocking receive)** = 과거 L2 대조 모드. v0.3에서 `wait_for_mention`과 함께 제거되었다.
- **배경(passive awareness)** = 수신을 전경에서 기다리지 않는다. 서버가 메시지를 inbox에 push하고, `step()`이 다음 경계에서 자동 흡수 → 에이전트는 계속 일한다. (원본 L3 = 이 프로젝트의 목표, §3.5.2)

### 2.3 5단계 협업 프로토콜 (P1~P5) — 장기 목표

원본의 전체 협업 프로토콜. **v0.1 범위가 아니며, 구현은 v0.2에서 다룬다** (§6 로드맵). 여기서는 계승할 최종 형태를 정의한다.

네 에이전트(수는 사용자 구성)가 고정 프로토콜을 돈다. 에이전트-1이 **어셈블러**가 되어 스레드를 개설하고 페이즈 전이를 게이트한다(모든 에이전트의 명시적 승인을 모아야 다음 페이즈로).

1. **P1 탐색** — 각자 [원본: 백그라운드 워처]를 켜고, 독립적으로 대상(질문/컨텍스트)을 탐색, 하위 질문 초안. (아무것도 안 보냄)
   > "백그라운드 워처"는 원본 L3의 표현이며, **이 구현(v0.1)은 A 모델(워처 없음, push+inbox)** 을 쓴다 (§3.5.2). v0.2에서 P1~P5를 얹을 때 A 모델에 맞게 재구현한다.
2. **P2 분할** — 어셈블러가 계획 스레드 생성. 발견 사항 풀링 → 하위 질문 분할 협상 → **전원 승인**까지 수정.
3. **P3 실행** — 각자 자기 몫 수행. 발견 즉시 워크로그에 공유(중간 발견/모순/장애/포기된 접근).
4. **P4 교차검토** — 각자 결과 스레드에 근거와 함께 방송. 검토자들이 사실 충돌·근거 부족·누락 관찰 지적.
5. **P5 제출** — 어셈블러가 승인된 결과로 최종 답을 조립 → 초안 방송 → 최종 승인 → 제출.

### 2.4 접두사 컨벤션 (수신자가 일을 멈추지 않고 분류하도록)

- `FYI:` — 답변 불필요, 참고만.
- `URGENT:` — 수신자 진행 중인 작업에 영향(가정 오류, 요청됐던 것) → 다음 작업 전 처리.
- 접두사 없음 — 일반 메시지, 자연스러운 휴지기에 답.

---

## 3. 아키텍처

### 3.1 전체 구조

```
┌─────────────────────────────────────────────────────┐
│                    사용자 (CLI / YAML)              │
│   에이전트 동적 생성 · 채널 선택 · 세션 시작/중단    │
└──────────────────────┬──────────────────────────────┘
                       │
                ┌──────▼───────┐
                │   Session    │  ← 세션 = 에이전트 묶음 + 메시지 서버
                │ (오케스트레이터)│     생명주기 / (v0.1b~) 게이트
                └──────┬───────┘
        ┌──────────────┼──────────────┐
        │              │              │
 ┌──────▼─────┐ ┌──────▼─────┐ ┌──────▼─────┐
 │ Agent #1   │ │ Agent #2   │ │ Agent #N   │  ← 독립 루프 (step()이 inbox drain)
 │ (모델 A)   │ │ (모델 B)   │ │ (모델 C)   │
 └──────┬─────┘ └──────┬─────┘ └──────┬─────┘
        │   세 프리미티브 (공통 결합)    │
        └───────────────┬───────────────┘
                 ┌──────▼────────┐
                 │ 내부 메시지 서버 │  ← SSOT (진실): 스레드/메시지/멘션/대기
                 └──────┬────────┘
                        │ 읽기 전용 미러 (선택적)
                 ┌──────▼───────┐
                 │  채널 어댑터  │  ← v0.1b 이후 사람용 관측창 (미러)
                 │ (읽기 전용)   │
                 └──────────────┘
```

### 3.2 핵심 추상화 계층

| 계층 | 책임 | 인터페이스 (초안) |
|------|------|-------------------|
| **Agent** | 모델에 시스템 프롬프트 주입, 턴 루프, 도구 호출 | `start()`, `send_system()`, `step()`, 도구 바인딩 |
| **Model Backend** | 특정 모델 API 호출 | `complete(messages, tools)` — OpenAI/Anthropic/기타 어댑터 |
| **Channel (Mirror)** | 내부 서버 상태를 채널에 읽기 전용 표시 | `mirror_thread()`, `publish_readonly()`, `disconnect()` |
| **Protocol** | 분할 합의 등 협업 게이트 (v0.1b~, P1~P5는 v0.2) | `run(session)` |
| **Session** | 에이전트 묶음 + 내부 메시지 서버 + 생명주기 | `start()`, `stop()`, 상태 조회 |
| **Message Server (SSOT)** | 스레드/메시지/멘션의 진실 공급원 | `create_thread()`, `send_message()` |

### 3.3 채널 설계 결정 — "내부 메시지 서버가 진실(SSOT)이다"

**결정:** 메시지의 단일 진실 공급원(SSOT)은 **내부 메시지 서버**다. Discord 등 메시징 채널은 **읽기 전용 미러(선택적 뷰)** 로만 붙는다. 프로토콜은 채널에 의존하지 않는다.

**이유 (리뷰 반영, 2026-08-26):**

- P1~P5·APPROVE·워크로그 같은 프로토콜 상태를 Discord의 기본 구조 위에 컨벤션으로 인코딩하려면(§3.4) 그 자체가 v0.1의 최대 리스크가 된다. SSOT를 채널에 두는 순간 프로토콜 상태가 외부 서비스 제약에 종속된다.
- 내부 서버를 SSOT로 두면 스레드/멘션/상태관리를 제어 가능하게 짤 수 있고, 채널은 "지켜보는 뷰"로 격리되어 채널을 늘릴 때도 프로토콜을 건드리지 않는다.
- 패시브 어웨어니스 검증(L2→L3 통신 모드)은 채널 종류와 무관한 코어 로직이어야 하므로, 코어는 채널에서 분리하는 게 맞다.

**대가(단점, 인지하고 수용):**

- 내부 서버를 하나 더 구현·운용해야 한다 (단, 원본의 106MB coral-server.jar를 재사용하지 않고 요구에 맞게 경량 재구현).
- Discord 미러는 "내부 상태 → 채널 표시" 동기화가 추가 작업이며, 사람이 Discord에서 개입해도 에이전트 코어는 기본적으로 그걸 듣지 않는다 (사람 개입 경로는 별도 설계 — v0.1 범위 밖).

### 3.4 내부 메시지 서버 (SSOT) — 스키마와 프리미티브

원본의 세 프리미티브를 **내부 메시지 서버**가 직접 구현한다. 프로토콜 상태는 전부 이 서버 위에 산다.

| 프리미티브 | 구현 |
|------------|------|
| `create_thread(name, participants)` | 스레드 레코드 생성, 스레드 ID 반환 |
| `send_message(thread, content, mentions)` | 메시지 추가, **즉시 반환** (fire-and-forget) |

#### 3.4.1 최소 스키마

```
agent    { agent_id: str }                       # "agent-1" …
thread   { thread_id: str, name: str, participants: [agent_id] }
message  { message_id: str, thread_id: str, author: agent_id,
           content: str, mentions: [agent_id], created_at: int }
inbox    { agent_id: str, queue: [message_id] }  # 서버가 push, step()이 drain (단일 소비자)
```

- **멘션 표기**: `mentions[]`에 `agent_id` 문자열을 담는다. 표면 문법(`@agent-2`)은 전적으로 프롬프트/도구가 처리하고, SSOT에는 정규화된 `agent_id`만 남긴다.
- **브로드캐스트 vs 멘션 전용**: `mentions`가 비어 있으면 브로드캐스트(스레드 `participants` 대상, author 제외 — §3.5.3), 있으면 명시 대상만 수신.
- 채널(Discord)은 이 서버 상태를 **읽기 전용 미러**로 표시할 뿐, 프로토콜 전이의 판단 근거가 아니다.

#### 3.4.2 스냅샷 폭증 방지 (기본값 고정)

"전체 스레드 스냅샷"을 그대로 반환하면 컨텍스트가 즉시 폭증한다. v0.1 기본값은 **unread-only**로 고정하고, 방어 옵션을 둔다.

- **기본 `unread-only`**: 호출자에게 **읽지 않은 멘션·메시지만** 반환 (호출자별 커서). (v0.3에서 `wait_for_mention`과 커서 기반 수신은 제거되었으며, inbox push + step() drain이 단일 경로다.)
- 옵션 `last_n`: 최근 N개.
- 옵션 `summary`: v0.2 이후 (컨텍스트 압축 시).
- 복구 시점에는 `read_resource` 상당(전체 상태 덤프)을 명시적 도구로만 제공하고, 자동 주입하지 않는다.

### 3.5 에이전트 실행 모델

#### 3.5.1 프리미티브 노출 (v0.1a 고정)

**결정:** 프리미티브는 에이전트에게 **명시적 도구(tool)** 로 노출한다. (A 모델, §3.5.2)

- `create_thread(name, participants)` — **도구**, 에이전트가 호출
- `send_message(thread, content, mentions)` — **도구**, 에이전트가 호출 (fire-and-forget)
- `read_resource()` — **도구**, 명시적 전체 상태 덤프 (복구/집계용, 자동 주입 없음)
- (참고: `wait_for_mention`(L2 foreground blocking)은 v0.3에서 완전히 제거되었다.)

L3에서 수신은 "도구 호출"이 아니라 **inbox에 push → step()이 자동 흡수**다. 에이전트는 언제 듣는지를 제어하지 않는다 — 그게 패시브 어웨어니스의 본질이다.

#### 3.5.2 수신 모델 (v0.1a 고정: A — send→inbox, step()만 drain)

**결정:** v0.1a는 **A 모델**로 고정한다. **L3에는 별도 워처 태스크가 없다.**

- **서버**: `send_message`가 대상의 **inbox에 즉시 push**.
- **step()만 drain**: 각 `step()` 직전에 inbox를 drain해 `[radio]` 블록의 단일 user turn으로 삽입 (§3.6).
- **패시브 = "전경에서 wait를 부르지 않음"**: 에이전트는 수신을 전경에서 기다리지 않는다. 대신 inbox에 쌓인 메시지가 다음 step()에서 자동 흡수된다. (과거 L2의 전경 blocking receive는 v0.3에서 제거.)

> 단일 소비자 원칙: inbox를 소비하는 주체는 **step() 하나**뿐이다. 서버(push)와 step(drain)이 큐를 나눠 쓰므로 레이스가 없다. (B의 워처 태스크, C의 "워처가 받아서 push"는 v0.1에서 쓰지 않는다.)

#### 3.5.3 브로드캐스트 fan-out (v0.1a 기본값)

- `mentions`가 비어 있으면(브로드캐스트) **해당 스레드의 `participants`에게만 fan-out**하고, **author(송신자)는 제외**한다.
- `mentions`가 있으면 명시 대상에게만 push. 단, **participants 밖 대상을 가리키는 멘션은 무시(reject)** — 실제 수신은 `participants ∩ mentions`로 한정한다.
- 모든 push는 FIFO. FYI/URGENT는 프롬프트 정책(모델이 읽고 분류)이며, drain 순서에는 관여하지 않는다.

#### 3.5.4 동시성 모델 (v0.1a 기본값 고정 → 병렬 실행으로 전환 완료)

**결정:** 단일 asyncio 이벤트 루프 내 에이전트 병렬(asyncio.Task), 프로세스/스레드 격리 없음.

- **병렬 실행**: `Session.run()`에서 각 에이전트가 독립 `asyncio.Task`로 돌며, 자기 자신이 finished될 때까지 step을 반복한다. 기존 라운드로빈 `for` 루프를 제거했다.
- **글로벌 스텝 예산**: 모든 에이전트의 step 합을 `max_steps`로 제한한다. asyncio 단일 루프상에서 +=는 atomic하므로 락 없이 동작한다.
- **게이트/프로토콜 상태 주입**: `_inject_protocol_gate_state(agent, protocol)` / `agent.gate_open` 갱신을 step 앞에 그대로 둔다. `MessageServer`가 단일 이벤트 루프+협력 스케줄링이라 메시지/게이트 상태 접근은 원자적이므로 race 없다.
- **도구 로그 실시간 스트리밍**: 병렬화로 도구 호출이 발생 즉시 큐에 밀려 출력된다. `_output_consumer` / `_log_tool_event`(cli.py)는 이미 flush=True이므로 변경 불필요.
- **서버 상태**: v0.1a는 **메모리**(dict + asyncio 큐). 영속화가 필요해지면 aiosqlite로 전환(단일 writer 태스크가 lock).
- **inbox**: 에이전트별 `asyncio.Queue`.
- **프로세스/스레드 격리는 v0.1에서 쓰지 않는다.** 에이전트 = 동일 루프 내 코루틴. (모델 호출은 어댑터가 비동기 HTTP로 띄움.)

이 결정은 v0.1a에서 동시성·잠금 논쟁을 미리 제거하기 위한 것이며, 에이전트 규모가 커지면 프로세스 격리로 재검토한다. 병렬 실행 전환으로 백엔드 LLM 응답을 기다리는 동안 다른 에이전트가 블로킹되지 않으며, 도구 호출 로그가 발생 순서대로 실시간 흘러나온다.

### 3.6 강제 삽입 포맷 (v0.1a 고정)

inbox drain 결과를 모델에 어떻게 넣는지 고정한다. (모델별 반응 차이를 줄이기 위해 단순화)

- drain한 메시지들을 **한 턴으로 합쳐 하나의 user turn**으로 삽입.
- `[radio]` 블록으로 감싸고, 각 메시지에 `from=<agent_id>`를 붙인다.

```
[radio]
from agent-2: (URGENT) 정답은 42가 아니라 43.
from agent-3: (FYI) 내 몫은 DB 쪽이야.
```

- system 턴(시스템 프롬프트 재주입)은 쓰지 않는다 — 오직 단일 user turn.
- `[radio]` 접두사의 존재가 "이건 동료의 라디오 메시지"임을 모델에게 알린다.

---

## 4. 기술 스택 및 프로젝트 구조 (초안)

### 4.1 스택

- **언어/런타임:** Python 3.11+ (asyncio 단일 루프, §3.5.4)
- **패키징:** `pyproject.toml` (uv 또는 pip)
- **설정:** YAML + CLI (둘 다 지원)
- **모델 SDK:** 백엔드별 어댑터. **1순위 OpenAI-compatible, 2순위 Nous Portal.** 최소 의존(표준 라이브러리 + `httpx`/`aiohttp` 정도).
- **서버 상태:** v0.1a 메모리(dict + asyncio 큐), 필요 시 aiosqlite.

### 4.2 디렉터리 (초안)

```
agent-augury/
  pyproject.toml
  README.md
  DESIGN.md
  LICENSE                  # Apache-2.0 (예정)
  src/
    agent_augury/
      cli.py               # CLI 진입점 (로그 미러)
      config.py            # YAML 로드/검증
      session.py           # 세션 = 에이전트 묶음 + 메시지 서버 + 생명주기
      server.py            # 내부 메시지 서버 (SSOT, in-process asyncio)
      agent/
        loop.py            # 에이전트 step() 루프 + inbox drain (단일 소비자)
        tools.py           # create_thread/send_message/read_resource 도구
        system_prompt.py   # 모델 무관 통신 규칙 프롬프트 템플릿
        # watcher.py 없음(A 모델) — 필요 시 v0.1+에서 B 모델 도입 시 추가
      backend/
        base.py            # ModelBackend 인터페이스
        openai_compat.py   # OpenAI 호환 어댑터 (1순위)
        nous_portal.py     # Nous Portal 어댑터 (2순위)
      channel/             # (v0.1b 이후) 사람용 관측창 / 미러
        base.py
      protocol/            # (v0.1b~) 분할 합의 등 최소 프로토콜
        phases.py
        approval.py
  examples/
    demo.yaml             # 예시 세션 구성
  tests/
```

### 4.3 라이선스

**결정(예정): Apache-2.0.**

원본이 Apache-2.0이고, 프로토콜/프롬프트 문장을 참고할 가능성이 있으면 Apache-2.0 + 논문·레포 인용이 마찰이 적다. 코드를 전혀 미복제하고 완전 재작성할 경우 MIT도 가능하지만, "원본 개념 계승"이라는 정체성상 Apache-2.0으로 두고 논문([arXiv:2607.28430](https://arxiv.org/abs/2607.28430))과 원본 레포를 명시 인용한다.

---

## 5. 열린 결정 (Open Decisions)

구현 착수 전에 확정할 항목. 사용자가 결정하거나, 구현 중 검증 후 못박는다.

| # | 항목 | 기본값(상태) |
|---|------|-------------|
| D1 | 프로젝트 이름 | `agent-augury` — **확정 (2026-08-26)** |
| D2 | 라이선스 | Apache-2.0 (예정) — §4.3 |
| D3 | Discord 위치 | v0.1a는 CLI 로그 미러만, Discord는 v0.1b 이후 — **확정** |
| D4 | Nous Portal API 스펙 | 2순위 어댑터라 v0.1a 블로커 아님 — 확인 시점 유동 |
| D5 | 서버 상태 저장 | v0.1a 메모리, 필요 시 aiosqlite — **확정** |
| D6 | 배경 워처 | **해당 없음** (A 모델: 워처 없음, §3.5.2) |

> 리뷰 반영으로 D1(이름)·D3·D5·D6이 v0.1a 기준으로 확정. D4(Nous Portal API 스펙)는 1순위 OpenAI-compatible 어댑터로 시작하므로 v0.1a 블로커가 아니다.

---

## 6. 구현 로드맵 (첫 릴리스 범위)

로드맵은 **"검증 대상을 최소화"** 하는 원칙으로 재편했다 (리뷰 반영, 2026-08-26).

**핵심 판단:** 이 프로젝트의 차별점은 P1~P5 프로토콜이 아니라 **패시브 어웨어니스(L2→L3 통신 모드)** 다. 그러므로 v0.1에서는 통신 메커니즘을 먼저 검증하고, 통과 후에야 최소 프로토콜을 얹는다. P1~P5 전체는 v0.2로 미룬다.

### v0.1a — 통신 메커니즘 검증

**목표:** 패시브 어웨어니스가 실제로 되는지, 그것만 단독 검증한다.

- 에이전트 **A/B 2명**(모델 무관) + 내부 메시지 서버(SSOT, in-process asyncio)
- `send_message` = fire-and-forget(도구), 수신은 **inbox push → step() 자동 흡수** (§3.5.2, A 모델)
- 에이전트가 일하는 동안 동료 멘션을 받아 **다음 step()에 자동 삽입**되는 것 확인 (§3.5.2)
- **P·APPROVE·워크로그 없음.** 프로토콜은 관여하지 않는다.
- **관측은 CLI 로그 미러.** Discord는 v0.1b 이후 사람용 관측창으로 미룬다.

**통과 기준(자동/스크립트 검증, 주관 판단 배제):** L2 vs L3 대조 토이 시나리오를 **Fake ModelBackend로 tool 시퀀스를 고정**해 숫자로 단언한다. 에이전트는 **A/B 2명**으로 충분하다.

```
시나리오: A가 숫자 탐색(search tool 반복) 중, B가 "정답은 42가 아니라 43"이라는
정정 멘션을 보낸다.

- Fake ModelBackend로 A의 tool 시퀀스를 고정(정정 전 search를 계속 호출하도록).
- L3(push+inbox): A가 search tool을 정정 전 N회 이상 호출 AND 최종 답이 43.
  (즉, 탐색을 멈추지 않았고 정정을 흡수함)
- L2(전경 wait): inbox push가 꺼져 있고, wait_for_mention이 전경 tool이라서
  search가 N 미만이거나, 정정 흡수까지 wall-step이 더 큼.

단언(스크립트):
  assert L3.search_count >= N
  assert L3.final_answer == 43
  assert L3.search_count > L2.search_count   (또는 L2는 정정 흡수 시점이 더 늦음)
```

> 논문의 SWE-Atlas(124태스크) 수준은 필요 없다. "L2→L3 통신 모드가 결과를 바꾼다"는 최소 재현이 이 프로젝트의 설득력이며, 그 판단은 "로그 감상"이 아니라 **search 횟수·최종 답·wall-step**이라는 숫자로만 이뤄진다.

### v0.1b — 최소 프로토콜 (분할 합의 1단) — 구현 완료

**목표:** v0.1a 위에 "게이트가 있는 협업"이 최소로 도는 것을 확인.

- 각자 초안 제시 → 한 스레드에서 분할안 논의 → **전원 APPROVE** → 각자 몫 수행(자유 실행, P4/P5 없음)
- 내부 서버의 스레드/멘션 위에서 게이트가 도는지 확인
- **Discord를 "사람용 관측창"으로 도입** (미러, 코어는 Discord 입력 안 들음)
- **Phase transition hook 도입** — `PhaseManager`로 게이트 OPEN을 명시적 훅으로 노출 (v0.2 P1~P5 확장 대비)
- **실제 OpenAI-compatible LLM 연동 예시** — `examples/consensus_openai.yaml` (PROPOSE/APPROVE 자율 생성)

**통과 기준:** 분할 합의 1단(제안→합의→승인→실행)이 끝까지 돌며 전원 승인 게이트가 동작. — `examples/consensus_demo.py` 검증 완료.

### v0.2 — P1~P5 전체 (구현 완료)

교차검토(P4), 어셈블러 제출(P5) 등 원본의 전체 프로토콜 구현 완료.

- 5단계 프로토콜 상태 머신: `CollaborationProtocol` + `PhaseManager`
- 4개 게이트: P2_SPLIT / P3_EXECUTE / P4_REVIEW / P5_SUBMIT
- 각 게이트는 전원 승인(APPROVE) 시 자동 개방, 다음 페이즈로 자동 진행
- P1~P5 E2E 검증: `examples/p1_to_p5_demo.py` — 3개 에이전트, Fake 백엔드
- 시스템 프롬프트 페이즈 주입: 각 에이전트의 step()마다 현재 페이즈 반영
- 접두사 컨벤션: `PROPOSE:` / `APPROVE:` / `REJECT:` / `RESULT:` / `FINAL:`

---

### 마일스톤 정리 (가설 검증 순서로 재배치)

핵심 가설(패시브 어웨어니스)에 가깝게 순서를 잡았다. 백엔드·채널 추상화를 M1에 몰아넣지 않는다.

| 단계 | 내용 | 통과 기준 |
|------|------|-----------|
| M1 | 스캐폴드 + **내부 메시지 서버 primitives + 단위 테스트** (thread/send/wait/inbox) | 서버 primitives 단위 테스트 통과 |
| M2 | **에이전트 루프**(step/inbox drain, 단일 소비자) | 루프 동작 (Fake ModelBackend 스텁으로) |
| M3 | **모델 어댑터** (1순위 OpenAI-compatible, 2순위 Nous Portal) | 실제 모델 호출 1회 성공 |
| M4 | **E2E 데모 + L2 vs L3 대조 검증** (v0.1a) | 위 "v0.1a 통과 기준" 스크립트 통과 |
| M5 | **(v0.1b)** 분할 합의 1단 + 전원 APPROVE + Discord 관측창 | 위 "v0.1b 통과 기준" — **완료** |
| M6 | **(v0.2)** P1~P5 전체 프로토콜 + 4개 게이트 + E2E 검증 | 위 "v0.2 구현 완료" — **완료** |

---

## 7. 참고

- 원본 저장소: <https://github.com/Coral-Protocol/AgentRadio> (Apache-2.0)
- 논문: <https://arxiv.org/abs/2607.28430>
- 상품: <https://coralcode.dev/> (참고용 — 이 프로젝트는 이와 무관한 독립 오픈소스)
