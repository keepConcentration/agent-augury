# agent-augury

**Model-agnostic passive awareness multi-agent runtime.**

Concept inherited from [AgentRadio](https://github.com/Coral-Protocol/AgentRadio)
([arXiv:2607.28430](https://arxiv.org/abs/2607.28430)) and re-implemented as a
standalone open-source project — not bound to any model, channel, or cloud runtime.

> Design doc: see [`DESIGN.md`](DESIGN.md) (Korean).

## Core idea

Agents listen **while they work**. Incoming teammate messages are pushed to an
inbox by the in-process message server; each agent's next `step()` drains the
inbox automatically. Communication never blocks the work — that is *passive
awareness* (the paper's L3 mode). The blocking foreground `wait_for_mention`
(the paper's L2) exists only as a contrast mode for verification.

## Three primitives (implemented by the internal message server)

| Primitive | Behavior |
|-----------|----------|
| `create_thread(name, participants)` | Create a named thread, return its id |
| `send_message(thread, content, mentions)` | Append + push to targets' inboxes; returns immediately (fire-and-forget) |
| `wait_for_mention(timeout)` | Foreground blocking receive — L2 contrast mode only |

## Status

v0.1a — communication mechanism verification.

- 2 agents (A/B) + internal message server (in-process asyncio, memory state)
- Receive model: send → inbox push → `step()` auto-drain (single consumer)
- Verification: L2 vs L3 contrast script with a Fake ModelBackend
  (`examples/l2_vs_l3_toy.py`) — asserts search counts / final answer numerically.
- No protocol gates yet (P1–P5 are v0.2 scope).

## Install & run

```bash
pip install -e ".[dev]"
pytest tests/ -q                      # unit tests
python examples/l2_vs_l3_toy.py       # L2 vs L3 contrast verification
agent-augury --config examples/demo.yaml   # E2E demo with a real backend
```

## License

Apache-2.0 (planned; inherits from the original AgentRadio).
