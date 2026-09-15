# Session checkpoint & resume — 설계

> **Status:** **M0–M3 landed** (CheckpointStore / conversation+phase+sqlite hydrate / CLI `--session` `--new-session` / headless+Ink D1–D3)  
> **Date:** 2026-09-15  
> **Priority:** P0 (실사용 연속성)  
> **Code:** `checkpoint.py`, `session.py` (`open_from_config`), `gateway/headless.py`, `gateway/session_stdio.py`, `cli.py`  
> **Tests:** `tests/test_checkpoint_resume.py`  
> **UX 동결:** D1 idle-wait · D2 append · D3 headless+Ink 기본 on (본 문서 §17)  
> **Related:**  
> - `docs/USER_INTERVENTION_DESIGN.md` (HITL / human 참가자)  
> - `docs/architecture/PROTOCOL_GATE_WAIT_PARK_DESIGN.md` (게이트 대기 park)  
> - `docs/architecture/TOOL_HUMAN_APPROVAL_DESIGN.md` (승인 토큰; pending 영속은 후속)  
> - `MessageServer` 기존 aiosqlite 경로 (`server.py` §3.5.4 D5 — 현재 `Session.from_config`는 `db_path=None`)

---

## 0. 한 줄

**프로세스 종료 후에도** 같은 업무를 이어갈 수 있도록, 런타임 상태의 **체크포인트를 디스크에 남기고 재기동 시 hydrate**한다.  
같은 task 문구를 다시 넣는 것은 resume이 아니다.

---

## 1. 배경 · 재현

### 1.1 사용자 재현 (실측)

1. Discord/headless로 세션 시작  
2. 프롬프트 예:  
   `---.xlsx 파일 읽어봐 여기 있는 상품의 상세페이지 기획 문서 작성해줘. 된다면 상세페이지 이미지도`  
3. 에이전트들이 정상 진행  
4. **인터럽트 + 프로세스 종료**  
5. `agent-augury` 재기동 후 **동일 프롬프트** 재입력  
6. 에이전트가 `python --version`, `print("hello")` 등 **도구 스모크**만 반복

### 1.2 근본 원인

| 상태 | 프로세스 유지 + 인터럽트만 | 인터럽트 + **종료** |
|------|---------------------------|---------------------|
| `AgentLoop.conversation` | 유지 (다음 `run()`에 append) | **소멸** |
| `MessageServer` 스레드/메시지 | 메모리 유지 | **소멸** (`db_path` 미연결) |
| `CollaborationProtocol.phase` / 게이트 | 유지 | **P1부터 재시작** |
| Discord 채널 히스토리 | 사람이 봄 | **모델 컨텍스트에 자동 주입 안 됨** |
| 승인 pending / inbox | 메모리 | **소멸** |

근거 코드:

- `Session.from_config` → `MessageServer()` (`db_path` 없음)
- `Session.run` docstring: *다음 `run()`은 clean start가 아니라 conversation 유지* — **동일 프로세스 한정**
- system prompt: `read_resource`로 히스토리를 **모델이 직접** 가져와야 함; 자동 push 없음

재기동 후 동일 프롬프트는 **콜드 스타트 + P1_EXPLORE + 도구 개방**과 동치다.  
첫 실행이 잘 된 것은 경로 의존(중간 도구 결과 축적)이지, 프롬프트 문자열이 “세션을 복원하는” 키가 아니다.

### 1.3 왜 지금 필요한가

- headless + Discord가 **장시간 로컬 작업** UX인데, Ctrl+C/종료가 흔함  
- 멀티에이전트는 중간 합의·파일 탐색 비용이 커서 **매번 콜드 스타트 비용이 큼**  
- 약한/free 모델은 콜드 스타트에서 스모크 툴 호출로 새기 쉬움

---

## 2. 목표와 비목표

### 2.1 목표

1. **명시적 세션 ID**로 체크포인트를 디스크에 저장한다.  
2. **정상 종료·인터럽트·크래시(가능하면)** 직전 상태를 복원할 수 있다.  
3. 재기동 시 기본 UX: **이어서 하기** (또는 “이어하기 / 새 세션” 선택).  
4. 복원 후 모델은 **이전 conversation + phase + (선택) 스레드 목록**을 보고 업무를 계속한다.  
5. 옵트인 가능하되, headless/Discord 실사용 프로필에서는 **기본 on**을 권장한다.  
6. 기존 `--demo` / 단위 테스트는 체크포인트 **off**로 결정론 유지.

### 2.2 비목표 (v1)

| 제외 | 이유 |
|------|------|
| 파일시스템 스냅샷 / git checkpoint | 별도 제품; conversation resume과 직교 |
| 멀티 호스트 공유 세션 | 로컬 싱글 유저 우선 |
| LLM 컨텍스트 무한 보존 (자동 요약 필수화) | v1은 truncate 정책만; 요약은 v2 |
| Discord 채널 메시지 전체를 모델에 리플레이 | 채널 ≠ Core SSOT; 체크포인트가 SSOT |
| pending approval 버튼 상태까지 완벽 복원 | v1은 pending을 **만료/deny**로 fail-closed; v1.1에서 영속 |
| 프로세스 유지 중 soft-interrupt와 구분되는 새 의미론 | 기존 `request_interrupt` 유지; 종료 시 flush만 추가 |

### 2.3 성공 기준

- 위 §1.1 재현에서, resume 후 **동일 업무를 xlsx 재탐색 스모크 없이** 이어서 진행 (또는 “이전 결과 파일 기준으로 이어서”가 가능).  
- conversation / phase가 체크포인트와 round-trip.  
- 에이전트 id 집합이 YAML과 불일치하면 **명확히 실패** (조용한 부분 복원 금지).  
- 체크포인트 손상 시 **새 세션으로 fall back** + Wire `log` 경고 (프로세스 hang 금지).

---

## 3. 원칙

| # | 원칙 | 설명 |
|---|------|------|
| R1 | **Core 상태가 SSOT** | Discord/Ink는 뷰. 복원은 Core 체크포인트에서만. |
| R2 | **메모리 primary, 디스크 secondary** | 기존 `MessageServer` D5와 동일. 런타임은 메모리; 주기·경계에서 flush. |
| R3 | **Fail-closed hydrate** | 스키마/에이전트 집합/버전 불일치 → resume 거부, fresh start 또는 에러. |
| R4 | **도구 부작용은 재실행하지 않음** | 복원은 “다시 생각하기”용 대화·협업 상태. grant된 툴을 자동 리플레이하지 않음. |
| R5 | **새 user 메시지 ≠ 무조건 새 세션** | resume 모드에서는 채널 입력이 **이어서** append; `--new-session`만 fresh. |
| R6 | **비밀 최소 저장** | OAuth 토큰·봇 토큰은 체크포인트에 넣지 않음 (기존 `.env` / TokenStore). |

---

## 4. 상태 분류

### 4.1 v1에 반드시 저장 (Must)

| 항목 | 위치 | 비고 |
|------|------|------|
| `session_id` | 신규 | UUID; 디렉터리/파일 키 |
| `config_fingerprint` | YAML path + agents[].id + protocol on/off 해시 | 불일치 시 resume 거부 |
| `created_at` / `updated_at` | 메타 | |
| `schema_version` | int | 마이그레이션 |
| per-agent `conversation` | `AgentLoop.conversation` | system 포함 전체 메시지 리스트 (tool_calls/tool results 포함) |
| per-agent `created_threads` | `AgentLoop.created_threads` | `$thread:N` 해석용 |
| per-agent `language` | | |
| protocol `phase` | `CollaborationProtocol.phase` | |
| gate 메타 | phase별 `thread_id`, `opened_at_seq` / 열린 여부, proposal 필요 여부 | 게이트 객체를 재바인딩 |
| MessageServer 스레드·메시지·seq | 기존 aiosqlite 스키마 재사용 또는 동일 JSON | inbox 재구성에 필요 |
| per-agent **undrained inbox** | `MessageServer` inbox 큐 | 인터럽트 직전 미처리 radio |
| `task` / 마지막 human turn 요약 | 메타 | UX·로그용 |
| `interrupted` / `exit_reason` | 메타 | `interrupted` \| `quit` \| `crash` \| `completed` |

### 4.2 v1에서 의도적으로 제외·단순화 (Defer)

| 항목 | v1 처리 |
|------|---------|
| `ApprovalStore` pending | hydrate 시 **전부 expired/denied** + 로그. 재요청은 모델이 다시 하면 됨 |
| Wire / Discord outbox | 복원 안 함 |
| Backend HTTP 연결 | 재연결 |
| Bot gateway 로그인 | `_setup`에서 재시작 |
| `total_steps` 카운터 | 0으로 리셋해도 무방 (예산은 새 turn 기준) 또는 체크포인트에 보존(선택) |

### 4.3 디스크上的 산출물 (참고)

에이전트가 쓴 `.md` / 이미지 등은 **체크포인트 밖**이다.  
resume는 “무엇을 이미 했는지”를 conversation으로 알려 주고, 파일은 워크스페이스에 그대로 둔다.  
(파일 롤백은 비목표.)

---

## 5. 저장 형식 · 위치

### 5.1 레이아웃 (제안)

```text
~/.agent-augury/sessions/
  <session_id>/
    meta.json           # id, schema_version, fingerprint, timestamps, exit_reason, phase
    conversations.json  # { "agent-1": [messages...], ... }
    server.sqlite       # MessageServer D5 (threads/messages) — 기존 스키마 우선
    protocol.json       # phase, gates binding, gate open flags
    inbox.json          # { "agent-1": [raw inbox msgs...], ... }  # sqlite에 없으면
```

대안(단순): 단일 `checkpoint.json` + 대용량 conversation.  
**권장:** conversation은 JSON, 메시지는 **기존 sqlite** 재사용 → `Session.from_config`에 `db_path`를 세션 디렉터리로 연결.

### 5.2 스키마 버전

```text
schema_version: 1
```

v1 → v2 마이그레이션은 로더에서 명시적 분기. 알 수 없는 버전은 resume 거부.

### 5.3 원자적 쓰기

1. `*.tmp`에 기록  
2. `fsync` (가능하면)  
3. rename으로 교체  
4. `meta.json`의 `updated_at` / `checkpoint_seq` 증가를 **마지막**에

부분 쓰기 중 크래시 → 이전 seq의 완전 스냅샷 유지.

---

## 6. 언제 flush 하는가

| 트리거 | 동기성 | 비고 |
|--------|--------|------|
| 각 agent `step()` 성공 직후 | async debounce (예: 500ms~2s coalescing) | 너무 잦은 디스크 IO 방지 |
| phase 전환 | 즉시 | |
| human.send / ask_user 관련 inbox push | debounce | |
| `request_interrupt()` | **동기 flush 시도** 후 루프 탈출 | 종료 직전 유실 최소화 |
| `session.close()` / quit | 동기 flush | |
| 주기 (예: 30s) | 백그라운드 | 장시간 park 대비 |

**크래시:** 마지막 성공 flush 지점까지 복원. step 도중 크래시는 해당 step의 부분 assistant 메시지 유실 가능 → 허용 (R3).

---

## 7. 시작 · Resume UX

### 7.1 세션 ID 결정

```text
우선순위:
1. CLI `--session <id>` / 환경변수 AGENT_AUGURY_SESSION
2. YAML `session.id` (선택 필드)
3. “last session” 포인터 `~/.agent-augury/sessions/LATEST` (같은 config fingerprint일 때만)
4. 없으면 새 UUID 발급
```

### 7.2 Headless / Discord (핵심 UX) — **동결**

프로세스가 살아날 때:

```text
if checkpoint exists ∧ fingerprint match ∧ not --new-session:
    hydrate Core
    publish Wire log: "resumed session <id> (phase=…, agents=…)"
    idle-wait: human 메시지 올 때까지 run() 하지 않음
    next human message → conversation에 무조건 append 후 run()
else:
    fresh Session
    first human message = 새 task (현재와 동일)
```

**동결 결정:**

| # | 결정 | 내용 |
|---|------|------|
| D1 | **idle-wait + 다음 메시지** | 재기동 직후 `run()` 자동 재개(auto-continue) **안 함**. Discord/headless는 hydrate 후 idle → 다음 human 입력에서 turn 시작. |
| D2 | **동일 프롬프트도 append** | 이전에 넣었던 task와 문자열이 같아도 skip/dedupe 하지 않음. 항상 새 user turn으로 append. |
| D3 | **체크포인트 기본 on** | headless **와** Ink/stdio **둘 다** `checkpoint.enabled: true` (단 `--demo`는 false). |

명시적 `/new` 또는 `--new-session` → fresh.

### 7.3 Ink / stdio — **동결 (D3)**

체크포인트·LATEST resume는 Ink에도 **기본 적용** (headless와 동일 store).

`resume: auto` (기본): 체크포인트가 유효하면 묻지 않고 hydrate 후 idle(또는 REPL에서 다음 입력 대기).  
`resume: ask`: 시작 시 확인:

```text
Resume session <short-id> (P3_EXECUTE, interrupted)? [Y/n/new]
```

`resume: never` / `--new-session`: 항상 fresh.

### 7.4 Wire 이벤트 (제안)

| type | 용도 |
|------|------|
| `session.checkpoint` | flush 완료 (observe, quiet 가능) |
| `session.resumed` | hydrate 성공 메타 |
| `session.resume_failed` | fall back 사유 |

기존 `session.started` / `session.ended`는 유지. `ended.reason`에 `interrupted` 기록.

---

## 8. Hydrate 절차 (순서)

```text
1. meta.json 로드 · schema_version / fingerprint 검증
2. MessageServer(db_path=server.sqlite) 생성 → load_from_db()
3. register_agent / register_human (YAML 순서)
4. inbox.json 또는 DB 보조 테이블에서 inbox 복원
5. AgentLoop 생성 후 conversation / created_threads / language 주입
   - system prompt는 현행 툴·phase로 _update_phase_in_prompt 한 번 갱신
6. CollaborationProtocol 구성
   - phase를 체크포인트 값으로 설정 (start()가 무조건 P1로 밀지 않도록
     `restore(phase=...)` API 추가)
   - gate thread_id 재바인딩; opened 상태 복원
7. ApprovalStore = 빈 저장소 (pending 폐기)
8. BotManager / Gateway _setup (네트워크)
9. Bridge recent_thread = human 스레드 복원
10. session.resumed 발행 후 **idle-wait** (다음 human 메시지까지 run() 금지 — D1)
```

### 8.1 `CollaborationProtocol.restore`

현행 `start()`는 `P1_EXPLORE`로 advance한다. resume용 API 필요:

```text
restore(phase, gate_snapshots) -> None
  - PhaseManager를 phase로 직접 세팅 (콜백은 1회 quiet 또는 replay 금지)
  - 각 ConsensusGate에 thread_id / opened_at_seq / 내부 승인 집합 복원
```

게이트 내부 “누가 APPROVE 했는지”까지 복원하지 않으면,  
열린 게이트는 `opened=True`로, 미개방 게이트는 **빈 승인 집합**으로 두되  
conversation·스레드 메시지에 이미 APPROVE가 있으면 다음 메시지/헬퍼로 재집계하거나  
v1에서는 “미개방이면 합의 다시”를 허용 (문서화).

**권장 v1:** 게이트 승인 집합도 protocol.json에 저장해 재집계 비용 제거.

---

## 9. Config

```yaml
session:
  id: null                    # 고정 ID (없으면 자동)
  checkpoint:
    enabled: true             # headless·Ink 공통 기본 true; --demo는 false
    dir: ~/.agent-augury/sessions
    flush_debounce_ms: 1000
    flush_interval_s: 30
    resume: auto              # auto | ask | never (둘 다 동일 의미)
```

CLI:

```text
agent-augury --config … --headless
agent-augury --config … --headless --session <id>
agent-augury --config … --headless --new-session
agent-augury sessions list
agent-augury sessions show <id>
```

`sessions list`는 v1.1 여유; v1은 디렉터리 + LATEST로도 충분.

---

## 10. Conversation 크기

멀티에이전트 × 긴 툴 결과는 체크포인트·API 컨텍스트 모두 비대해진다.

| 정책 | v1 |
|------|-----|
| 저장 | **전체** conversation 저장 (정확한 resume) |
| 모델 호출 | 기존 backend가 받던 그대로; 별도 truncate 없으면 공급자 한도에 맡김 |
| v2 | 에이전트별 요약 메시지 삽입 + 오래된 tool result tombstone |

디스크 용량 경고: `meta.json`에 `approx_bytes`; 임계 초과 시 Wire 경고.

---

## 11. 보안 · 프라이버시

- 체크포인트에 **봇 토큰 / OAuth refresh** 금지.  
- conversation에 사용자가 붙여 넣은 비밀이 있을 수 있음 → 파일 권한 `0600`, 디렉터리 `0700`.  
- `sessions` 삭제는 사용자 명시 (`sessions rm` 또는 수동).

---

## 12. 구현 마일스톤

### M0 — 스펙·스케폴드

- `CheckpointStore` 인터페이스 (save/load/list)  
- schema_version=1 dataclass  
- 단위 테스트: round-trip fixture  

### M1 — Conversation + phase only (최소 가치)

- flush on interrupt/close  
- hydrate conversation + phase (`MessageServer`는 빈 채로 시작 가능)  
- resume 후 모델이 “이전에 뭘 했는지”는 알지만 스레드 id는 깨질 수 있음  
- **이 단계만으로도** §1.1 스모크 재발은 크게 줄어듦  

### M2 — MessageServer sqlite 연결

- `Session.from_config(..., db_path=session_dir/server.sqlite)`  
- load_from_db + inbox 복원  
- gate thread 재바인딩  

### M3 — UX 완성

- `--session` / `--new-session` / `resume: auto`  
- Wire `session.resumed`  
- headless LATEST 포인터  
- README + Discord 운영 가이드 (“종료해도 이어짐”)  

### M4 — 경화 → **M4a–M4d landed** (M4e LLM 요약은 옵션/미구현)

상세: [`SESSION_RESUME_M4_DESIGN.md`](./SESSION_RESUME_M4_DESIGN.md)

| 내부 | 내용 | 상태 |
|------|------|------|
| M4a | pending 승인 → `approvals.json` + resume 시 재발행 | done |
| M4b | conversation 규칙 기반 compact / tombstone | done |
| M4c | `agent-augury sessions list\|show\|rm` | done |
| M4d | 손상 체크포인트 → `sessions/quarantine/` | done |
| M4e | (옵션) LLM 요약 | deferred |

**권장 출시 컷:** M1+M2+M3. M4a–d는 실사용 다듬기.

---

## 13. 테스트 계획

| # | 시나리오 | 기대 |
|---|----------|------|
| T1 | step 후 checkpoint round-trip | conversation/phase 동일 |
| T2 | interrupt → flush → 새 Session hydrate → run | 이전 assistant/tool 맥락 유지, phase 유지 |
| T3 | fingerprint 불일치 (agent 수 변경) | resume 거부, fresh 또는 에러 |
| T4 | 손상 meta / 중간 truncate | fall back + log |
| T5 | `--new-session` | 새 id, 이전 파일 미로드 |
| T6 | debounce coalescing | N step → flush 호출 수 ≪ N |
| T7 | protocol gate open 상태 복원 | 재기동 후 불필요한 P1 재진입 없음 |
| T8 | (통합) §1.1 유사: fake backend script로 “xlsx 경로 이미 앎” conversation 복원 후 다음 step이 스모크 커맨드가 아님 | 정책 검증 |

Windows: rename 원자성·경로 권한 테스트 포함.

---

## 14. 리스크 · 트레이드오프

| 리스크 | 완화 |
|--------|------|
| 체크포인트 비대 | M2 이후 크기 경고; v2 요약 |
| 게이트 승인 집합 미복원 시 교착 | v1부터 승인 집합 저장 (§8.1) |
| 사용자가 “새 일”인데 auto-resume | `--new-session` / `/new` 문서화; `resume: ask` |
| 툴 결과를 재실행으로 오해 | R4 명시; system에 “상태는 복원됨, 도구는 다시 돌리지 말고 이어서” 한 줄 옵션 |
| sqlite + JSON 이중 소스 | 단일 writer = CheckpointStore; MessageServer만 sqlite |

---

## 15. 운영자 가이드 (구현 전 임시 완화)

구현 전:

1. **종료하지 말고** 인터럽트만으로 이어가기  
2. 종료가 필요하면 재기동 프롬프트에 **절대 경로·중간 산출물 경로·이미 한 일**을 명시  
3. 산출물을 워크스페이스 파일로 남기기  

구현 후:

1. 기본 auto-resume  
2. 새 업무만 `--new-session`  
3. `sessions` 디렉터리 백업 = 업무 연속성 백업  

---

## 16. 결정 요약 (**동결**)

| 결정 | 선택 |
|------|------|
| SSOT | Core 체크포인트 (Discord 리플레이 아님) |
| 최소 가치 | conversation + phase (M1) |
| 완전 가치 | + MessageServer sqlite + gate (M2) |
| pending approval | v1 discard |
| 재기동 직후 | **idle-wait** — 다음 human 메시지에서 `run()` (D1; auto-continue 없음) |
| 동일 프롬프트 재입력 | **항상 append** (D2; dedupe 없음) |
| 체크포인트 기본 | headless **+ Ink** 둘 다 `enabled: true` (D3); `--demo`만 false |
| resume 모드 기본 | `auto` (유효 체크포인트면 hydrate); Ink에서 확인 UI는 `resume: ask` |
| 새 세션 | `--new-session` / `/new` / YAML `resume: never` |
| 툴 리플레이 | 안 함 |

---

## 17. UX 결정 기록

| # | 질문 | 결정 | 일자 |
|---|------|------|------|
| D1 | auto-continue vs idle-wait | **idle-wait + 다음 메시지** | 2026-09-15 |
| D2 | 동일 프롬프트 append vs skip | **append** | 2026-09-15 |
| D3 | 체크포인트 기본 범위 | **headless + Ink 둘 다** | 2026-09-15 |

이 세 항은 v1 구현 동결. 변경 시 본 표를 개정한다. M0 착수 가능.
