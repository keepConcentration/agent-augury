# Protocol gate-wait idle park — implementation design

> **Status:** implemented (v1 poll wakeup)  
> **Date:** 2026-09-13  
> **Code:** `Session._is_gate_waiting` / `Session._wait_for_gate_wakeup` in `session.py`  
> **Tests:** `tests/test_gate_wait_park.py`

---

## 0. Problem

Today `Session._run_impl` / `run_agent` exits when:

```text
no tool_calls ∧ text is None ∧ inbox empty
```

So an agent waiting for teammates’ `READY:` / `APPROVE:` either:

1. narrates “대기하겠습니다” forever (`text` keeps the loop alive), or  
2. returns empty → **exits the turn** while the gate is still closed.

Desired UX (chat-like): **say nothing, wait, resume when something happens.**

---

## 1. Scope (narrow)

| In scope | Out of scope |
|----------|----------------|
| `session.protocol is not None` | No-`protocol` free multi-agent |
| Phase is **gate-waiting** (see §2) | Terminal `COMPLETED` / `REJECTED` |
| Idle = no `tool_calls` ∧ inbox empty (after step) | Changing Wire / Ink |
| Wake = inbox ∨ phase/gate change ∨ human interrupt/quit | “All agents idle ⇒ end turn” |
| | Hard wall-clock timeout as primary end condition |

---

## 2. When is an agent allowed to park?

Define **`gate_waiting(session) -> bool`**:

```text
protocol exists
∧ phase ∉ {COMPLETED, REJECTED}
∧ (
     phase == P1_EXPLORE
     ∨ (current phase has a bound ConsensusGate ∧ gate.opened_at_seq is None)
   )
```

Rationale:

- **P1:** no gate object yet; waiting is for everyone’s `READY:` (phase advance).  
- **P2–P5:** waiting is for unanimous `APPROVE:` on that phase’s gate thread.  
- Once the gate opens, `on_gate_open` advances phase → parkers wake via **phase change**.

Do **not** park when:

- no protocol  
- gate already open for this phase (work-share allowed / phase about to advance)  
- agent still has inbox messages (must step to drain `[radio]`)

---

## 3. Control flow (per agent)

After a successful `agent.step()` → `result`:

```text
has_pending = inbox_size(agent) > 0

if result.tool_calls:
    yield; continue          # normal work

if has_pending:
    yield; continue          # drain next step

if gate_waiting(session):
    # IDLE under gate wait — do not call the model again yet
    woke = await wait_gate_wakeup(agent)
    if woke:
        continue             # step again (likely new radio or new phase)
    else:
        break                # only interrupt / run cancelled
else:
    # legacy finish
    if result.text is None:
        break
    yield; continue
```

### UI

- Prefer **not** to suppress the last useful step.  
- Optional later: drop pure waiting narration from the output queue; **not required for v1** if park stops re-calls.  
- Empty completions while parked never hit the model again → no spam.

### Prompt (already partially landed)

Keep system/phase lines: *do not narrate waiting; empty completion when blocked on teammates.*  
Park is the **enforcement**; prompt is **soft**.

---

## 4. Wake API

### 4.1 `wait_gate_wakeup(agent) -> bool`

Returns `True` = step again; `False` = exit agent loop.

**Wake (True) when any of:**

1. `inbox_size(agent) > 0`  
2. `protocol.phase` ≠ phase snapshot at park entry  
3. Current phase gate transitions closed → open (if observable without phase change race)  
4. (Optional) `max_steps` budget exhausted → treat as False / break at loop top as today  

**Return False when:**

1. `session._interrupt` is set (Ctrl+C / quit path)  
2. Session is closing  

**Explicitly do NOT wake/end on:**

- “all agents are parked and all inboxes empty” → **forbidden** (caused early P2 end / test hangs in the rejected design)  
- fixed 120s “everyone done” timeout as turn terminator  

Optional **safety**: very long park (e.g. 30–60 min) may log a Wire `log` warning but should **not** auto-end the turn in v1; human `/quit` or interrupt remains the escape hatch. If a timeout is required later, make it config (`protocol.idle_timeout_s`) defaulting to off/infinite.

### 4.2 Efficient waiting (implementation note)

v1 may `asyncio.sleep(0.05)` poll (same as rejected prototype).  

v1.1+: `asyncio.Event` per session:

- `MessageServer` delivery to inbox → `session._agent_wake[agent_id].set()`  
- protocol `on_phase_change` / gate `on_open` → set all participant events  

Polling is acceptable for design approval; Event is the preferred follow-up for CPU.

---

## 5. Interaction with gates & threads

Unchanged and important:

- While gate closed, `send_message` to **non-gate** threads still returns `gate_closed`.  
- Park does not unblock that; it only stops useless LLM turns.  
- Progress to execution/review/submission threads still requires unanimous `APPROVE:` (prefix) on the gate thread.

---

## 6. Human / Ink turn model

Ink `session_stdio` already: after `run()` finishes → summary → wait for next `human.send`.

With gate-wait park:

- `run()` **stays open** while any agent is parked on a closed gate (others may still be working).  
- Turn ends when **every** agent loop exits — which under this design means: interrupt, or no longer `gate_waiting` and classic finish, or agents complete post-gate work with empty idle outside gate-wait.

Edge: one agent parked, others still tool-calling → fine.  
Edge: all four parked waiting for `APPROVE:` that never comes → turn stays open until human interrupt/quit (desired vs today’s silent abort).

---

## 7. Files to touch

| File | Change |
|------|--------|
| `src/agent_augury/session.py` | `gate_waiting`, `wait_gate_wakeup`, integrate into `run_agent`; optional wake events |
| `src/agent_augury/protocol/collaboration.py` | optional: expose `is_gate_waiting` helper; hook phase-change to wake events |
| `src/agent_augury/server.py` | optional v1.1: signal on inbox push |
| `src/agent_augury/agent/system_prompt.py` | keep/strengthen no-wait-narration (already started) |
| `tests/test_gate_wait_park.py` | new (see §8) |

No Ink/Wire changes required for v1.

---

## 8. Test plan

1. **Park under P2 closed gate**  
   Backend returns text-only “waiting” once → further `complete()` calls must **not** grow while inbox empty & gate closed.

2. **Wake on inbox**  
   While A parked, B `send_message` mentioning A (or broadcast) → A’s `complete()` runs again and drains `[radio]`.

3. **Wake on phase advance**  
   Simulate unanimous `APPROVE:` / `finish_p1` → parked agents resume with new `current_phase`.

4. **No protocol regression**  
   Session without `protocol:`: text-only still continues; empty text ends (legacy).

5. **Fake P1–P5 YAML E2E**  
   `examples/p1_to_p5_protocol.yaml` still completes; no hang (no “all idle ⇒ exit”).

6. **Interrupt**  
   Parked agent + `request_interrupt()` → loops exit promptly.

---

## 9. Rejected alternatives (record)

| Idea | Why rejected |
|------|----------------|
| Park on every idle (with or without protocol) | Breaks free-form sessions; confuses “think then act” |
| End turn when all agents parked | Aborts P2 waiting for `APPROVE:` |
| 120s global idle timeout as end | Flaky under slow models; looks like random death |
| UI-only filter of “대기” strings | Doesn’t stop LLM cost or turn exit on empty |

---

## 10. Rollout

1. Land tests (§8.1–8.4) red  
2. Implement `gate_waiting` + poll wakeup in `session.py`  
3. Green fake protocol E2E  
4. (Follow-up) Event-based wake via server subscribe  
5. (Follow-up) optional config timeout  

---

## 11. Success criteria

- P2: agents that have posted (or have nothing left to post) **do not** spam “대기하겠습니다”.  
- Turn **does not** end solely because everyone went quiet before unanimous `APPROVE:`.  
- After all `APPROVE:`, phase advances and agents work on the next gate thread without a human kick.  
- Non-protocol sessions behave as before.

---

## 12. Follow-up (chatter reduction)

Park alone still allows **one model call per idle cycle** when an agent
already READY/APPROVE:d keeps stepping (wait-narration alone does **not**
fuel the loop — already parks at §3 control flow).

See [`PROTOCOL_CHATTER_REDUCTION_DESIGN.md`](./PROTOCOL_CHATTER_REDUCTION_DESIGN.md) **rev.6**:
duplicate vote no-op, `sleep`/`true` idle block, `_ready_states` checkpoint,
done-set + D5 drain-only skip (main token win), C0a `session.gate` → C0b Ink bar,
light/off.

**Caveat:** done-set park + duplicate-vote no-op together **deadlock P2** unless
`ConsensusGate` re-checks unanimity on a late `PROPOSE:` (D10 `_maybe_open`).
Park makes done agents silent in the step log — intentional; observability
moves to `session.gate` (C0a).
