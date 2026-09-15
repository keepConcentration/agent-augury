# External binding (A5) — 플랫폼 ↔ Core 매핑 영속

> **Status:** **A5 v1 landed** (bindings.json + bridge hydrate + Discord lookup)  
> **Date:** 2026-09-15  
> **Parent:** `MULTI_FRONT_DESIGN.md` §3.3, `IMPLEMENTATION_GAP_CONSOLIDATED.md` A5  
> **Code:** `core/external_binding.py`, `core/checkpoint.py`, `core/session.py`, `gateway/bridge.py`, `channels/discord/inbound.py`

---

## 0. 한 줄

Discord/Slack **채널 좌표 → `thread_id`**, **진행 중 `ask_user` → `PendingQuestion` 큐**를  
세션 디렉터리 `bindings.json`에 저장해 **프로세스 재기동·M4 resume** 후 Bridge에 복원한다.

---

## 1. M4 checkpoint와 구분

| | M4 checkpoint | A5 bindings |
|--|---------------|-------------|
| SSOT | conversation, protocol, inbox, **tool approvals** | **채팅 UI 좌표**, bridge HITL 큐 |
| 파일 | meta, conversations, … | `bindings.json` |
| Core server | sqlite 메시지 | 변경 없음 |

---

## 2. `bindings.json` (schema v1)

```json
{
  "schema_version": 1,
  "recent_thread": "thr-…",
  "platform_threads": {
    "discord:ch:123456789:th:": "thr-human-…"
  },
  "pending_questions": [
    {
      "question_id": "…",
      "thread_id": "…",
      "agent_id": "a1",
      "question": "…",
      "options": []
    }
  ]
}
```

**Platform ref key:** `{surface}:ch:{channel_id}:th:{thread_ts_or_empty}`  
(`source` from Wire `human.*` — Discord inbound sets `surface`, `channel`, optional `thread`.)

---

## 3. 런타임 흐름

1. **기록:** `Session.human_send(..., source=…)` 성공 시 `platform_threads[key]=resolved_thread`.  
2. **Bridge:** `human.question` / `thread.created` 시 RAM (`_pending`, `_recent_thread`) — 기존과 동일.  
3. **Flush:** `flush_checkpoint_sync` 직전 `capture_from_bridge` → `bindings.json` atomic write.  
4. **Resume:** bootstrap `load_bindings` → `_setup`에서 `apply_to_bridge` (승인 재발행 전).  
5. **Discord inbound:** pending 없을 때 `lookup_platform_thread(source)` → `human.send`에 `thread_id` 첨부.

---

## 4. 비목표 (v1)

- Slack inbound / Block Kit 상관 id  
- 다중 human principal (A9)  
- TTL·LRU eviction (키 무한 증가 가능 — 세션 삭제 시 디렉터리와 함께 제거)  
- checkpoint 비활성 세션에 bindings (enabled=false면 파일 없음)

---

## 5. 테스트

- `tests/test_external_binding.py` — key, roundtrip, resume bridge pending, inbound lookup
