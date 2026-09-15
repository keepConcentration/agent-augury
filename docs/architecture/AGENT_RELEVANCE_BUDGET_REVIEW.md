# AGENT_RELEVANCE_BUDGET_DESIGN 검토 및 후속 작업 (리뷰)

> **대상 문서:** `docs/architecture/AGENT_RELEVANCE_BUDGET_DESIGN.md`
> **검토 기준:** 현재 소스 `src/agent_augury/` (v0.7, 2026-09-15 시점) 대조
> **결론 (당시):** 설계 방향은 타당. 구현 전 갭 — (1) skip≠종료, (2) human 예외, (3) F3 근거 부재, (4) checkpoint 정합.
> **현황 (V1 landed):** `core/attention.py` · loop/session/config 배선 · `tests/test_attention_budget.py` ✅.  
> P0-1/P0-2/P1-1/P1-4 반영. P0-3(digest checkpoint)은 V1에서 digest=conversation 옵션만 — 큐 영속은 후속.
---

## 0. 요약 (TL;DR)

- 설계 문서가 전제한 삽입점(`AgentLoop.step()`의 drain→`backend.complete`)과 안전장치(park wake = `inbox_size > 0`, `complete(messages, tools)`에 `max_tokens` 없음)는 **모두 실제 코드와 일치**한다. 문서의 코드 이해도는 높다.
- V1을 그대로 구현하면 되는 수준이지만, 문서에 **빠진 핵심 3가지**가 있다:
  1. **T0 스킵 ≠ 에이전트 종료** 구분 — `session.py`의 `result.text is None → break` 경로와 충돌.
  2. **human 메시지 강제 처리** — 사용자 메시지가 휴리스틱에 의해 T0로 무시될 수 있음.
  3. **checkpoint/resume 정합** — digest 큐 등 신규 in-memory 상태가 재개 시 유실/이중 주입될 수 있음.
- 그 외에 **F3 phase_role의 근거 데이터 부재**, **`RelevancePolicy` 시그니처에 context 누락**, **`context_max_chars` 의미 모호**, **batch max의 전량 주입 낭비**는 문서 내 불일치/미결정 항목으로 정리 필요.

---

## 1. 검토 범위 · 근거 (대조한 코드)

| 파일 | 확인 내용 |
|------|-----------|
| `src/agent_augury/core/agent/loop.py` | `format_radio_block()`(51), `step()`(153)에서 `drain_inbox`(157) → `backend.complete`(164) 무조건 호출. budget 분기 없음. |
| `src/agent_augury/core/session.py` | `run_agent`(1165), `total_steps[0] += 1`(1209), `has_pending`(1220), `_is_gate_waiting`(1258), `_maybe_nudge_ready`(1273), `_wait_for_gate_wakeup`(1298, wake 조건 1325), `result.text is None → break`(1244). |
| `src/agent_augury/core/server.py` | `send_message`(276: 멘션/브로드캐스트 라우팅), `human_send`(350~), `inbox_size`(469), `drain_inbox`(503), `snapshot`(537). 메시지 dict에 `thread_id/author/content/mentions/delivered_to/created_at/seq` 보유. |
| `src/agent_augury/core/protocol/collaboration.py` | `assembler_id`(82), `has_ready`(181), `gate_for`(304). 페이즈는 `phases.py`의 P1_EXPLORE~P5_SUBMIT. |
| `src/agent_augury/backend/base.py` | `complete(self, messages, tools)`(35) — `max_tokens`/tool allowlist 없음 → V1.1 이월 타당. |
| `src/agent_augury/backend/fake.py` | `FakeModelBackend.calls`/`call_count`(17~28) — complete 호출 횟수·주입 messages 측정 가능 (테스트 계획에 활용 가능). |
| `src/agent_augury/config.py` | `load_config`(329) 반환 dict. `attention` 섹션 검증/정규화 없음. 기본값 주입은 `data.setdefault(...)`(524 부근) 패턴. |
| `src/agent_augury/core/compact.py` | conversation history 압축(장기). attention의 "단기·상대별 주입 슬라이스"와 직교 — 문서 판단과 일치. |
| `examples/benchmark/` | L3 passive awareness 벤치(결정적, FakeModelBackend, 수치 단언). V1 절감 측정 하니스의 참고 모델. |

---

## 2. 설계 평가 (잘 된 점)

1. **"수신 budget 우선 + SSOT 보존"** 판단이 실제 구조와 정확히 맞다. 서버의 `send_message`는 저장·전달만 하고 `step()`이 유일한 inbox 소비자라는 계약을 그대로 존중한다.
2. **T0 = drain 필수 + `complete`만 스킵**이라는 불변식은 `session.py:1325`의 `inbox_size > 0 → wake`와 정확히 연결되어 있어, "step 호출 생략" 금지의 이유가 코드로 증명된다. 이 지점을 문서가 정확히 짚었다.
3. **V1.1 이월(API 갭)** 판단이 맞다. `ModelBackend.complete`에 `max_tokens`/tool allowlist가 없음을 확인했고, V1 측정을 "skip 횟수 + 주입 chars"로 한정한 것은 `FakeModelBackend.calls`로 바로 측정 가능해 현실적이다.
4. **`enabled: false` 기본값(옵트인)** 은 기존 테스트/데모 회귀 방지에 필수이며, `config.py`의 정규화 패턴(`normalize_human_approval`)으로 자연스럽게 붙일 수 있다.
5. **A6/Gateway 표면과의 분리** — `ink/src/wire.ts`의 표시 포맷과 Core 인지를 섞지 않은 것도 적절하다.

---

## 3. 코드 대조 결과 (설계 가정 vs 실제)

| 설계 문서 주장 | 실제 코드 | 판정 |
|----------------|-----------|------|
| `step()`이 drain 후 `backend.complete` 호출 | `loop.py:157,164` — 일치 | ✅ |
| park wake 조건 = `inbox_size > 0` | `session.py:1220,1325` — 일치 | ✅ |
| `complete(messages, tools)`에 `max_tokens` 없음 | `base.py:35-38` — 일치 | ✅ |
| `near_gate` 플래그 없음 → `gate_for(phase)+not is_open` | `session.py:1258-1272`, `collaboration.py:304` — 일치 | ✅ |
| `format_radio_block()` 전량 주입 (skim 시 변경 대상) | `loop.py:51-60` — 일치 | ✅ |
| `total_steps`는 성공한 step마다 증가 | `session.py:1209` — T0 미계상은 **미구현 포인트** | ⚠️ |
| `StepResult`에 LLM 실행 여부 없음 | `loop.py` `StepResult` — 필드 추가 필요 | ⚠️ |
| F3 "assignee/proposer/assembler" | 코드에는 `assembler_id`만 존재, assignee/proposer 개념 없음 | ⚠️ |
| human 메시지도 inbox 경유 | `server.py` `human_send` — 문서에 human 예외 규정 없음 | ⚠️ |
| SSOT 보존 / 메시지 영구 삭제 금지 | `drain_inbox`가 dict 사본 반환, SSOT 유지 — 일치 | ✅ |

---

## 4. 발견된 문제 · 불일치

### P0 — 안전/회귀 (구현 전 반드시 결정)

**P0-1. T0 스킵이 "에이전트 종료"로 오인된다.**
`run_agent`는 `result.text is None → break`(session.py:1244)로 에이전트를 종료한다. T0 스킵(`run_llm=False`)이 `text=None`을 반환하면, 게이트 대기 중이 아닌 열린 구간(P2~P5 실행 중)에서 무관 broadcast만 받은 에이전트가 **조기 종료**되어 이후 참여 기회를 잃는다.
→ `StepResult`에 `run_llm`/`skipped_llm`을 추가하고, 스킵 step은 `break`가 아니라 `continue`(또는 park)로 처리. "LLM 미호출"과 "완료(무출력)"를 분리해야 한다.

**P0-2. human(사용자) 메시지에 대한 예외가 없다.**
`server.human_send`가 inbox로 메시지를 넣으므로, 사용자 직접 메시지가 휴리스틱 F1~F6만으로 T0/T1이 될 수 있다. 사용자 메시지는 **항상 engage 이상**으로 강제해야 한다 (예: `author ∈ server.humans → r=1.0` 또는 floor 적용). 문서 §4.1/§6 어디에도 이 규정이 없다.

**P0-3. checkpoint/resume과 attention 상태 정합 누락.**
`checkpoint.py`가 inbox ids를 `export_inbox_ids`/`restore_inbox_ids`로 저장/복원하지만, V1에서 추가될 in-memory 상태(T0 digest 큐, skim 주입 여부 등)는 복원 경로가 없다. 재개 시 "drain은 됐는데 인지 주입은 유실" 또는 "digest 재주입" 불일치가 생길 수 있다. 문서 §12가 SESSION_RESUME_M4를 언급만 하고 이 정합을 다루지 않았다.

### P1 — 설계 결함/불일치

**P1-1. F3 `phase_role`의 근거 데이터가 코드에 없다.**
문서 §4.1/§4.2 예시는 F3=0.3("능동 페이즈 역할")으로 r≈0.55→T2를 계산하지만, 코드에는 assignee/proposer 개념이 없다(assembler_id뿐). 따라서 V1에서 F3는 사실상 **0이거나 assembler로 한정**된다. → V1에서 F3 제거(또는 "assembler=phase_role" 한정)로 수정하고, 가중합 예시를 재계산해야 한다.

**P1-2. `RelevancePolicy.score_message(receiver_id, message, *, phase)` 시그니처로는 F2/F4 계산 불가.**
F2는 `message["thread_id"]`로 스레드 `participants`를 조회해야 하고, F4는 최근 메시지 이력이 필요하다. 단일 `message`와 `phase`만 받는 API로는 계산할 수 없다. → `RelevanceContext`(server snapshot 또는 thread lookup + 최근 이력)를 주입하거나, policy 생성 시 server 참조를 넘겨야 한다.

**P1-3. `context_max_chars`/`engage_max_chars` 의미가 모호하다.**
- "주입 context chars"가 **새 [radio] 블록의 chars**인지 **전체 conversation**인지 불명확(§9 시나리오 B는 새 주입량처럼 읽힘).
- `engage_max_chars: 4000` 기본값이면, `enabled: true` 시 T2/T3도 오늘(unlimited)과 동작이 달라진다. compact와 이중 절삭 혼선 가능.
→ V1은 **skim만 truncate**, engage/intervene은 `None`(unlimited) 권장. "주입 블록 기준"임을 명시.

**P1-4. "멘션 = 최소 engage"가 임계값에만 의존한다.**
`mention_boost=0.5 ≥ tiers.engage=0.45`라 성립하지만, YAML override로 깨질 수 있다. → config 검증에서 `mention_boost >= tiers.engage` 불변식을 강제(또는 코드에서 멘션 감지 시 `r = max(r, engage)` 보장).

**P1-5. batch max의 전량 주입 낭비.**
멘션 1개 때문에 batch 전체(drained 전체)를 T2로 full 주입하면, 같은 배치의 무관 broadcast까지 full 주입된다. 문서 §4.3-3이 의도한 단순화지만, **V1 한계로 명시**하고 per-message injection을 V1.1/V2 항목으로 남겨야 한다.

### P2 — 구현 디테일/명확화

- **P2-1.** skim "최신 1메시지"는 **`seq` 기준**으로 명시 (drain FIFO 순서 ≠ 논리적 최신, 스레드 교차 시 어긋날 수 있음).
- **P2-2.** T0 "digest 큐"의 자료구조·수명·포맷 미정의. 결정론적 1줄 포맷(예: `[skim] N msgs from {authors}`)을 정하고, checkpoint 포함 여부 결정.
- **P2-3.** `_update_phase_in_prompt()`가 T0 스킵 시에도 매 step 호출됨(loop.py:156). 무해하지만 "스킵 시에도 호출한다"는 의도를 문서에 남길 것.
- **P2-4.** `FakeModelBackend`는 script를 순서대로 소모하므로, T0로 `complete`가 스킵되면 **script 정렬이 어긋난다**. 결정적 attention 테스트는 "T0로 스킵될 step 수를 미리 반영한 script" 또는 "호출 횟수/주입 chars만 단언하는 시나리오"로 설계 필요.
- **P2-5.** F6 시간 감쇠(옵션)는 감산 항목이라 clip 하한 확인 필요(문서의 clip[0,1]로는 음수 미방어 — 명시적으로 floor).

---

## 5. 추가 할 일 (실행 항목)

> ID 뒤의 파일은 주요 변경 지점. V1a~V1f는 설계 문서 §8과 정렬하되, 위 문제를 반영해 보강했다.

### V1 핵심

| ID | 작업 | 파일 | 완료 기준 |
|----|------|------|-----------|
| **V1a** | `core/attention.py` 신규: `AttentionScores`, `BudgetDecision`, `RelevancePolicy`. **context(server snapshot/thread lookup) 주입, human 강제 engage, seq 기반 최신, 결정론적 digest 생성 포함.** F3는 V1에서 제거 또는 assembler 한정. | `core/attention.py` (신규) | F1/F2/F4만으로 batch max→tier 결정; human 메시지가 항상 T2+ |
| **V1b** | `attention:` YAML 검증/정규화 + 기본값 주입 + per-agent override merge + **`mention_boost ≥ tiers.engage` 불변식**. | `config.py` | 잘못된 경계/불변식 위반 시 `ConfigError` |
| **V1c** | `StepResult`에 `run_llm`(또는 `skipped_llm`) 추가. `step()` 분기: T0=drain 후 `complete` 스킵(단 `run_llm=False` 반환), T1=skim radio 블록, T2/T3=기존. `format_radio_block`에 skim 모드(요약 1줄+최신 1, `skim_max_chars` 절삭). | `loop.py` | T0 후 inbox==0; skim 주입 ≤ `skim_max_chars` |
| **V1d** | `session.run_agent`에서 **스킵 step을 `continue`로** 처리하고 `total_steps` 미증가; `result.text is None→break`는 "실제 완료"에만 적용. floor 배선: near_gate(`gate_for+not is_open`), P1+READY 미제출(`not has_ready`), assembler/per-agent floor를 step 직전 주입. | `session.py` | P0-1 해소; 게이트 창 T0 금지; nudge 경로 유지 |
| **V1e** | 테스트 `tests/test_attention_budget.py` + 회귀(`enabled:false` 기존 스위트 동행). 결정적 fixture는 `FakeModelBackend.calls/call_count` 활용. | `tests/` | §7 시나리오 전부 단언 |
| **V1f** | 갭 문서·벤치 노트: `IMPLEMENTATION_GAP_CONSOLIDATED.md`에 한 줄, `examples/benchmark/`에 attention 절감 시나리오(선택). | `docs/`, `examples/` | 절감 수치를 재현 가능한 스크립트로 기록 |

### 후속

| ID | 작업 | 비고 |
|----|------|------|
| **V1.1** | `ModelBackend.complete(..., max_tokens=, tool allowlist)` 시그니처 확장 + `BudgetDecision` 사용 | `backend/base.py` + 구현체 3종(fake/openai_compat/nous_portal_oauth) |
| **V2** | 송신 sparse(digest-only broadcast 제안), F5 role graph | 별 트랙 |
| **V2b** | `read_resource` soft truncate by tier | `tools.py` |
| **V3** | Wire `log.attention` / 학습형 score | 옵션 |

### 설계 문서 자체 보완 (문서 수정 TODO)

- [ ] §4.1/§4.2: F3 제거(또는 assembler 한정) + 가중합 예시 재계산.
- [ ] §4.1: human 메시지 예외 규정 추가.
- [ ] §5/§6: `context_max_chars`를 "주입 블록 기준"으로 명시, engage/intervene 기본 `None`으로 조정.
- [ ] §7: `RelevancePolicy` 시그니처에 context 인자 반영.
- [ ] §9/§10: P0-1(스킵≠종료)·P0-3(checkpoint 정합) 위험·완화 추가.
- [ ] §11 결정 로그에 아래 §9 결정 항목 추가.

---

## 6. 구현 순서 / PR 구성 제안

1. **PR 1 (설계 수정):** 이 문서의 "설계 문서 보완 TODO"를 `AGENT_RELEVANCE_BUDGET_DESIGN.md`에 반영. 코드 없이 설계 확정.
2. **PR 2 (V1c+V1d, 동일 PR 필수):** `StepResult.run_llm` + `step()` T0 분기 + `session` 스킵 처리/floor 배선. **floor 없이 T0만 켜면 합의/READY 회귀**하므로 문서 §8의 "V1c와 V1d는 같은 PR" 권장을 그대로 따른다.
3. **PR 3 (V1a+V1b):** `core/attention.py` + `config.py` 검증. (2와 3은 순서 바꿔도 되나, 3이 2의 정책 공급자이므로 2를 먼저 stubbing하거나 함께.)
4. **PR 4 (V1e+V1f):** 테스트·벤치·문서.

> 권장 최소 검증: 4에이전트 P3 broadcast 시나리오에서 `enabled:true` 시 `backend.call_count` 감소 + near_gate에서 T0 금지 + P1 READY nudge 유지.

---

## 7. 테스트 계획 보강

설계 문서 §9의 8개 항목에 아래를 추가/보강:

| # | 시나리오 | 단언 |
|---|----------|------|
| 1 | `enabled:false` 회귀 | 기존 `test_phases`/p1~p5 데모/`test_regression` 통과 |
| 2 | 멘션 vs 비멘션 | 멘션 수신자만 T2+, 비멘션은 T0/T1 |
| 3 | near_gate floor | T0 금지, 전원 최소 skim |
| 4 | P1+READY 미제출 | T0 금지, `_maybe_nudge_ready` 무력화 없음 |
| 5 | assembler floor | P5에서 assembler engage 이상 |
| 6 | T0 drain 불변식 | T0 틱 후 `inbox_size==0`, park↔wake 스핀 없음 |
| 7 | batch max | 멘션 1+broadcast 다수 → tier ≥ engage |
| 8 | 토큰/step 비교 | `FakeModelBackend.call_count` 감소 + 주입 chars ≤ 한도 |
| **9 (신규)** | **human 메시지** | `author=human` 메시지가 T0로 무시되지 않음 (항상 T2+) |
| **10 (신규)** | **스킵≠종료** | T0 스킵 후에도 에이전트 태스크가 종료되지 않고, 이후 멘션 수신 시 재참여 |
| **11 (신규)** | **checkpoint 재개** | T0 drain 직후 checkpoint→resume 시 인지 소실/이중 주입 없음 |

> `FakeModelBackend`는 script를 순서 소모하므로, 시나리오 8/10은 **호출 횟수·주입 chars 단언 중심**으로 설계(스킵 반영된 script 또는 빈 completion 허용).

---

## 8. 열린 질문 (결정 필요)

1. **F3 처리:** V1에서 F3(phase_role)를 (a) 완전 제거, (b) assembler만으로 한정, (c) 최소 assignee 매핑 신설 — 어느 쪽?
2. **engage/intervene context cap:** V1에서 T2/T3를 unlimited로 둘지(`None`), 아니면 `engage_max_chars`를 실제 적용할지.
3. **T0 스킵 후 idle 에이전트:** 열린 게이트 구간에서 무관 broadcast만 받은 에이전트를 (a) 대기 유지(refresh tick 필요), (b) 종료 허용 — 어느 쪽이 기대 동작인지.
4. **human 메시지 floor 값:** 강제 T2(engage)로 충분한지, T3(intervene)까지 올릴지.
5. **digest 큐 checkpoint 범위:** T0 digest 큐를 checkpoint에 저장할지(정합 우선) vs 재개 시 재구성/포기할지(단순 우선).
6. **per-agent override 시점:** V1b 포함(문서 §6) vs V1은 assembler floor만(문서 §4.3-4의 "둘 중 하나") — 확정 필요.

---

## 9. 결정 로그 제안 (설계 문서 §11에 추가)

| 결정 | 대안 | 이유 |
|------|------|------|
| T0 스킵은 `run_llm=False` + `continue` (종료 신호 분리) | `text=None` 재사용 | `result.text is None→break`와 충돌 방지 |
| human 메시지는 휴리스틱과 무관하게 강제 engage+ | score 병합 | 사용자 입력 유실 방지 |
| V1에서 F3 제거(assembler floor로 대체) | assignee 매핑 신설 | 코드에 근거 데이터 없음 |
| `context_max_chars`는 "새 주입 블록" 기준, engage/intervene 기본 unlimited | 전체 conversation 기준 | compact와 이중 절삭 방지 |
| `mention_boost ≥ tiers.engage`를 config 불변식으로 강제 | 코드 분기 | "멘션=최소 engage" 보장 |
| attention 상태(digest 큐)는 checkpoint에 포함 | 재구성/포기 | 재개 시 인지 소실 방지 |
