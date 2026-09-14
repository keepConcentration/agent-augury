# Tool human-approval (fail-closed, pending) — 배경 · 설계 · 구현 계획

> **Status:** **M0–M4 landed** (policy / Store / execute gate / Ink + Discord resolve / README); remaining optional: Discord buttons, pending 영속, smart 분류  
> **Date:** 2026-09-14  
> **Priority:** P0 (제품 신뢰도)  
> **Related:**  
> - `PRODUCT_GAPS_AND_ORCHESTRATOR_TRANSFER.md` §4 P0  
> - `docs/USER_INTERVENTION_DESIGN.md` (ask_user / HITL; 본 문서는 **부작용 도구 집행 게이트**)  
> - `docs/AGENT_TOOLS_EXPANSION_DESIGN.md` (`ToolPolicy`)  
> - `docs/architecture/IMPLEMENTATION_GAP_CONSOLIDATED.md` (`human_approval` 미구현 항목과 구분)  
> **비교 원본:** `~/orchestrator` D14 (`gate.sh` fail-closed), Hermes Agent (`tools/approval.py` / `pending_approval`)

---

## 0. 한 줄

위험 도구(`run_command` / 파일 쓰기 등)는 **승인 토큰 없이 본문을 실행하지 않는다**.  
승인은 **blocking `await`가 아니라** `pending_approval` → Wire → inbox `[radio]`로 흡수한다  
(원칙 #1 *Keep working while listening*).

---

## 1. 배경

### 1.1 제품 문제

agent-augury는 동시 멀티 LLM 협업 런타임으로 Wire / Ink / `ask_user` 골격은 있다.  
그러나 **실사용 로컬 툴** 관점에서 가장 큰 신뢰 갭은:

- `run_command` / `write_file` / `edit_file` / `append_file` 앞에 **사람 승인 하드 게이트가 없음**
- `ask_user`의 `REQUEST_APPROVAL:`은 **도구 설명 관례**일 뿐, 런타임이 실행을 막지 않음
- `ToolPolicy`는 blocklist / `allowed_roots` / timeout 등 **정적 안전장치**만 제공
- Interactive Surface가 없어도 위험 도구는 그대로 열릴 수 있음 (헤드리스 시 “영원히 pending” 위험도 있음)

`USER_INTERVENTION_DESIGN.md`의 `human_approval` / `REQUEST_APPROVAL:` 게이트는  
**프로토콜·합의 메시지 경로**에 가깝고, 본 문서는 **도구 부작용 집행 경로**를 다룬다. 둘은 보완 관계다.

### 1.2 외부 시스템에서 배운 것

| 시스템 | 강점 | augury에 그대로 쓰기? |
|--------|------|------------------------|
| **orchestrator D14** | 산문이 아니라 코드가 fail-closed 집행; 승인 값 정확 일치(G5); 감사 태그 | **정신만** — 워커 one-shot·파일 SSOT 모델은 비목표 |
| **Hermes Agent** | 위험 명령 감지 + `smart`/`manual`/`off`; 게이트웨이 `pending_approval`; cron/`-q`는 즉시 deny | **gateway pending 패턴**은 적합; CLI 300s blocking wait는 부적합 |
| **agent-augury (현)** | inbox / `[radio]` / 비차단 step; 다에이전트 병렬 | 승인 대기에도 동일 모델을 써야 함 |

### 1.3 적합 결론 (이미 합의)

**집행 철학 = orchestrator (D14 fail-closed)**  
**실행 형태 = Hermes gateway (`pending_approval`)**  
**타이밍 모델 = augury inbox / `[radio]`**

Hermes `smart` LLM 분류·orchestrator `gate.sh` 전면 워커 게이트는 P0에 넣지 않는다.

---

## 2. 목표와 비목표

### 2.1 목표

1. Config로 위험 도구 클래스별 승인 정책을 켠다 (`require` / `off`).
2. 승인 필요 호출은 **즉시** `pending_approval`을 도구 결과로 반환한다 (본문 미실행).
3. Wire로 `approval.request`를 interact surface에 발행한다.
4. 사람 grant/deny → 요청 에이전트 inbox → 다음 `step()`에서 `[radio]`로 흡수.
5. **GRANTED + 토큰 유효**일 때만 부작용을 실행하고 결과를 inbox/radio로 전달한다.
6. interact surface가 없으면 **즉시 DENIED** (`no_approval_channel`) — hang 금지.
7. `--demo` / 테스트 bypass로 결정론 테스트 유지.
8. 감사 이벤트: `approval.granted` / `tool.denied` / `approval.expired`.

### 2.2 비목표 (P0)

| 제외 | 이유 |
|------|------|
| `_execute_tool` 내 human 응답 `await` | 해당 에이전트 step 정지 → 원칙 #1 위반 |
| Hermes `smart` 보조 LLM 분류 | 비용·비결정·멀티에이전트 스팸; P2 후보 |
| 파일-only 승인 로그로 MessageServer 대체 | SSOT 정체와 충돌 |
| 자동 git revert / 컨테이너 격리 | P2 blast-radius; 본 P0은 집행 게이트만 |
| 프로토콜 `REQUEST_APPROVAL:` ConsensusGate | 별도 HITL 트랙 (`USER_INTERVENTION_DESIGN`) |
| Slack interact 신규 구현 | Discord inbound 재사용 + “미지원은 deny”로 충분 |

### 2.3 성공 기준

- 정책 `require`인 도구는 토큰 없이 subprocess/파일 I/O가 **한 번도** 돌지 않는다 (테스트로 증명).
- 승인 대기 중 **다른 에이전트**는 계속 step 가능하다.
- interact surface 0개 + `require` → 첫 호출이 `denied`/`no_approval_channel`.
- `--demo` 세션의 기존 fake E2E가 승인에 걸리지 않는다.

---

## 3. 원칙

| # | 원칙 | 함의 |
|---|------|------|
| A | Fail-closed | 애매·채널 없음·만료·digest 불일치 → **거부** (허용 아님) |
| B | Non-blocking approval | 최초 `_execute_tool`은 pending만; 코루틴 block 금지 |
| C | 동료와 동일 흡수 경로 | grant/deny/실행 결과는 inbox → `[radio]` |
| D | 정적 policy ≠ 동적 토큰 | `ToolPolicy`는 클래스별 require/off; 호출별 토큰은 `ApprovalStore` |
| E | 능력 = 구현 | 승인 채널이 없으면 “대기”가 아니라 deny |
| F | Demo 명시 바이패스 | 프로덕션 기본 bypass 없음 |

---

## 4. 설계

### 4.1 Config

```yaml
tools:
  approval:
    shell: require          # require | off
    file_write: require     # write_file / edit_file / append_file
    web: off                # P0 기본 off (부작용 상대적 약함)
    bypass: false           # true 또는 --demo → 자동 GRANTED
    ttl_seconds: 600        # pending 만료
```

- 글로벌 `tools.approval` + (선택) 에이전트별 deep-merge는 기존 `ToolPolicy` 병합 패턴을 따른다.
- CLI `--demo`는 `bypass: true`와 동치로 취급한다.
- **기본값 제안 (P0 도입 시):**  
  - 호환을 위해 첫 릴리스는 `shell`/`file_write` 기본 `off` + README에서 `require` 권장,  
    **또는** 브레이킹으로 기본 `require` (제품 신뢰 우선).  
  - **권장 결정:** 첫 구현은 기본 `require`, `--demo`/테스트만 bypass.  
    (기존 실사용 YAML은 릴리스 노트에 마이그레이션 한 줄.)

`ToolPolicy` 확장 필드 초안:

```text
approval_shell: "require" | "off"
approval_file_write: "require" | "off"
approval_web: "require" | "off"
approval_bypass: bool
approval_ttl_seconds: float
```

### 4.2 승인 대상 도구 클래스

| 클래스 | 도구 | P0 |
|--------|------|-----|
| `shell` | `run_command` | require 권장 |
| `file_write` | `write_file`, `edit_file`, `append_file` | require 권장 |
| `web` | `web_search`, `fetch_url` | 기본 off |
| (비대상) | `create_thread`, `send_message`, `read_resource`, `ask_user`, `read_file`, … | 승인 불필요 |

읽기 전용 파일/`read_resource`는 P0 게이트 밖 (기존 `allowed_roots`만).

### 4.3 ApprovalStore (동적 토큰)

Session이 소유하는 in-proc 저장소 (P0 최소 = 메모리; A5 영속은 후속).

| 필드 | 의미 |
|------|------|
| `approval_id` | 안정 UUID |
| `agent_id` | 요청 에이전트 |
| `tool` | 도구 이름 |
| `args_digest` | 정규화 args 해시 (경로·커맨드 포함) |
| `args_snapshot` | grant 시 실행에 쓸 인자 (불변 스냅샷) |
| `created_at` / `expires_at` | TTL |
| `state` | `pending` \| `granted` \| `denied` \| `expired` \| `executed` |

**정확 일치 (orchestrator G5):**  
grant 후 실행 직전 `args_digest` 재계산 ≠ 저장값 → 거부 + `tool.denied`.

**중복 합류:**  
동일 `(agent_id, tool, args_digest)` 에 미만료 `pending`이 있으면  
새 id를 만들지 않고 기존 `approval_id`를 `pending_approval` 결과에 재사용.  
Wire `approval.request` 재발행은 throttle (생략 또는 최소 간격).

**TTL:**  
만료 시 `pending → expired`, 해당 에이전트 inbox에  
`DENIED (reason=expired, approval_id=…)` radio push.  
조용한 drop 금지.

### 4.4 실행 모델 (시퀀스)

```text
AgentLoop._execute_tool(name, args)
  │
  ├─ approval 불필요 / bypass
  │     → 기존대로 handler 실행
  │
  ├─ require ∧ interact surface 없음
  │     → 즉시 {"status":"denied","reason":"no_approval_channel"}
  │     → Wire tool.denied (감사)
  │
  ├─ require ∧ (기존 pending 합류 or 신규 토큰)
  │     → ApprovalStore upsert pending
  │     → Wire approval.request
  │     → return {"status":"pending_approval","approval_id":...}   ★ 본문 미실행
  │
  └─ (이후) human.answer / approval.resolve
        → Store grant|deny
        → GRANTED: args_digest 검증 → handler 실행 → inbox에 결과 radio
        → DENIED/만료: inbox에 DENIED radio
        → Wire approval.granted | tool.denied | approval.expired
```

**Grant 시 실행 위치 (채택):**  
사람이 grant하는 시점에 Core가 **스냅샷 args로 실행**하고 결과를 inbox에 넣는다.  
모델이 동일 호출을 재시도하게 두지 않는다 (UX 단순, 이중 실행 위험 감소).  
재시도가 와도 digest가 같고 `state=executed`면 idempotent 결과 또는 `already_executed` 반환.

### 4.5 Wire / Surface

**이벤트 (초안)** — `schemas/wire/events.schema.json`에 추가:

```json
{
  "type": "approval.request",
  "approval_id": "...",
  "agent_id": "...",
  "tool": "run_command",
  "args_preview": { "...": "..." },
  "ttl_seconds": 600
}
```

```json
{
  "type": "approval.resolved",
  "approval_id": "...",
  "decision": "granted|denied|expired",
  "reason": "..."
}
```

**커맨드:**  
기존 `human.answer`를 재사용하거나 `approval.resolve`를 추가한다.

- 재사용 시: `human.answer` payload에 `approval_id` + `decision` 필드를 둔다.
- Ink: `ask_user`와 유사한 승인 카드 (Approve / Deny).
- Discord inbound: 기존 interact 경로로 동일 커맨드 전달.

**interact surface 판정:**  
Gateway에 `mode=interact`로 attach된 surface가 1개 이상이면 “채널 있음”.  
observe-only (webhook mirror)는 승인 채널로 치지 않는다.

### 4.6 Inbox radio 포맷 (초안)

```text
[radio] from human: approval_id=<id> GRANTED tool=run_command
[radio] from human: approval_id=<id> DENIED reason=user|expired|digest_mismatch|no_approval_channel
[radio] from human: approval_id=<id> RESULT tool=run_command <json or summary>
```

모델이 다음 step에서 결과를 보고 후속 행동을 이어가도록 system/tool 안내를 보강한다  
(과한 프롬프트 변경은 최소: tool result JSON의 `status`만으로도 충분하게).

### 4.7 `_execute_tool` 삽입점

현재 (`agent/loop.py`):

1. `local_tools` handler  
2. gate-closed `send_message` 가드  
3. `tools.execute`

승인 가드는 **local 부작용 도구 handler 호출 직전** (1번 경로)에 둔다.  
`send_message` 게이트 가드와 독립.

Local tool wrapper 또는 `AgentLoop` → policy 조회 → `ApprovalStore` 접근이 필요하므로  
Session이 store / “has_interact_surface” 콜백을 loop에 주입한다.

### 4.8 `--demo` / 테스트

| 조건 | 동작 |
|------|------|
| `allow_fake` / `--demo` | approval bypass (자동 granted 취급, Store 생략 가능) |
| `tools.approval.bypass: true` | 동일 (테스트 전용; README에 경고) |
| 단위 테스트 | bypass 또는 mock Store + fake interact surface |

기존 fake YAML E2E가 깨지지 않게 CI는 `--demo` 경로를 유지한다.  
승인 자체 테스트는 bypass 없이 mock surface로 별도 모듈.

### 4.9 감사

Core 이벤트 또는 Wire:

- `approval.request` (이미 §4.5)
- `approval.granted` / `approval.expired`
- `tool.denied` (`reason`)

선택: aiosqlite 미러는 P1(A5)과 묶는다. P0은 in-proc + Wire 구독으로 충분.

### 4.10 보안 메모

- YOLO/bypass는 **프로세스 시작 시 고정**하거나 config 로드 시점에만 읽고,  
  런타임 중 에이전트/스킬이 env로 우회하지 못하게 한다 (Hermes `_YOLO_MODE_FROZEN` 교훈).
- `args_digest`는 키 정렬·경로 normalize 후 해시.
- grant 실행은 **요청 에이전트의 policy** (cwd, allowed_roots, shell allow/block)를 그대로 적용.

---

## 5. 구현 계획

### 5.1 마일스톤

| Phase | 내용 | 완료 조건 |
|-------|------|-----------|
| **M0** | 스키마·정책·Store 골격 | 단위 테스트: digest, TTL, 합류, fail-closed API |
| **M1** | `_execute_tool` pending 경로 + grant 시 실행 | shell/file_write 통합 테스트; 본문 미실행 assert |
| **M2** | Wire `approval.*` + Bridge/`approval.resolve` + Ink 카드 | Bridge 단위 테스트 + Ink Approve/Deny UI |
| **M3** | interact 없음 → deny; Discord inbound 응답 | ✅ 헤드리스 + Discord 단위 |
| **M4** | 감사 이벤트 + README/마이그레이션 노트 | ✅ README Tool approval 절 |

### 5.2 예상 터치 파일

| 영역 | 파일 |
|------|------|
| Policy | `agent/policy.py`, `config.py` |
| Store | 신규 `agent/approval.py` 또는 `session` 하위 |
| Loop | `agent/loop.py`, local tools (`shell`/`file`) |
| Session | `session.py` (store 소유, surface 유무, inbox push) |
| Wire | `schemas/wire/events.schema.json`, `commands.schema.json` |
| Gateway | `gateway/bridge.py`, `translate.py`, `session_stdio.py` |
| Ink | `fronts/ink/src/*` (승인 UI) |
| Discord | inbound → `approval.resolve` / `human.answer` |
| Tests | `tests/test_tool_approval.py` 등 |
| Docs | README Quick start 보안 절, 본 문서 status → implemented |

### 5.3 테스트 매트릭스 (필수)

1. `require` + mock interact → pending → grant → 명령/쓰기 1회 실행  
2. `require` + deny → 본문 0회  
3. `require` + surface 0 → 즉시 `no_approval_channel`  
4. digest 불일치 grant 시도 → deny  
5. TTL 만료 → expired radio  
6. 동일 args 재호출 → approval_id 합류  
7. `--demo` bypass → 기존 E2E 통과  
8. 에이전트 A pending 중 에이전트 B step 계속 (비차단)

### 5.4 롤아웃

1. 플래그/기본값 확정 (권장: 기본 `require`).  
2. 예제 YAML에 `tools.approval` 명시.  
3. 릴리스 노트: breaking 여부·`--demo`·bypass 경고.  
4. P1 후보로 남길 것: A5 pending 영속, Hermes-like `smart` 분류, scope_check 스냅샷.

### 5.5 리스크

| 리스크 | 완화 |
|--------|------|
| 모델이 pending을 무시하고 다른 경로로 우회 | local tool 외 실행 경로 없음 유지; shell만 게이트해도 파일 도구 별도 require |
| 승인 UI 미비로 실사용 deny만 발생 | M2 Ink 카드를 P0 완료 조건에 포함 |
| grant 시점 실행 vs 모델 재시도 혼선 | §4.4 “grant 시 실행” 고정 + `executed` idempotent |
| 멀티 에이전트 승인 스팸 | digest 합류 + Wire throttle |

---

## 6. P0 체크리스트

- [x] `tools.approval` config + `ToolPolicy` 필드  *(M0)*
- [x] `ApprovalStore` (pending/grant/deny/expire + digest + 합류)  *(M0)*
- [x] `_execute_tool` pending 반환 (blocking await 금지)  *(M1)*
- [x] grant 시 부작용 실행 + inbox 결과  *(M1)*
- [x] Wire `approval.request` / resolve + 감사 이벤트 스키마·Core 발행  *(M0–M2: Bridge `approval.resolve` 라우팅 포함)*
- [x] interact surface 없음 → 즉시 deny  *(M1)*
- [x] `--demo` / `bypass`  *(M1: `Session.from_config(approval_bypass=…)` / `session_stdio --demo`)*
- [x] TTL → `DENIED(reason=expired)` radio  *(M1: `Session.expire_approvals`)*
- [x] Ink 승인 UI (최소 Approve/Deny)  *(M2)*
- [x] Discord inbound로 resolve (있으면)  *(M3)*
- [x] 테스트 매트릭스 §5.3 중 Store/policy/gate + execute/grant/deny/bypass/expire  *(M0–M1)*
- [x] README / 릴리스 노트  *(M4: README Tool approval 절)*

---

## 7. 결정 로그

| ID | 결정 | 일자 |
|----|------|------|
| T1 | 승인은 pending/radio; `_execute_tool` await 금지 | 2026-09-14 |
| T2 | Fail-closed = 토큰 없이 본문 미실행; 채널 없으면 deny | 2026-09-14 |
| T3 | Grant 시점에 스냅샷 args로 실행 (모델 재시도 계약 아님) | 2026-09-14 |
| T4 | P0에 Hermes smart LLM 분류 제외 | 2026-09-14 |
| T5 | 기본값 권장 = `shell`/`file_write` require (`--demo` bypass) | 2026-09-14 |

---

## 8. 참고

- orchestrator: `_shared/design-basis.md` D14, `_shared/adapters/gate.sh`, `approval-policy.md`  
- Hermes: `tools/approval.py`, `website/docs/user-guide/security.md` (`approvals.mode`, `pending_approval`)  
- augury: `agent/loop.py` `_execute_tool`, `agent/policy.py` `ToolPolicy`, DESIGN 원칙 #1
