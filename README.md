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

v0.2 — P1~P5 full collaboration protocol.

- 3+ agents (A/B/C) + internal message server (in-process asyncio, memory state)
- Receive model: send → inbox push → `step()` auto-drain (single consumer)
- Verification: L2 vs L3 contrast script with a Fake ModelBackend
  (`examples/l2_vs_l3_toy.py`) — asserts search counts / final answer numerically.
- Consensus gate: propose → unanimous APPROVE → gate OPEN → work shares
  (`examples/consensus_demo.py`) — order-based assertions on server sequence numbers.
- **P1~P5 full protocol**: explore → split → execute → review → submit
  (`examples/p1_to_p5_demo.py`) — all five phases advance in order with unanimous gates.
- Real-backend E2E: `examples/consensus_openai.yaml` — a real OpenAI-compatible
  LLM generates PROPOSE/APPROVE messages autonomously (secrets via `.env` only).
- Discord observation mirror: read-only webhook flush; core never reads back.
- Phase transition hooks: explicit `PhaseManager` for v0.2 P1~P5 expansion.

## Fake demo vs. real collaboration

| Dimension | `consensus_demo.py` (Fake) | `consensus_openai.yaml` (Real) |
|-----------|---------------------------|-------------------------------|
| Model | `FakeModelBackend` — pre-scripted messages | OpenAI-compatible LLM (e.g. `gpt-4o-mini`) |
| PROPOSE/APPROVE content | Fixed in code | Generated autonomously by the model |
| Secrets | None | `OPENAI_API_KEY` from environment |
| Purpose | Gate logic verification (deterministic) | E2E collaboration with real reasoning |

Both run the identical gate protocol: propose → unanimous approve → gate OPEN → work shares. The fake demo verifies the protocol is correct; the real config shows it works with an actual LLM.

## Install & run

```bash
pip install -e ".[dev]"
pytest tests/ -q                      # unit tests (offline; skips OpenAI integration)
python examples/l2_vs_l3_toy.py       # L2 vs L3 contrast verification
python examples/consensus_demo.py     # v0.1b consensus gate verification
python examples/p1_to_p5_demo.py      # v0.2 P1~P5 full protocol verification
agent-augury --config examples/p1_to_p5_protocol.yaml  # same P1~P5 flow via YAML (offline)
agent-augury --config examples/demo.yaml   # E2E demo with a fake backend
agent-augury --config examples/consensus_openai.yaml  # E2E with a real LLM (needs OPENAI_API_KEY)

# Opt-in OpenAI API smoke (incurs cost):
#   export AUGURY_RUN_OPENAI_TESTS=1 OPENAI_API_KEY=sk-...
#   pytest tests/test_integration_openai.py -m openai -v
```

## License

Apache-2.0. See [LICENSE](LICENSE).
