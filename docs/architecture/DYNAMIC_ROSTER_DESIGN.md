# Dynamic roster — 작업 크기에 맞춰 활성 에이전트 수를 정한다

> **Status:** 구현 완료 (v1 R0+R1), 리뷰 후속 수정 반영
> **Date:** 2026-09-18
> **Rev:** v1.4 — 구현 리뷰 반영: `run()` 종료 시 roster 콜백 해제(후속 턴 중복 spawn), 강등 중 전송에 `not_a_participant` 안내, pool 밖 에이전트 비활성 명시.
> **Rev:** v1.3 — 3차 리뷰 반영. inbox 는 에이전트당 1개이므로 drain 을 **페이즈 스레드 유래로 한정**, spawn SSOT 단일화(`Session.roster` 제거), `ensure_human_thread` 를 pool 기준으로
> **구현됨:** `ConsensusGate.set_participants` + seq 인자, `MessageServer.current_seq` / `set_thread_participants` / `drop_inbox_from`, `CollaborationProtocol.pool`·`set_roster`·R1 `_commit_p2_draft`·강등 `is_agent_done`·`begin_round` R0, Session roster spawn + config `protocol.roster`, `tests/test_dynamic_roster.py`
> **Code (현재):** `core/session.py` `run()` / `from_config`, `core/protocol/collaboration.py`, `core/protocol/approval.py` `ConsensusGate`, `core/protocol/assignments.py`, `core/server.py`, `core/agent/loop.py` `_not_on_thread_denied`
> **Tests:** `tests/test_dynamic_roster.py` (20)
> **관련:** `PHASE_MACHINE_ROUTING_AND_CHECKING_DESIGN.md` (R1 `SPLIT: none`), `PROTOCOL_GATE_WAIT_PARK_DESIGN.md` (park/D12), `FOLLOWUP_TURN_PROTOCOL_DESIGN.md` (`begin_round`), `AGENT_RELEVANCE_BUDGET_DESIGN.md`

---

## 0. Problem

지금 세션의 **참가자 수는 설정 시점에 고정**된다. `agents:` 에 N명을 적으면 작업 크기와 무관하게 매 턴 N명 전원이 돈다.

코드 근거:

| 위치 | 현재 동작 | 100명 설정 시 결과 |
|------|-----------|--------------------|
| `session.py` spawn 블록 | `tasks = [create_task(run_agent(a)) for a in self.agents]` | 100개 asyncio task, 100개 모델 호출 루프 |
| `session.py` `from_config` | `participants=protocol_spec.get("participants", participant_ids)` | 프로토콜 참가자 = 전원 (설정 고정) |
| `approval.py` `bind_to_thread` | `self.participants = list(thread["participants"])` (bind 시 1회) | 게이트가 **100표**를 기다림 |
| `collaboration.py` `all_ready` | `set(self.participants) <= self._ready_states` | P1 진입에 100개 `READY:` 필요 |
| `collaboration.py` `_commit_p2_draft` | `parse_assignments(...)` → `self._assignments` | 명단을 **뽑아만 두고 정족수엔 반영 안 함** |

마지막 줄이 핵심 갭이다. P2 는 이미 "누가 무엇을 한다"를 `ASSIGN` 줄로 선언하고 런타임이 파싱까지 하는데, 그 결과가 정족수에 반영되지 않는다. 3명이 할 일을 결정해도 P3·P4 게이트는 여전히 전원의 승인을 기다린다.

두 번째 갭: **명단에서 빠진 에이전트는 park 도 못 한다.** `collaboration.py` 의 `is_agent_done()`은 READY 집합 / 게이트 approvals 로만 판정하므로 비참가자는 영영 `protocol_done == False` → `session.py` 의 C2 무모델 park 경로에 못 들어가고 계속 `step()` 한다.

세 번째 갭: **메시지 전달은 스레드 참가자 기준**이다(`server.py` `send_message` 전달 규칙). 스레드에 남겨둔 에이전트는 읽는 task 가 없어도 inbox 가 쌓이고, D12 의 `all(inbox_size == 0 for a in self.agents)`(`session.py` D12 검사) 가 영영 False 가 되어 교착이 감지되지 않는다.

즉 **참가자 리스트만 줄이면 토큰은 그대로 나가고 교착 탐지만 망가진다.**

---

## 1. 근거 — 왜 "많을수록 좋다"가 아닌가

| 출처 | 수치 | 함의 |
|------|------|------|
| Google, *Towards a Science of Scaling Agent Systems* (arXiv:2512.08296) | 병렬 가능 작업 **+80.9%**, 순차 작업 **−39~70%** | 작업 성격이 전부. 수가 아니다 |
| 〃 | 오류 증폭: 독립 17.2x / 중앙조율 4.4x | 무분별한 팬아웃은 오류를 키운다 |
| Anthropic, *Multi-agent research system* | 단순 조회 1명 / 비교 2~4명 / 복잡 조사 10+ | 실무 기준표 |
| 〃 | 싱글 에이전트 4x, 멀티 15x 토큰 | 작은 작업에 멀티 = 15배 내고 성능 하락 |
| MAST (arXiv:2503.13657) | 실패 14유형, 대부분 명세·조율 문제 | 혼자면 존재하지 않는 실패 종류 |
| MacNet (arXiv:2406.07155) | 로지스틱(S자) 성장, 유한한 스케일 지평 | 증원 이득은 금방 평평해진다 |
| MAD 재평가 (arXiv:2502.08788) | 5개 토론 방법 중 CoT 대비 승률 20% 초과 없음 | 토론형 팬아웃은 기본값이 될 수 없다 |

결론 두 줄:

1. **기본은 적게.** 순차·단일 맥락 작업(디버깅, 리팩터링, 단계 추론)에서 멀티는 확실히 손해다.
2. **사전 예측하지 말고 선언받아라.** 분해 가능성은 탐색 전에 알 수 없다. 이 repo 는 이미 P1(탐색) → P2(분할 선언) 구조라, 예측 대신 **선언 시점에 명단을 확정**하면 된다.

---

## 2. 핵심 결정

### 2.1 세 층위를 분리한다

| 층위 | 정의 | 쓰이는 곳 | 크기 |
|------|------|-----------|------|
| **pool** | `protocol.participants` 가 있으면 그것, 없으면 `agents:` 전원 | **ASSIGN/SUBMITTER 파싱의 known set** | 고정 (예: 100) |
| **roster** | 현재 활성 명단 | 게이트 정족수 · READY 정족수 · 스레드 참가자 · spawn 대상 | 가변 (예: 2) |
| **bench** | `pool − roster` | 없음 (task 없음, 스레드 없음, 정족수 없음) | 나머지 |

- **pool 정의**: 기존 `protocol.participants`(예: `examples/p1_to_p5_protocol.yaml:11`, `attention_budget_demo.yaml:16`)는 "프로토콜에 참여할 수 있는 전체"라는 뜻을 그대로 유지한다. 없으면 `agents:` 전원. `roster.start` 는 **이 pool 리스트의 앞에서** 뽑는다.
- **파싱은 pool 기준, 정족수는 roster 기준.**
- **pool 밖 에이전트는 완전히 비활성이다.** `agents:` 에 있지만 `protocol.participants` 에 없는 에이전트는 task 도 안 뜨고(`session.py` spawn), 사람 스레드에도 안 들어간다(§2.2). 이전에는 전원 spawn 이었으므로 **동작 변경**이다 — 곁다리 작업용으로 pool 밖 에이전트를 두던 설정은 pool 에 넣어야 한다.

이게 v1.0 설계의 치명적 구멍이었다. `parse_assignments(content, self.participants)`(`collaboration.py` `_commit_p2_draft`)는 known set 에 없는 id 를 **버린다**. roster 를 2명으로 줄여둔 상태에서 P2 가 `ASSIGN a7` 을 쓰면 a7 은 `set_roster` 가 호출되기도 전에 파싱 단계에서 사라진다 → 벤치 기동이 구조적으로 불가능.

### 2.2 roster 는 한 곳이 아니라 세 곳을 동시에 정의한다

> **roster = 게이트 참가자 = 페이즈 스레드 참가자**

하나의 명단이 정족수·전달·spawn 을 모두 결정한다. 이렇게 묶으면 따로 풀어야 할 문제 하나가 **저절로 사라진다**:

- **체크포인트** — `ConsensusGate.restore_state`(`approval.py` `ConsensusGate.restore_state`)는 재개 시 participants 를 **스레드에서 다시 읽는다**. 스레드 참가자가 곧 roster 면 이 코드가 그대로 정답이다. 게이트 snapshot 에 필드를 추가할 필요 없음.

**D12 는 "무수정"이 아니다** — never-spawned 벤치는 스레드 참가자가 아니라 전달 대상에서 빠지므로(`server.py` broadcast 대상) inbox 가 비어 있지만, **강등된 에이전트는 탈락 직전에 받은 메시지가 inbox 에 남아 있다.** §2.3 에서 drain 으로 처리한다.

예외 하나: **사람 스레드(`HUMAN_CHAT_THREAD_NAME`)는 pool 전체를 유지한다.** 사람 메시지는 interact surface 가 붙어 있을 때만 흐르고, 그때 D12 는 애초에 비활성(`not self.has_interact_surface()`)이다. 사용자가 벤치 에이전트를 직접 부를 길을 남겨두는 값이 더 크다. 이 전제가 깨지면(interact 없이 사람 스레드로 메시지가 들어오는 경로가 생기면) 사람 스레드도 roster 로 좁힌다.

현재 `session.py` 의 `ensure_human_thread` 는 `self.agents` 전원을 넣는다. pool 이 `protocol.participants` 로 좁혀진 설정에서는 **pool 기준으로 맞춘다**(pool ⊆ agents).

### 2.3 "안 돌린다"의 두 가지 경로

| 상태 | 정의 | 처리 | inbox | 비용 |
|------|------|------|-------|------|
| **never-spawned bench** | R0 에서 한 번도 뽑히지 않음 | asyncio task 자체를 만들지 않음 | 애초에 전달 안 됨 | 0 |
| **demoted** | R0 에서 돌다가 R1 에서 탈락 | `is_agent_done → True` → 무모델 park | **페이즈 스레드 유래만 drain** | 0 (현재 step 종료 후) |

park 조건은 `agent.protocol_done and inbox_size == 0`(`session.py` C2 무모델 park) **둘 다**다. `is_agent_done` 만 고쳐서는 잔여 inbox 가 있는 강등 에이전트가 park 하지 못하고 모델을 호출한다. 그래서 강등 시 **inbox 를 비운다.**

버려도 되는 근거: inbox 는 `asyncio.Queue[str]` — 메시지 **id 큐**일 뿐이고(`server.py` `_inboxes` / 전달 시 push), 본문은 `_messages`(SSOT)에 남는다. 즉 drain 은 "안 읽음 표시"만 지우는 것이고, 재승격 시 스레드를 읽으면 전부 그대로 있다. DESIGN.md §3.3 "SSOT 는 내부 서버, 나머지는 뷰" 와 같은 논리.

**단, inbox 는 스레드별이 아니라 에이전트당 큐 하나다**(`server.py` `_inboxes`). 통째로 비우면 같은 큐에 있던 **사람 스레드 미읽음까지 사라진다** — §2.2 가 "사용자는 벤치를 부를 수 있다"고 남겨둔 경로가 R1 타이밍의 poke 하나로 무효가 된다. 본문이 `_messages` 에 남아도 재전달이 없으면 그 poke 로는 깨어나지 않는다. 따라서 drain 은 **페이즈 스레드에서 온 id 만** 버린다(§4.2 `drop_inbox_from`).

> 검토했다 버린 대안: **B. drain-only park**(inbox 있어도 모델 없이 비우기) — session 루프에 분기가 하나 더 생긴다. **C. D12 검사를 roster 한정** — 강등 에이전트의 모델 호출 자체는 그대로 남는다. A 만이 두 문제를 한 번에 없앤다.

강등 에이전트가 park 에 들어가는 조건 `_is_gate_waiting()`(`session.py` `_is_gate_waiting`)은 P1 에서 항상 True, 그 외엔 현재 페이즈 게이트가 닫혀 있으면 True 다. 게이트가 열려 깨어나도 다시 `is_agent_done → True` + 빈 inbox 로 park 한다. 모델 호출 0.

**task cancel 은 쓰지 않는다.** step 도중 취소는 메시지·체크포인트 정합을 흔들고, 재승격 시 재spawn 이 필요하다. 이미 있는 park 경로를 쓰는 게 한 줄이고, parked 로 집계되니 D12 의 `live` 계산도 그대로 맞는다.

### 2.4 버린 대안

- **사전 라우터 모델** (턴 시작에 "몇 명?" 1회 질의) — 분해 가능성을 탐색 전에 물어보는 것이라 자주 틀리고, 호출이 하나 늘고, P2 가 같은 판단을 다시 한다. 중복.
- **프롬프트 길이·키워드 휴리스틱** — 길이와 분해 가능성의 상관이 약하다. 근거 없음.
- **강등 시 task cancel** / **inbox 보존** / **게이트 snapshot 필드 추가** — §2.2, §2.3.

---

## 3. 두 개의 레버 (v1)

v1 에서 roster 가 바뀌는 지점은 **정확히 두 번**이다.

```text
pool (protocol.participants 또는 agents: 전원 — 100명)
  │
  ├─ R0  세션 시작 / begin_round (후속 턴)
  │        set_roster(pool[:start])        # 기본 start=2
  │        roster 만 spawn, 페이즈 스레드도 roster 로 생성
  │        → P1 정족수 = 2 (100 아님)
  │
  └─ R1  P2 게이트 open 시 (_commit_p2_draft)
           파싱 known set = pool
           SPLIT: none        → roster = [submitter]          (혼자 P5)
           ASSIGN a1,a3,a7    → roster = {a1,a3,a7} ∪ {submitter}
                                a7(벤치) 기동 · a2(탈락) 강등 + inbox drain
```

- **R0** 는 탐색 인원이다. 연구상 탐색은 병렬 이득 구간이므로 1이 아니라 **2~3 권장**. `pool[:start]` 이므로 **pool 의 나열 순서가 곧 탐색 우선순위**다.
- **R0 는 spawn 제한만이 아니라 `set_roster` 호출이다.** 이게 빠지면 스레드가 `protocol.participants` 전체로 생성되고(`session.py` 게이트 스레드 생성) P1 은 여전히 100개 `READY:` 를 기다린다.
- **후속 턴**: `begin_round()`(`collaboration.py` `begin_round`)는 `_ready_states` / `_assignments` 는 지우지만 `participants` 는 되돌리지 않는다. 그대로 두면 R1 로 1명이 된 roster 가 다음 턴 탐색까지 1명으로 굳는다 → **`begin_round` 에서 R0 로 리셋한다.** 턴마다 크기를 다시 정하는 것이 이 기능의 취지.
- **R1** 이 실질적 결정점. 파싱은 `assignments.py` 에 이미 있고 커밋 지점도 `_commit_p2_draft`(`collaboration.py` `_commit_p2_draft`)로 이미 존재한다. **반영만 추가한다.**
- `mode: light` 는 P2 가 없다 → **R0 만 적용, roster 는 턴 내내 고정.**
- 실행 중 증원(`RECRUIT:`)은 **v2**.

---

## 4. 컴포넌트별 변경

> **모든 경로는 sync 다.** `collaboration.py` 의 `_handle_gate_open` → `_commit_p2_draft` 는 게이트 구독 콜백이라 sync 이고, 이걸 async 로 바꾸면 `on_open` 체인 전체가 오염된다. 상태 변경은 메모리에서 sync 로 끝내고, DB 영속화만 `asyncio.create_task` 로 뒤에 붙인다. `register_agent` 는 이미 sync(`server.py`, "no IO involved")라 신규 참가자 등록도 sync 로 가능하다.

### 4.1 `ConsensusGate.set_participants()` (`protocol/approval.py`)

게이트 참가자는 `bind_to_thread()` 에서 1회 확정된다. 명단 교체 API 가 필요하다.

현 `_maybe_open(self, message)` / `_open_gate(self, message)` 는 `message["seq"]` 만 쓴다. 합성 dict 를 넘기는 대신 **seq 를 인자로 받도록 바꾼다** — 호출부 2곳에서 `message["seq"]` 를 넘기면 끝.

```python
def _maybe_open(self, seq: int) -> None: ...
def _open_gate(self, seq: int) -> None:
    self.opened_at_seq = seq
    ...

def set_participants(self, participant_ids: list[str], *, seq: int) -> None:
    """명단 교체. 빠진 사람의 표는 버리고, 남은 사람만으로 만장일치를 재평가한다."""
    self.participants = list(participant_ids)
    self.approvals &= set(self.participants)   # 유령 표 제거
    if not self.is_open:
        self._maybe_open(seq)                  # 남은 전원이 이미 찬성했으면 즉시 open
```

`seq` 는 현재 메시지 순번(`server.py` seq 부여 = `len(self._messages)`). 재평가가 없으면 축소 직후 "이미 다 찬성했는데 안 열리는" 상태가 생긴다.

### 4.2 `MessageServer` 확장 (`core/server.py`)

`create_thread` 는 기존 스레드에 참가자를 **합집합으로만** 더한다. 축소 경로가 없다.

```python
def current_seq(self) -> int:
    """다음 메시지에 부여될 seq (= len(self._messages)). §4.1 용."""

def set_thread_participants(self, thread_id: str, agent_ids: list[str]) -> None:
    """참가자 교체(증감 모두). 메모리 갱신은 sync, 영속화는 create_task."""
    # 신규 id 는 register_agent() 로 inbox 먼저 (기존 create_thread 순서 :243)
    # 제거되는 id 는 drop_inbox_from(agent_id, [thread_id]) — §2.3
    # 영속화: 실행 중 루프가 있으면 create_task, 없으면(단위 테스트) 메모리만

def drop_inbox_from(self, agent_id: str, thread_ids: set[str]) -> None:
    """해당 스레드에서 온 미읽음만 버리고 나머지는 순서대로 되돌린다.

    inbox 는 에이전트당 큐 하나라(:71) 통째로 비우면 사람 스레드 poke 까지
    날아간다(§2.3). id → _messages[id]["thread_id"] 로 걸러낸다.
    """
    q = self._inboxes[agent_id]
    kept = []
    while True:
        try:
            mid = q.get_nowait()
        except asyncio.QueueEmpty:
            break
        msg = self._message_index.get(mid)
        if msg is None or msg.get("thread_id") not in thread_ids:
            kept.append(mid)      # 모르는 id 는 버리지 말고 되돌린다
    for mid in kept:
        q.put_nowait(mid)
```

### 4.3 `CollaborationProtocol.set_roster()` (`protocol/collaboration.py`)

```python
def set_roster(self, participant_ids: list[str]) -> None:
    # 불변식 1·2 가 사는 자리: pool 밖은 버리고, 남는 게 없으면 no-op.
    cleaned = [p for p in participant_ids if p in set(self.pool)]
    if not cleaned:
        return
    roster = list(dict.fromkeys(cleaned))   # 중복 제거, 순서 유지
    seq = self._server.current_seq()
    self.participants = roster
    self._ready_states &= set(roster)       # P1 정족수도 축소
    for gate in self._gates.values():
        if gate is not None:
            gate.set_participants(roster, seq=seq)
            if gate.thread_id:
                # 탈락자는 여기서 스레드에서 빠지고, 그 스레드 유래 미읽음만 버려진다
                self._server.set_thread_participants(gate.thread_id, roster)
    if self._on_roster_change:              # 세션에 spawn 을 맡긴다
        self._on_roster_change(set(roster))
```

호출 지점 셋: **R0**(세션 시작, 스레드 생성 직후) / **R1**(`_commit_p2_draft` 끝) / **`begin_round()` 끝**(§3).

### 4.4 `_commit_p2_draft` 의 R1 규칙 (`protocol/collaboration.py`)

```python
# 파싱 known set = pool (roster 아님 — §2.1)
self._assignments = parse_assignments(content, self.pool) or {}
self.submitter_id = parse_submitter(content, self.pool)
self.split_none = parse_split(content)

# R1 축소 — 하나라도 안 맞으면 no-op (현행 유지)
if self.submitter_id is None:
    return                                   # 불변식 3: 제출자 없으면 축소하지 않는다
if self._assignments:                        # E2: ASSIGN > SPLIT: none
    new_roster = [self.submitter_id] + [
        a for a in self._assignments if a != self.submitter_id
    ]
elif self.split_none:
    new_roster = [self.submitter_id]
else:
    return                                   # 선언 없음 → 현 roster 유지
self.set_roster(new_roster[:self.roster_max])          # max 절삭
```

- **절삭 순서**: submitter 가 항상 첫 번째, 그다음 `ASSIGN` 이 쓰인 순서. 뒤에서 자른다.
- `self.pool` 은 `CollaborationProtocol.__init__` 에 새로 받는다(현 `participants` 인자와 별개, 기본값 = participants).

### 4.5 강등 처리 (`protocol/collaboration.py` `is_agent_done`)

```python
def is_agent_done(self, agent_id: str) -> bool:
    if agent_id not in self.participants:
        return True          # roster 밖 = 이 페이즈에 할 일 없음 → 무모델 park
    ...                      # 이하 현행
```

한 줄. inbox 쪽 절반은 §4.2 `drop_inbox_from` 이 맡는다 — **둘 다 있어야** park 조건(`protocol_done and inbox_size == 0`)이 성립한다.

### 4.6 spawn / 지연 기동 (`core/session.py`)

`run_agent` 은 `run()` 내부 클로저다(`run_agent`). 나중에 기동하려면 spawn 함수를 세션에 보관한다.

```python
# run() 안, tasks 생성부 교체
def _spawn(agent):                       # 이미 돌고 있으면 no-op
    ...

def _spawn_roster(agent_ids):            # §4.3 콜백이자 최초 기동
    for agent_id in agent_ids:
        agent = agent_by_id.get(agent_id)
        if agent is not None:
            _spawn(agent)

self._agent_tasks = []
self.protocol.on_roster_change(_spawn_roster)
_spawn_roster(self.protocol.participants)   # 순서 있는 리스트 → 기동 순서 결정적

# ... 그리고 턴이 끝나면 반드시:
finally:
    self._agent_tasks = []
    self.protocol.on_roster_change(None)     # v1.4 — 아래 설명
```

**콜백은 `run()` 이 끝날 때 반드시 뗀다.** 콜백은 그 run 의 `_spawn` 을 클로저로 물고 있는데, 후속 턴은 `begin_round()` → `set_roster()` 를 **다음 run 이 재등록하기 전에** 부른다. 떼지 않으면 지난 run 의 `_spawn` 이 되살아나 `run()` 밖에서 task 를 띄운다 — 추적도 await 도 취소도 안 되는 중복 루프가 에이전트마다 하나씩 생긴다. (구현 중 실제로 발생, `test_followup_turn_does_not_double_spawn`)

기존 `tasks` 지역 변수를 쓰는 대기/취소 코드는 `self._agent_tasks` 를 보도록 바꾼다 — **도중에 늘어나는 리스트**이므로 `asyncio.gather(*tasks)` 한 방으로는 부족하고, 완료 대기는 "남은 task 가 없을 때까지" 루프여야 한다.

**spawn 의 SSOT 는 `protocol.participants` 하나다.** `Session.roster` 같은 별도 필드를 두지 않는다 — 두 곳에 같은 명단을 들고 있으면 체크포인트 왕복에서 어긋나고, 어긋난 쪽이 정답인지 판정할 방법이 없다. 세션은 "누가 돌고 있나"를 `self._agent_tasks` 로만 알면 되고, "누가 돌아야 하나"는 매번 프로토콜에 묻는다.

`ensure_human_thread`(:1065)의 참가자도 `self.agents` → **pool** 로 바꾼다(§2.2).

### 4.7 config (`config.py`)

```yaml
protocol:
  mode: full
  participants: [...]   # 기존 키 = pool. 없으면 agents: 전원
  roster:
    start: 2      # P1 탐색 인원. pool 나열 순서대로 앞에서 뽑는다. -1 = 전원
    max: 8        # 한 페이즈 최대 활성 수. ASSIGN 이 더 많이 지목하면 뒤에서 자른다
```

기본값 `start = min(2, len(pool))`, `max = len(pool)`. 설정을 안 쓰면 **현재와 동일 동작이 아니다**(의도된 기본값 변경). 기존 동작이 필요하면 `start: -1`.

**기본값이 두 겹이다** — 이 `start=2` 는 `load_config` 의 `normalize_protocol_roster` 가 채운다. `CollaborationProtocol` 생성자를 직접 부르는 코드(테스트·임베딩)의 기본값은 `roster_start=-1`(전원)이라 기존 동작 그대로다. YAML 을 거치는 세션만 좁아진다.

---

## 5. 상태와 체크포인트

**새 체크포인트 필드는 없다.**

- 게이트 정족수 — `restore_state` 가 스레드에서 읽고(`approval.py` `ConsensusGate.restore_state`), 스레드 참가자는 `set_thread_participants` 가 영속화했다(§2.2).
- roster — `protocol.participants` 가 이미 직렬화된다(`collaboration.py` `snapshot` / `restore`). 재개 시 세션은 여기 있는 id 만 spawn 한다(§4.6, SSOT 단일).
- `pool` 은 config 에서 다시 읽으므로 저장 불필요.

---

## 6. 불변식 (테스트로 고정할 것)

1. `roster ⊆ pool` — 항상. 파싱 known set 은 `pool`, 정족수는 `roster`.
2. `len(roster) >= 1` — 빈 명단 금지. 축소 조건이 안 맞으면 **no-op**(현 roster 유지).
3. `submitter ∈ roster` — `SUBMITTER` 를 못 읽으면 축소 자체를 하지 않는다 (커밋 6736ea3 "비제출자에게 초안을 시키지 않는다" 와 같은 부류의 버그).
4. `gate.approvals ⊆ gate.participants` — 축소 후.
5. `roster == gate.participants == 페이즈 스레드 참가자` — 사람 스레드만 예외(§2.2).
6. **never-spawned bench**: asyncio task 없음 · 페이즈 스레드 참가자 아님 · 전달 대상 아님 → **페이즈 스레드 유래 inbox 0**.
7. **demoted**: 강등 직후 `is_agent_done == True` **이고 페이즈 스레드 유래 inbox 0** → 모델 호출 0, parked 로 집계. 사람 스레드 미읽음은 **남는다**(§2.3).
8. 6 + 7 ⇒ interact surface 가 없는 세션(= D12 가 켜지는 유일한 경우)에서는 사람 스레드 메시지 자체가 없으므로 `all(inbox_size == 0 for a in self.agents)`(`session.py` D12 검사)가 성립 — D12 코드 자체는 무수정.
9. 축소 직후 남은 전원이 이미 승인 상태면 게이트는 **즉시** open.
10. `begin_round()` 후 `roster == pool[:start]`.

---

## 7. 로드맵 / 통과 기준

**v1 (R0 + R1)**

- [x] `_maybe_open` / `_open_gate` seq 인자화 + 호출부 2곳 — 기존 게이트 테스트 무회귀
- [x] `ConsensusGate.set_participants` + 재평가 — `test_shrink_opens_gate_when_remaining_all_approved`
- [x] `MessageServer.set_thread_participants` / `drop_inbox_from` / `current_seq` — `test_thread_participants_shrink_and_persist`, `test_drop_inbox_keeps_human_messages`
- [x] `CollaborationProtocol.set_roster` — `test_set_roster_shrinks_ready_quorum`
- [x] **파싱 known set = pool** — `test_assign_can_name_bench_agent`
- [x] R0 — `test_start_k_limits_initial_roster`, `test_p1_quorum_is_start_not_pool`
- [x] R1 — `test_assign_lines_become_roster`, `test_split_none_roster_is_submitter_only`, `test_no_submitter_is_noop`, `test_max_truncates_keeping_submitter`
- [x] 벤치 기동 — `test_assigned_bench_agent_is_spawned`
- [x] 강등 — `test_demoted_agent_parks_without_model_call` (**탈락 직전 브로드캐스트가 inbox 에 있는 상태에서** 시작할 것)
- [x] D12 — `test_deadlock_still_detected_with_bench_and_demoted` (interact 없는 headless)
- [x] 사람 스레드 — `test_human_can_poke_demoted_agent` (강등 직전 poke 가 살아남아 깨우는지)
- [x] 후속 턴 — `test_begin_round_resets_roster_to_start`
- [x] 체크포인트 — `test_roster_survives_resume` (재개 후 게이트 정족수 == roster, 새 필드 없이)
- [x] 루프 없는 단위 테스트에서 `set_thread_participants` 가 메모리만으로 동작 — `test_set_participants_without_running_loop`
- [x] 회귀: 100명 풀 + `SPLIT: none` 에서 **벤치 모델 호출 0** (`test_large_pool_split_none_model_calls_bounded`)
- [x] 마이그레이션: 3인 이상 기존 예제에 명시적 `start` — `examples/attention_budget_demo.yaml`(4), `examples/p1_to_p5_protocol.yaml`(3). `multi_bot_demo.yaml` 은 `protocol:` 섹션이 없어 대상 아님

**리뷰 후속 (v1.4)**

- [x] 후속 턴 중복 spawn — `run()` finally 에서 `on_roster_change(None)`, `test_followup_turn_does_not_double_spawn`
- [x] 강등 중 전송 — `AgentLoop._not_on_thread_denied`, `test_off_thread_send_gets_clear_message`
- [x] 강등 테스트에 이빨 — 무한 스크립트 + 정착 후 재측정 (`test_demoted_agent_parks_without_model_call`)

**v2 (조건부)** — `RECRUIT:` 실행 중 증원. v1 운용 중 "명단이 모자라 실패한 턴"이 관측될 때만.

---

## 8. 리스크와 대가

| 리스크 | 완화 |
|--------|------|
| P2 가 명단을 너무 좁게 잡아 품질 하락 | `roster.start` ≥ 2 로 탐색은 복수 유지. v2 `RECRUIT:` 를 예비로 남김 |
| 강등 시 drain 으로 "안 읽은 지시"가 사라짐 | 본문은 `_messages`(SSOT)에 남고 재승격 시 스레드로 읽힌다. **inbox 는 큐일 뿐 기록이 아니다** |
| drain 범위를 잘못 잡아 사람 poke 유실 | `drop_inbox_from` 은 스레드 id 로 거른다(§4.2). 불변식 7 + `test_drop_inbox_keeps_human_messages` 로 고정 |
| 강등된 에이전트를 도중에 다시 부를 수 없음 | 사람 스레드는 pool 전체 유지(§2.2) → 사용자는 부를 수 있다. 에이전트끼리는 v2 |
| 사람 스레드 예외가 D12 를 깨는 경로 | 불변식 8 을 테스트로 고정. interact 없이 사람 스레드로 메시지가 들어오는 경로가 생기면 예외 철회 |
| 기본값 변경(`start=2`) | 릴리스 노트 + `start: -1` 탈출구 + §7 마이그레이션 항목 |
| roster 콜백이 `run()` 밖까지 살아남음 | **실제로 발생했다.** `begin_round()` 가 다음 run 의 등록보다 먼저 `set_roster()` 를 호출해 추적되지 않는 중복 루프를 띄웠다. `finally` 에서 해제 + 회귀 테스트 |
| `_agent_tasks` 가 도중에 증가 → 기존 gather 가정 붕괴 | §4.6 대기 루프 교체. 놓치면 기동한 에이전트를 기다리지 않고 턴이 끝난다 |
| `set_thread_participants` 의 persist 가 create_task → 재개 직전 크래시 시 유실 | 최악의 경우 재개 후 정족수가 한 페이즈 넓어짐(진행 불가 아님). 필요하면 flush 지점에서 await |
| 실행 루프 없는 단위 테스트에서 create_task 실패 | 루프 유무를 보고 없으면 메모리만 갱신(§4.2). 테스트로 고정 |

---

## 9. 비목표

- 작업 난이도를 추정하는 라우터 모델 — §2.4 에서 기각.
- 에이전트 자동 생성(풀에 없는 새 에이전트 스폰) — 풀은 사람이 정한다.
- 토폴로지 최적화(MacNet 류 DAG) — 본 설계는 **수**만 다룬다.
- 페이즈별 서로 다른 모델 배정 — 별건.
