# Session resume M4 — 경화 설계

> **Status:** **M4a–M4e landed** (pending approvals / quarantine / sessions CLI / rule compact / optional LLM summary); default `llm_summary: false`  
> **Date:** 2026-09-15  
> **Parent:** `docs/architecture/SESSION_RESUME_DESIGN.md` (M0–M3 landed)  
> **Priority:** P1 (실사용 다듬기; resume 자체는 M3로 이미 가능)  
> **Code:** `core/checkpoint.py`, `core/compact.py`, `sessions_cli.py`, `core/agent/approval.py`, `core/session.py`, `cli.py`  
> **Tests:** `tests/test_m4_resume.py`  
> **UX 상속:** D1 idle-wait · D2 append · D3 headless+Ink 기본 on (변경 없음)

---

## 0. 한 줄

M0–M3가 “끄고 다시 켜도 이어짐”을 만들었다면, **M4는 그 세션을 안전하게·작게·관리 가능하게** 만든다.  
네 트랙: **pending 승인 영속**, **대화 압축**, **sessions CLI**, **손상 격리**.

---

## 1. 배경 — M3 한계

| M3 동작 | 실사용 불편 |
|---------|-------------|
| hydrate 시 `ApprovalStore` 비움 | 종료 직전 Approve 버튼이 떠 있던 요청이 사라짐 → 모델이 도구를 다시 요청하거나 사용자가 헷갈림 |
| conversation 전체 저장 | 긴 세션에서 JSON·API 컨텍스트 폭증 |
| 세션 id는 LATEST/`--session`만 | “어제 그거”를 찾기 어려움 |
| corrupt → 조용히 fresh | 깨진 디렉터리가 남고, 사용자는 왜 새 세션인지 모름 |

M4는 위 네 가지를 **제품 품질**로 올린다. resume 프로토콜(D1–D3)·SSOT(R1–R6)는 그대로다.

---

## 2. 목표 · 비목표

### 2.1 목표

1. **pending 승인**을 체크포인트에 넣고, resume 후 interact surface에 **다시 노출**한다.  
2. conversation이 임계를 넘으면 **요약 + tombstone**으로 줄이되, resume 의미는 유지한다.  
3. `agent-augury sessions …`로 list / show / rm / quarantine 관리.  
4. 손상·지문 불일치 체크포인트를 **quarantine**하고 Wire/CLI로 이유를 남긴다.

### 2.2 비목표

| 제외 | 이유 |
|------|------|
| Discord 메시지 리플레이로 승인 UI 복원 | Core SSOT; 버튼은 resume 후 **재발행** |
| 자동 LLM 요약 필수 (항상 on) | 비용·비결정; 기본은 규칙 기반 tombstone, LLM 요약은 옵션 |
| 멀티 호스트 세션 공유 | 로컬 싱글 유저 |
| git / 파일시스템 체크포인트 | 직교 기능 |
| granted 도구 자동 재실행 | R4 유지 |

### 2.3 성공 기준

- 종료 직전 pending 1건 → resume → 동일 `approval_id`로 Approve/Deny 가능 (TTL 유효 시).  
- 대화 토큰/바이트가 임계 초과 시 다음 flush에서 압축되고, 모델이 “이전 작업 요약”을 system/user로 본다.  
- `sessions list`에 id·phase·updated_at·상태(ok/quarantined) 표시.  
- 손상 meta는 `quarantine/`로 이동하고 fresh 세션 + `session.resume_failed` reason=`quarantined:…`.

---

## 3. 트랙 A — ApprovalStore pending 영속

### 3.1 저장 내용

파일: `~/.agent-augury/sessions/<id>/approvals.json`

```json
{
  "schema_version": 1,
  "records": [
    {
      "approval_id": "…",
      "agent_id": "a1",
      "tool": "run_command",
      "args_digest": "…",
      "args_snapshot": { "command": "…" },
      "created_at": 0.0,
      "expires_at": 0.0,
      "state": "pending",
      "reason": null
    }
  ]
}
```

- **pending만** 저장 (granted/denied/expired/executed는 flush 시 생략 또는 감사 로그 전용).  
- `args_snapshot`은 grant 시 실행에 필요 — v1 Store와 동일 필드.  
- 비밀: 명령/경로에 토큰이 있을 수 있음 → 파일 mode `0600` (기존 체크포인트와 동일).

### 3.2 Flush / hydrate

| 시점 | 동작 |
|------|------|
| `flush_checkpoint*` | `approvals.export_pending()` → `approvals.json` |
| hydrate (`_apply_resume_payload`) | `ApprovalStore.import_pending(records, *, now)` |
| import 시 TTL 만료 | state=`expired`, Wire `approval.expired` (가능하면), **버튼 재발행 안 함** |
| import 시 유효 pending | Store에 넣고, interact surface 있으면 `approval.request` **재발행** (Discord 버튼 재생성) |

### 3.3 Discord / Ink UX

- **새 approval_id를 만들지 않는다** — 같은 id로 재발행해야 중간 채널 메시지와 추후 resolve가 맞음.  
- Discord: 이전 메시지 버튼은 죽었을 수 있음 → resume 후 **새 메시지**로 카드 재전송 (id는 동일).  
- 사용자가 오래된 Discord 버튼을 누르면: unknown/expired 처리 (현행 fail-closed).

### 3.4 동시성 · 중복

- `request_or_join` digest 로직 유지.  
- import 후 동일 digest pending이 있으면 join 가능해야 함 (Store API가 이미 지원).

### 3.5 schema_version

체크포인트 `meta.schema_version`을 **2**로 올리거나, `approvals.json` 자체 `schema_version: 1`을 독립 유지.  
권장: **meta는 2**, 로더는 1(승인 파일 없음=빈 Store)과 2를 모두 수용.

---

## 4. 트랙 B — Conversation 요약 / tombstone

### 4.1 문제

멀티에이전트 × 긴 `run_command` stdout이 `conversations.json`과 매 스텝 API payload를 키운다.

### 4.2 정책 (기본 = 규칙 기반, LLM 없음)

에이전트별로 flush 직전(또는 step 후 debounce) 검사:

```text
if approx_chars(conversation) > soft_limit:   # 예: 200_000
    compact(agent)
```

`compact` 알고리즘 (결정론):

1. **system** 메시지는 유지 (또는 phase 반영된 최신 system 1개만).  
2. 최근 `keep_tail` 메시지(예: 40개, 또는 80_000 chars)는 그대로.  
3. 앞쪽 구간에서:
   - `role=tool` / tool result 내용은 **tombstone**으로 치환:  
     `[omitted tool result: <tool_name|id>, <N> chars]`  
   - 연속 assistant+tool 덩어리는 한 줄 요약 리스트로 접을 수 있음.  
4. 접힌 구간 앞에 **synthetic user** 1개 삽입:  
   `[checkpoint compact] Earlier work summary: …`  
   - 요약 텍스트는 규칙으로 생성: 등장한 파일 경로·마지막 user task·phase 이름 나열 (LLM 호출 없음).  
5. compact 사실을 `meta.compactions[]`에 기록 (`at_seq`, `chars_before`, `chars_after`).

### 4.3 설정

```yaml
session:
  checkpoint:
    compact:
      enabled: true
      soft_limit_chars: 200000
      keep_tail_chars: 80000
      keep_tail_messages: 40
      llm_summary: false      # true → agent backend 요약 (M4e); 실패 시 규칙 fall back
```

`--demo`에서는 compact off 가능 (결정론 테스트).

### 4.4 LLM 요약 (옵션 M4e) — **landed**

`llm_summary: true`일 때만 (`compact_conversation_async` / async `flush_checkpoint`·`close`):

- compact 대상 구간을 **해당 에이전트 백엔드**로 요약 (`tools=[]`).  
- 실패·빈 응답 시 규칙 기반으로 fall back (`meta.llm_summary: false`).  
- 비용·레이트 리밋 → 기본 **false**.  
- sync interrupt flush는 규칙만 (LLM 호출 없음).

### 4.5 모델 호출과의 관계

- 디스크 flush와 **모델에 보내는 conversation**은 동일 리스트를 쓰는 것이 단순 (compact in-place).  
- “디스크만 전체, API만 압축”은 v2 후보 (이중 소스).

### 4.6 테스트

- soft_limit 강제 → tombstone 삽입·system/tail 보존.  
- resume 후 compact된 conversation이 round-trip.  
- tool tombstone이 있어도 다음 step이 크래시하지 않음.

---

## 5. 트랙 C — `sessions` CLI

### 5.1 명령

```text
agent-augury sessions list [--dir DIR]
agent-augury sessions show <session_id>
agent-augury sessions rm <session_id> [--yes]
agent-augury sessions quarantine list
agent-augury sessions restore-quarantine <session_id>   # 고급; 기본 숨김 가능
```

서브커맨드 파서: `argparse` subparsers (`cli.py`).

### 5.2 `list` 출력 (테이블/TSV)

| 컬럼 | 출처 |
|------|------|
| id (short 8 + full) | dirname / meta |
| updated_at | meta |
| phase | meta |
| exit_reason | meta |
| agents | meta.agent_ids |
| fingerprint | meta |
| status | `ok` \| `corrupt` \| `quarantined` |
| approx_bytes | dir walk |

`LATEST` 포인터가 가리키면 `*` 표시.

### 5.3 `show`

- meta.json pretty  
- protocol.phase, compaction 횟수, pending approvals 수  
- conversations 메시지 수 / chars (에이전트별)  
- db sqlite 존재 여부  

### 5.4 `rm`

- `sessions/<id>/` 삭제 (확인 프롬프트; `--yes`로 스킵)  
- 삭제 대상이 LATEST면 LATEST 파일 삭제 또는 다음 최신으로 갱신  

### 5.5 구현 위치

- `agent_augury/checkpoint.py`: `list_sessions`, `read_meta`, `remove_session`, `quarantine_session`  
- `cli.py`: `sessions` 서브커맨드 → 위 함수  

Ink/headless 기동 경로와 독립 (읽기 전용 도구).

---

## 6. 트랙 D — 손상 체크포인트 quarantine

### 6.1 감지 조건

hydrate / `bootstrap_session` / `CheckpointStore.load` 중:

| 조건 | reason 코드 |
|------|-------------|
| meta/conversations JSON 파싱 실패 | `corrupt_json` |
| schema_version 불명 | `unsupported_schema` |
| fingerprint 불일치 | `fingerprint_mismatch` |
| agent id 집합 불일치 | `agent_mismatch` |
| sqlite 열기 실패 (M3 db 사용 시) | `sqlite_error` |
| approvals.json 파손 (M4) | `approvals_corrupt` (Store만 비우고 세션은 살릴지 / 전체 quarantine — **권장: 세션은 살리고 approvals만 drop + log**) |

### 6.2 동작 (fail-closed + 가시성)

```text
on hard failure:
  1. move sessions/<id>/ → sessions/quarantine/<id>-<utc>/
  2. write quarantine/<…>/REASON.txt
  3. clear LATEST if it pointed here
  4. allocate fresh session id
  5. Wire: session.resume_failed reason="quarantined:<code>:…"
  6. stderr / log 동일 문구
```

fingerprint / agent mismatch는 “손상”이 아니라 **설정 변경**일 수 있음.  
권장:

- `fingerprint_mismatch` / `agent_mismatch` → **quarantine 대신 leave-in-place** + fresh id (현 M3) + reason 로그.  
- **JSON/schema/sqlite**만 quarantine.

설정으로 통일 가능:

```yaml
session:
  checkpoint:
    quarantine_on:
      - corrupt_json
      - unsupported_schema
      - sqlite_error
    # fingerprint_mismatch: leave
```

### 6.3 quarantine 보관

- 기본 보관; `sessions rm` / 수동 삭제.  
- 자동 TTL 삭제는 M4.2 (예: 30일) — 기본 off.

---

## 7. 레이아웃 확장 (M4)

```text
~/.agent-augury/sessions/
  LATEST
  <session_id>/
    meta.json              # schema_version: 2
    conversations.json
    protocol.json
    inbox.json
    approvals.json         # NEW
    server.sqlite
  quarantine/
    <session_id>-<utc>/
      … (원본 트리)
      REASON.txt
```

`meta.json` 추가 필드:

```json
{
  "schema_version": 2,
  "compactions": [
    { "at": 1710000000.0, "agent_id": "a1", "chars_before": 250000, "chars_after": 90000 }
  ],
  "pending_approvals": 2
}
```

---

## 8. Config 요약

```yaml
session:
  id: null
  checkpoint:
    enabled: true
    dir: ~/.agent-augury/sessions
    flush_debounce_ms: 1000
    flush_interval_s: 30
    resume: auto
    approvals_persist: true          # M4 기본 true
    compact:
      enabled: true
      soft_limit_chars: 200000
      keep_tail_chars: 80000
      keep_tail_messages: 40
      llm_summary: false
    quarantine_on:
      - corrupt_json
      - unsupported_schema
      - sqlite_error
```

---

## 9. 구현 마일스톤 (M4 내부)

| 단계 | 내용 | 의존 |
|------|------|------|
| **M4a** | `approvals.json` export/import + resume 시 `approval.request` 재발행 | ApprovalStore 직렬화 API |
| **M4b** | 규칙 기반 compact + meta.compactions | flush 경로 |
| **M4c** | `sessions list/show/rm` | CheckpointStore 헬퍼 |
| **M4d** | quarantine move + REASON + Wire reason | bootstrap_session |
| **M4e** (옵션) | `llm_summary` → `compact_conversation_async` | ✅ landed (기본 off) |

권장 착수 순서: **M4a → M4d → M4c → M4b → M4e** (완료).  
(승인 복원·격리·관리 CLI가 체감 크고, compact는 긴 세션 나올 때.)

---

## 10. 테스트 계획

| # | 시나리오 | 기대 |
|---|----------|------|
| A1 | pending 저장 → resume → resolve grant | 동일 id로 도구 실행 |
| A2 | pending TTL 만료 후 resume | expired, 버튼 없음 |
| A3 | resume 후 Discord 재발행 | Wire `approval.request` 1회+ |
| B1 | soft_limit 강제 compact | chars 감소, system/tail 유지 |
| B2 | compact 후 resume | tombstone 유지 |
| B3 | `llm_summary` 성공 | meta.llm_summary true + 요약 본문 |
| B4 | `llm_summary` 실패 | 규칙 fall back, meta.llm_summary false |
| C1 | list에 LATEST·phase | 파싱 가능 |
| C2 | rm 후 list 없음 / LATEST 갱신 | |
| D1 | 깨진 JSON | quarantine 이동 + fresh |
| D2 | fingerprint mismatch | leave-in-place + fresh (quarantine 아님) |

---

## 11. 리스크

| 리스크 | 완화 |
|--------|------|
| args_snapshot에 비밀 | 0600; README 경고 |
| compact가 맥락 과삭제 | keep_tail + 경로 나열 요약; soft_limit 보수적 기본값 |
| 승인 재발행 스팸 (N봇) | 기존 inbound 단일/dedupe 권장; 재발행은 agent당 1회 |
| quarantine 디스크 | list에 크기 표시; 문서에 정리 방법 |

---

## 12. 결정 요약 (동결 후보)

| 항목 | 제안 |
|------|------|
| pending 영속 | `approvals.json`, 유효 TTL만 재발행 |
| compact 기본 | 규칙 기반 tombstone; LLM off (`llm_summary` 옵션 landed) |
| fingerprint mismatch | quarantine 말고 leave + fresh |
| 진짜 corrupt | `sessions/quarantine/` |
| CLI | `sessions list\|show\|rm` |
| meta schema | 2 (1 로더 호환) |

열린 질문 없이 위 표로 구현 들어가도 되고, compact 기본 on/off만 운영 취향에 따라 정하면 된다.

---

## 13. Parent 문서와의 관계

- `SESSION_RESUME_DESIGN.md` §12 M4 한 줄 → 본 문서가 상세 SSOT.  
- M3 코드 경로(`CheckpointStore.save/load`, `Session.flush_checkpoint*`, `bootstrap_session`)에 훅만 추가.  
- D1–D3·R1–R6 재정의 없음.
