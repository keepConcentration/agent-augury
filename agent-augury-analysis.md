# agent-augury 프로젝트 분석 문서

> 분석일: 2026-03-21
> 버전: v0.6.7
> 저장소: `C:/Users/test/IdeaProjects/agent-augury`

---

## 1. 프로젝트 개요

**agent-augury**는 **로컬 멀티 에이전트 협업 런타임**이다. 여러 LLM 에이전트를 한 팀으로 구성하고, 공유 스레드 기반 비동기 메시징을 통해 협업을 수행하는 Python 패키지다.

| 항목 | 내용 |
|------|------|
| 이름 | `agent-augury` |
| 버전 | 0.6.7 |
| 라이선스 | Apache-2.0 |
| 언어 | Python 3.11+ |
| 패키징 | `pyproject.toml` (hatchling 빌드 시스템) |
| 진입점 | `agent_augury.cli:main` |

---

## 2. 핵심 설계 원칙

DESIGN.md에 명시된 6가지 원칙:

1. **Keep working while listening** — 동료 메시지가 blocking wait를 강제하지 않는다.
2. **No single agent is SSOT** — 메시지 서버가 공유 상태를 소유한다.
3. **Surfaces are views** — Discord/Slack/Ink는 관측·상호작용 표면일 뿐, 프로토콜 상태를 소유하지 않는다.
4. **Models are swappable** — 통신 규칙은 런타임에 있으며 특정 벤더 SDK에 종속되지 않는다.
5. **Humans participate** — 사용자는 세션 중 `ask_user`, `@mention` 등으로 참여한다.
6. **Stay observable** — 도구 호출, 스텝, 메시지가 Wire 버스를 통해 스트리밍된다.

---

## 3. 아키텍처

### 3.1 전체 구조

```
사용자 (CLI / YAML)
       │
  Session + Gateway
       │
  ┌────┼────┐
  ▼    ▼    ▼
Agent A  Agent B  Agent N
  │    │    │
  └────┼────┘
       ▼
 Message Server (SSOT)
       │
  Channel Adapters (읽기 전용 미러)
```

### 3.2 핵심 추상화 계층

| 계층 | 책임 | 주요 파일 |
|------|------|-----------|
| **Agent** | 시스템 프롬프트 주입, 턴 루프, 도구 호출 | `agent/loop.py` |
| **Model Backend** | 특정 모델 API 호출 어댑터 | `backend/base.py`, `openai_compat.py`, `nous_portal.py` |
| **Message Server (SSOT)** | 스레드/메시지/멘션의 단일 진실 공급원 | `server.py` |
| **Session** | 에이전트 묶음 + 메시지 서버 + 생명주기 | `session.py` |
| **Protocol** | P1~P5 협업 게이트 상태 머신 | `protocol/collaboration.py`, `approval.py` |
| **Gateway / Bridge** | Wire 버스 이벤트 발행, 표면 연결 | `gateway/` |
| **Channel** | Discord/Slack 읽기 전용 미러 | `channel/` |

---

## 4. 통신 프리미티브

에이전트 간 통신은 3가지 프리미티브로 이뤄진다:

| 프리미티브 | 동작 |
|------------|------|
| `create_thread(name, participants)` | 이름 있는 대화 스레드 생성 |
| `send_message(thread, content, mentions)` | fire-and-forget 메시지 전송 |
| `read_resource()` | 전체 상태 덤프 (복구/집계용) |

### 4.1 Non-blocking inbox 모델 (A 모델)

- `send_message` → 대상의 **inbox에 즉시 push**
- 각 `step()` 직전에 inbox를 drain → `[radio]` 블록으로 단일 user turn에 삽입
- 에이전트는 수신을 전경에서 기다리지 않는다 (**패시브 어웨어니스**)
- inbox의 유일한 소비자는 `step()` (단일 소비자 원칙)

### 4.2 브로드캐스트 규칙

- `mentions`가 비어 있으면 → 스레드 `participants` (author 제외)에게 fan-out
- `mentions`가 있으면 → `participants ∩ mentions` 에게만 push
- participants 밖을 가리키는 멘션은 reject

---

## 5. 에이전트 도구 (ToolBox)

| 도구 | 설명 |
|------|------|
| `create_thread` | 스레드 생성 |
| `send_message` | 메시지 전송 |
| `read_resource` | 전체 상태 덤프 |
| `ask_user` | 사용자에게 질문 (fire-and-forget) |
| `read_file` | 파일 읽기 |
| `list_directory` | 디렉터리 목록 |
| `write_file` | 파일 쓰기 |
| `run_command` | 셸 명령 실행 (shlex 파싱, 셸 인젝션 방지) |
| `fetch_url` | URL 조회 (SSRF 방어) |
| `edit_file` | 파일 부분 수정 (고정 문자열 1회 치환) |
| `append_file` | 파일 끝에 추가 |
| `web_search` | 웹 검색 (LocalTool 트랙 B로 주입) |

### 5.1 보안 메커니즘

- **allowed_roots**: 파일 도구의 경로 제한 (`Path.resolve() + relative_to()`)
- **SSRF 방어**: `web.py`의 `is_blocked_host` (정수/hex IP 우회 포함)
- **셸 블랙리스트**: `rm -rf`, `mkfs`, `sudo`, `reboot` 등 차단
- **도구 승인**: `tools.approval` 설정으로 shell/file_write 승인 요구 (fail-closed)

---

## 6. 협업 프로토콜 (P1~P5)

선택적 모드. `protocol:` 설정 시 활성화된다.

| 단계 | 이름 | 내용 |
|------|------|------|
| P1 | EXPLORE | 각자 독립 탐색. `READY:` 신호로 완료 |
| P2 | SPLIT | 발견 공유 → 하위 질문 분할 협상 → 전원 `APPROVE` |
| P3 | EXECUTE | 각자 할당 작업 수행. 워크로그 공유 |
| P4 | REVIEW | 결과 방송 + 교차 검토 |
| P5 | SUBMIT | 최종 답 조립 → 방송 → 최종 승인 |

### 6.1 게이트 메커니즘

- 각 페이즈 전환 지점에 `ConsensusGate` 배치
- `PROPOSE:` → `APPROVE:` / `REJECT:` 컨벤션
- 전원 승인 시 게이트 개방, 다음 페이즈로 자동 진행
- P1은 `READY:` 기반 완료 정책 (전원 READY 시 자동 진행)

### 6.2 접두사 컨벤션

- `FYI:` — 참고용, 답변 불필요
- `URGENT:` — 수신자의 현재 작업에 영향, 즉시 처리
- `PROPOSE:` / `APPROVE:` / `REJECT:` / `RESULT:` / `FINAL:`

---

## 7. 모델 백엔드

`ModelBackend` 추상 클래스를 통해 모델 무관 설계:

| 백엔드 | 파일 | 인증 방식 |
|--------|------|-----------|
| OpenAI-compatible | `backend/openai_compat.py` | API Key |
| Nous Portal | `backend/nous_portal.py` | API Key |
| Nous Portal OAuth | `backend/nous_portal_oauth.py` | OAuth Device Code |
| Fake (테스트용) | `backend/fake.py` | 없음 |

- `Completion` 데이터클래스: `text`, `tool_calls`, `usage` 필드
- `OAuthModelBackend`: OAuth 토큰 관리 추상 클래스

---

## 8. 설정 시스템

### 8.1 YAML 구조

```yaml
agents:
  - id: agent-1
    role: orchestrator
    backend:
      type: openai
      base_url: https://api.openai.com/v1
      api_key_env: OPENAI_API_KEY

roles:
  orchestrator:
    prompt: "You are the orchestrator..."

tools:
  approval:
    shell: require
    file_write: require
    web: off

protocol:
  participants: [agent-1, agent-2]
  gates:
    P2_SPLIT: plan-thread
    P3_EXECUTE: work-thread

surfaces:
  discord:
    mode: observe
    mirror:
      url_env: DISCORD_WEBHOOK_URL
```

### 8.2 환경 변수 확장

- `${VAR_NAME}` 구문으로 환경 변수 참조
- `.env` 자동 로드 (`~/.agent-augury/.env` 우선)

---

## 9. 게이트웨이와 표면

### 9.1 Gateway 아키텍처

- `SessionGateway`: Wire 버스 이벤트 발행
- `SessionBridge`: Core 이벤트를 Wire로 변환
- 표면(Ink, Discord, Slack)은 Gateway를 통해 연결

### 9.2 표면 종류

| 표면 | 역할 | 파일 |
|------|------|------|
| Ink | 대화형 TUI (Node.js 기반) | `fronts/ink/` |
| Discord | 관측/상호작용 | `channel/discord_*.py` |
| Slack | 관측 (Incoming Webhook) | `channel/slack_*.py` |

---

## 10. 동시성 모델

- **단일 asyncio 이벤트 루프** 내 에이전트 병렬 실행
- 각 에이전트는 독립 `asyncio.Task`
- 글로벌 스텝 예산 (`max_steps`)으로 전체 제한
- 게이트 대기 중 유휴 에이전트는 **park** (빈 루프 방지)
- 프로세스/스레드 격리 없음 (v0.1 기본값)

---

## 11. 영속성

- 런타임 기본값: **메모리** (dict + asyncio.Queue)
- 선택적 영속화: `MessageServer(db_path=...)` → **aiosqlite**
- 스레드/메시지 테이블에 자동 미러링
- 재시작 시 DB에서 상태 복원 (카운터 포함)

---

## 12. 프로젝트 디렉터리 구조

```
agent-augury/
├── pyproject.toml          # 패키지 메타데이터, 의존성
├── README.md               # 사용자 문서
├── DESIGN.md               # 설계 SSOT
├── LICENSE                 # Apache-2.0
├── NOTICE                  # 학술 인용
├── src/agent_augury/
│   ├── cli.py              # CLI 진입점
│   ├── config.py           # YAML 로드/검증
│   ├── session.py          # 세션 관리
│   ├── server.py           # 메시지 서버 (SSOT)
│   ├── wizard.py           # 대화형 설정 위저드
│   ├── agent/
│   │   ├── loop.py         # 에이전트 step() 루프
│   │   ├── tools.py        # ToolBox
│   │   ├── system_prompt.py # 시스템 프롬프트 템플릿
│   │   ├── approval.py     # 도구 승인 로직
│   │   ├── policy.py       # ToolPolicy
│   │   └── web.py          # 웹 도구 (SSRF 방어)
│   ├── backend/
│   │   ├── base.py         # ModelBackend 추상 클래스
│   │   ├── openai_compat.py
│   │   ├── nous_portal.py
│   │   └── nous_portal_oauth.py
│   ├── protocol/
│   │   ├── collaboration.py # P1~P5 상태 머신
│   │   ├── approval.py      # ConsensusGate
│   │   ├── phases.py        # Phase 상수/PhaseManager
│   │   └── signals.py       # READY 메시지 감지
│   ├── gateway/            # Wire 버스, 브릿지
│   ├── channel/            # Discord/Slack 어댑터
│   └── auth/               # OAuth 토큰 저장
├── tests/                  # pytest 테스트 스위트
├── examples/               # 예시 구성/데모
├── docs/                   # 아키텍처 문서
└── fronts/ink/             # Ink TUI 프론트엔드
```

---

## 13. 의존성

| 패키지 | 용도 |
|--------|------|
| `httpx>=0.27` | 비동기 HTTP 클라이언트 |
| `PyYAML>=6.0` | YAML 파싱 |
| `aiosqlite>=0.20` | 비동기 SQLite (영속성) |
| `py-cord>=2.6` | Discord 봇 |
| `python-dotenv>=1.0` | .env 로드 |
| `rich>=13.0` | 터미널 출력 포맷팅 |

개발 의존: `pytest`, `pytest-asyncio`, `ruff`

---

## 14. 구현 로드맵 (DESIGN.md §6)

| 단계 | 내용 | 상태 |
|------|------|------|
| v0.1a | 통신 메커니즘 검증 (L2 vs L3) | ✅ 완료 |
| v0.1b | 최소 프로토콜 (분할 합의 1단) | ✅ 완료 |
| v0.2 | P1~P5 전체 프로토콜 | ✅ 완료 |
| v0.3 | Non-blocking inbox 확립, wait_for_mention 제거 | ✅ 완료 |
| v0.7 | 에이전트 도구 확장 (run_command, fetch_url 등) | ✅ 완료 |
| v1.0 | Human-in-the-loop 내장, Ink Surface | ✅ 완료 |

---

## 15. 테스트

- **프레임워크**: `pytest` + `pytest-asyncio`
- **테스트 위치**: `tests/`
- **주요 테스트 파일**:
  - `test_server.py` — 메시지 서버 프리미티브
  - `test_agent_loop.py` — 에이전트 step/inbox drain
  - `test_broadcast.py` — 브로드캐스트 fan-out
  - `test_protocol.py` — P1~P5 프로토콜
  - `test_tool_approval.py` — 도구 승인
  - `test_persistence.py` — aiosqlite 영속성
  - `test_wizard.py` — 설정 위저드
  - `test_integration_openai.py` — OpenAI 통합 (opt-in)

---

## 16. 결론

agent-augury는 **로컬 멀티 에이전트 협업**에 특화된 런타임이다. 가장 큰 차별점은 **non-blocking inbox 모델**을 통해 에이전트가 멈추지 않고 동료의 메시지를 자연스럽게 흡수하는 것이다. 이 위에 P1~P5 프로토콜, HITL, 다양한 모델 백엔드, Discord/Slack 표면이 얹혀진 구조다.

모델 무관 설계, fail-closed 도구 승인, SSRF 방어 등 보안 메커니즘도 잘 갖춰져 있으며, 단일 asyncio 루프 기반 병렬 실행으로 효율적인 리소스 사용을 달성했다.