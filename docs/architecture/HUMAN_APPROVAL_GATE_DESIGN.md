# Human approval gate — YAML 협업 흐름 차단 설계

> **Status:** **H1–H5 landed** (config · ConsensusGate after_agents · Session · prompt · Wire/D3); H6 docs below  
> **Date:** 2026-09-15  
> **Priority:** P1 (Ink/Discord 우선 제품; Slack/Web/M8과 독립)  
> **Parent:** `docs/USER_INTERVENTION_DESIGN.md` §4.4 / §5 (`human_approval`)  
> **구분:** `docs/architecture/TOOL_HUMAN_APPROVAL_DESIGN.md` = **도구 부작용 집행** 게이트  
> **본 문서:** **프로토콜 페이즈 합의**에 사람 서명을 넣는 게이트  
> **Code:** `protocol/human_approval.py`, `protocol/approval.py`, `config.py`, `session.py`, `agent/system_prompt.py`, `gateway/bridge.py`, `gateway/types.py`  
> **Tests:** `tests/test_human_approval_gate.py`

---

## 0. 한 줄

선택한 프로토콜 페이즈(예: `P5_SUBMIT`)에서 **에이전트끼리 먼저 만장일치**한 뒤에야  
사람에게 승인이 넘어가고, 사람이 `APPROVE:` 하기 전까지 **페이즈 전환이 막힌다**.  
설정은 `protocol.human_approval` 한 블록으로 켠다 (모드 기본 = `after_agents`).  
**페이즈 기본값은 P2~P5 전부 `false`** (키 생략 = false).

---

## 1. 배경 — 두 가지 “사람 승인”이 다르다

| | 도구 승인 (landed) | 본 설계 `human_approval` |
|--|-------------------|---------------------------|
| 막는 것 | `run_command` / 파일쓰기 등 **부작용 실행** | **페이즈 게이트 개방** → 다음 프로토콜 단계 |
| 경로 | `ToolPolicy` → `pending_approval` → Wire `approval.*` | `ConsensusGate` 2단 (에이전트 → human) |
| 표현 | UI Approve/Deny 버튼 (Ink/Discord) | 에이전트 `APPROVE:` 후 → 사람 `APPROVE:` / `REJECT:` |
| YAML | `tools.approval` | `protocol.human_approval` |

둘은 보완 관계다. 위험한 셸을 막는 것과, “제출 전에 사람 사인오프”는 같은 버튼이 아니다.

### 1.1 왜 지금 설계가 필요한가

`USER_INTERVENTION_DESIGN.md` §4.4에 YAML 스케치만 있고:

- `config.py`에 `human_approval` 검증·배선 없음  
- 초안 `co_sign`(에이전트+human 동시 투표)은 **“에이전트 합의 후 사람에게 요청”** UX와 맞지 않음  
- `ConsensusGate`는 지금 **한 단** 만장일치만 지원 → human 대기 상태(`human_pending`)가 필요  

본 문서가 YAML·2단 흐름·런타임 계약을 채운다.

### 1.2 현재 게이트 동작 (제약)

- P2: `require_proposal=True`, bind `PROPOSE:` → 전원 `APPROVE:`  
- P3+: `require_proposal=False` → 첫 메시지로 bind, 전원 `APPROVE:`  
- 게이트 스레드 참가자 = `CollaborationProtocol.participants` (보통 에이전트 id만)  
- P1 `READY:` 도 **participants 전원** 기준 → 여기에 `human`을 넣으면 사람이 READY를 보내야 P2 진입 (비목표)

---

## 2. 목표 · 비목표

### 2.1 목표

1. YAML로 **페이즈별** human 서명 필수 여부를 켠다.  
2. 켠 페이즈에서는 **① 에이전트 만장일치 → ② 사람에게 요청 → ③ human APPROVE** 순서.  
3. ②③ 구간에도 게이트는 닫힌 채 **gate-wait park** — blocking `await` 없음.  
4. Ink / Discord inbound의 `human_send`로 ③을 처리.  
5. 설정 없으면 **오늘날과 100% 동일** (opt-in).

### 2.2 비목표

| 제외 | 이유 |
|------|------|
| 도구 승인 UI/토큰과 통합 | 경로·의미가 다름; 혼동만 증가 |
| Slack / Web / Desktop | 후순위; Core SSOT + Ink/Discord로 검증 |
| 다중 human principal (A9) | v1은 예약어 `"human"` 하나 |
| P1에 human READY 강제 | 탐색 페이즈를 사람이 막지 않음 |
| 에이전트+human **동시** 만장일치 (`co_sign`)를 v1 기본으로 | 사용자 선호는 순차(`after_agents`) |
| Hermes식 smart LLM 분류 | 비결정·비용 |

### 2.3 성공 기준

- `human_approval.P5_SUBMIT: true`에서 에이전트만 전원 `APPROVE:` → **아직** 페이즈 미진행 (`human_pending`).  
- 그 다음 사람 `APPROVE:` → 게이트 open → 다음 페이즈.  
- 사람 `REJECT:` → 에이전트 표 리셋 + `human_pending` 해제 → 에이전트 재합의.  
- 키 없음 / `false` → 기존 동작.  
- interact 경로 없으면 **기동 실패** (D4).

---

## 3. YAML 계약 (동결 후보)

### 3.1 권장 형태 — 페이즈 → bool 맵

```yaml
protocol:
  participants: [agent-1, agent-2, agent-3]   # 에이전트만
  assembler_id: agent-1                        # 선택
  gates:
    P2_SPLIT: plan
    P3_EXECUTE: execution
    P4_REVIEW: review
    P5_SUBMIT: submission
  human_approval:
    P5_SUBMIT: true            # 제출: 에이전트 합의 후 사람 승인
    # P4_REVIEW: true
```

- **키:** `P2_SPLIT` | `P3_EXECUTE` | `P4_REVIEW` | `P5_SUBMIT`  
- **값:** `true` | `false`  
- **기본값:** 네 페이즈 모두 **`false`** (섹션 없음 · 키 생략 · 명시적 `false` 동등)  
- **P1_EXPLORE:** 키로 넣으면 **ConfigError**

`true`로 켠 페이즈만 **독립적으로** ①에이전트 합의 → ②사람 승인이 한 번씩 돈다.

### 3.2 동등한 긴 형태

```yaml
protocol:
  human_approval:
    phases:
      P5_SUBMIT:
        required: true
        mode: after_agents     # v1 기본·유일 구현 모드
```

짧은 맵 `P5_SUBMIT: true` → `{ required: true, mode: after_agents }` 로 정규화.

### 3.3 검증 규칙 (`config.py`)

| 규칙 | 동작 |
|------|------|
| `human_approval` 없음 | OK, no-op |
| 키가 `gates`에 없는 페이즈 | `ConfigError` |
| `P1_EXPLORE` | `ConfigError` |
| `protocol` 없이 `human_approval`만 | `ConfigError` |
| `participants`에 `"human"` | **ConfigError** (전역 READY 오염). human은 투표 집합에 넣지 않음 |
| `mode` ≠ `after_agents` (v1) | `ConfigError` (`co_sign` 등은 예약) |

### 3.4 기본값

| 항목 | 기본 |
|------|------|
| `P2_SPLIT` / `P3_EXECUTE` / `P4_REVIEW` / `P5_SUBMIT` | 각 **`false`** |
| `human_approval` 섹션 자체 | 없어도 됨 (= 전부 false) |
| `mode` (긴 형태) | `after_agents` |

- wizard `DEFAULT_PROTOCOL`: **`human_approval` 없음** (전체 false와 동일)  
- 정규화 후에는 내부적으로 네 페이즈 bool 맵을 갖되, 생략 키는 `false`로 채운다.

---

## 4. 투표 모드 — v1 = `after_agents` (동결)

### 4.1 흐름 (한 페이즈 기준)

```text
[에이전트 단]  기존과 동일: PROPOSE:/작업 로그 + 에이전트들 APPROVE:
        │
        ▼  에이전트 만장일치 직전(열리기 직전)
[가로채기]  opened_at_seq 아직 null · human_pending = true
        │     Wire/로그: “에이전트 합의됨 — 사람 승인 대기”
        ▼
[사람 단]   human_send APPROVE:  → 게이트 실제 open → 페이즈 진행
            human_send REJECT: → 에이전트 approvals clear · human_pending=false
                                 → 에이전트 단부터 다시
```

**사람에게 “요청”하는 시점** = 에이전트 만장일치가 성립한 직후 (그 전엔 사람에게 승인 UI/요청을 띄우지 않음).

### 4.2 스레드 · 참가자

- 게이트 스레드 **participants = 에이전트만** (human 추가 **안 함**).  
- 사람 메시지는 기존처럼 `human_send`로 스레드에 들어가고, 게이트가 **`human_pending`일 때만** human의 `APPROVE:`/`REJECT:`를 2단으로 해석한다.  
- 에이전트 단에서는 human의 `APPROVE:`를 **무시** (실수 입력 방지).

### 4.3 에이전트 단 bind 규칙

human_approval이 켜진 페이즈도 **에이전트 단 prefix는 기존과 동일**:

- P2: `PROPOSE:` + 에이전트 `APPROVE:`  
- P3+: 기존 `require_proposal=False` 규칙  

`REQUEST_APPROVAL:` 은 **에이전트 필수 prefix가 아님** (합의 후에야 사람에게 넘어가므로).  
선택: assembler가 요약용으로 `REQUEST_APPROVAL: …`를 보내도 되고, Core가 human_pending 진입 시 **합성 로그/Wire**로 요약해도 됨 (아래 D5).

### 4.4 Core 가로채기 (ConsensusGate / Protocol)

기존 `on_message`에서 “만장일치 → 즉시 `opened_at_seq` + `on_open`” 하던 지점을 분기:

```text
if agent_unanimity and phase has human_approval:
    if not human_pending:
        human_pending = true
        notify surfaces (D5)
        # do NOT open
    # wait for human message
elif human_pending and author == "human":
    if APPROVE: → open (set opened_at_seq, on_open)
    if REJECT:  → clear agent approvals; human_pending = false
else:
    # 기존 게이트 로직
```

구현 위치 후보: `ConsensusGate`에 `human_approval_mode` / `human_pending` 필드, 또는 Protocol 래퍼.  
**권장:** `ConsensusGate`에 opt-in 플래그 (`await_human_after_agents: bool`) — Session이 human_approval 페이즈에만 true.

### 4.5 예약 모드 (v1 미구현)

| mode | 의도 |
|------|------|
| `co_sign` | 스레드에 human 넣고 에이전트+human **동시** 만장일치 (이전 초안) |
| `human_only` | 에이전트 합의 없이 human 한 표만 |

스키마에 이름만 예약; v1 파서는 `after_agents`만 허용.

### 4.6 prefix · 표 (동결)

| 단계 | 문자열 | 누가 |
|------|--------|------|
| 에이전트 proposal (P2) | `PROPOSE:` | 에이전트 |
| 에이전트 찬성 | `APPROVE:` | 에이전트 |
| 에이전트/공유 반대 (단1) | `REJECT:` | 에이전트 → 표 리셋 (기존) |
| 사람 찬성 (단2) | `APPROVE:` | human only, `human_pending`일 때 |
| 사람 반대 (단2) | `REJECT:` | human → 단1부터 재시작 |

---

## 5. 런타임 배선

### 5.1 설정 → Session

```text
load_config
  → normalize protocol.human_approval → dict[Phase, {required, mode: after_agents}]
Session
  → bind_gate(...)  # 에이전트 규칙 유지
  → gate.await_human_after_agents = True  # 해당 페이즈만
_setup
  → create_thread(..., participants=agents only)  # human 미포함
```

`CollaborationProtocol.participants`에도 human 없음.

### 5.2 시스템 프롬프트

```text
Human phase approval (protocol), phases: P5_SUBMIT:
- Reach agent consensus as usual (PROPOSE:/APPROVE: among agents).
- After all agents APPROVE, the gate waits for the human — do not assume
  the phase advanced until you see the phase change.
- If the human REJECT:s, re-form agent consensus, then wait again.
- Separate from tool-approval buttons for shell/file tools.
```

### 5.3 Surface UX (Ink / Discord)

| 시점 | 표시 |
|------|------|
| 에이전트 합의 중 | 기존 스레드 로그 |
| `human_pending` 진입 (D5) | “에이전트 합의됨 — `APPROVE:` / `REJECT:` 로 응답” |
| human APPROVE/REJECT | `human_send` → **현재 게이트 스레드** (D3) |

v1에서 도구 승인 Wire(`approval.*`)는 재사용하지 않음.  
**D5 (동결):** `human_pending` 진입 시 Wire `session.human_approval_pending` (phase, thread_id, optional summary) 1회 발행 + observe 포맷 강조.

**D3 (동결):** inbound default `thread_id` = `protocol.current_gate.thread_id`.

### 5.4 interact 부재 (D4)

`human_approval`이 하나라도 true 인데 Ink도 Discord inbound도 없으면 → **`ConfigError`**.

### 5.5 체크포인트 / resume

snapshot에 추가:

- `human_pending: bool`  
- (기존) `approvals`, `opened_at_seq`, `proposal_received`  

fingerprint에 `human_approval` 정규화 맵 포함.

---

## 6. 도구 승인과의 경계

| 상황 | 사용자에게 보이는 것 |
|------|---------------------|
| `rm -rf` 류 | 도구 승인 카드 |
| 페이즈 사인오프 | 에이전트 합의 **이후** “사람 승인 대기” → `APPROVE:` / `REJECT:` |

---

## 7. 구현 마일스톤

| 단계 | 내용 | 통과 |
|------|------|------|
| **H0** | 본 설계 동결 (`after_agents`) | ✅ |
| **H1** | config 파싱·검증 | ✅ |
| **H2** | `ConsensusGate.await_human_after_agents` + `human_pending` | ✅ |
| **H3** | Session 배선 + fake E2E | ✅ (게이트 단위 + Session 배선) |
| **H4** | system prompt | ✅ |
| **H5** | D5 Wire + D3 inbound thread | ✅ |
| **H6** | README + USER_INTERVENTION 정합 | ✅ |

---

## 8. 테스트 계획

| # | 시나리오 | 기대 |
|---|----------|------|
| T1 | 설정 없음 | 기존과 동일 |
| T2 | true, 에이전트만 만장일치 | `human_pending`, 페이즈 미진행 |
| T3 | T2 후 human APPROVE | 게이트 open, 페이즈 진행 |
| T4 | T2 후 human REJECT | approvals clear, pending 해제, 재합의 |
| T5 | human이 에이전트 합의 **전**에 APPROVE | 무시 (단2 아님) |
| T6 | `P1_EXPLORE: true` | ConfigError |
| T7 | interact 없음 | ConfigError (D4) |
| T8 | resume with `human_pending` | 복원 후 human APPROVE로 open |
| T9 | P1 READY 회귀 | human 없이도 P2 진입 |

---

## 9. 결정 요약 (동결 표)

| 항목 | 결정 |
|------|------|
| YAML | `protocol.human_approval: { P5_SUBMIT: true, … }` |
| 페이즈 기본 | **P2~P5 전부 `false`** (omit = false) |
| mode 기본 | **`after_agents`** |
| 순서 | **에이전트 만장일치 → 사람 요청/대기 → human APPROVE** |
| 스레드 participants | 에이전트만 (human 미포함) |
| P1 | 설정 불가 |
| 사람 요청 시점 | 에이전트 합의 직후 (D5 Wire) |
| interact 없음 | 기동 실패 (D4) |
| inbound default thread | 현재 게이트 스레드 (D3) |
| `co_sign` | 예약만, v1 미구현 |
| 도구 승인 Wire | 재사용 안 함 |

---

## 10. 관련 문서

- `docs/USER_INTERVENTION_DESIGN.md` — HITL 전체; §4.4는 본 문서 SSOT  
- `docs/architecture/TOOL_HUMAN_APPROVAL_DESIGN.md` — 도구 집행 게이트  
- `docs/architecture/PROTOCOL_GATE_WAIT_PARK_DESIGN.md` — 게이트 닫힘 idle park  
- `docs/architecture/IMPLEMENTATION_GAP_CONSOLIDATED.md` — P1 `human_approval`
