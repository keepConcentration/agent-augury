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
awareness*.

## Three primitives (implemented by the internal message server)

| Primitive | Behavior |
|-----------|----------|
| `create_thread(name, participants)` | Create a named thread, return its id |
| `send_message(thread, content, mentions)` | Append + push to targets' inboxes; returns immediately (fire-and-forget) |
| `read_resource()` | Explicit full state dump for recovery or aggregation |

## Status

v0.3 — OAuth provider-level authentication, unlimited steps by default.

- 3+ agents (A/B/C) + internal message server (in-process asyncio, memory state)
- Receive model: send → inbox push → `step()` auto-drain (single consumer)
- Consensus gate: propose → unanimous APPROVE → gate OPEN → work shares
  (`examples/consensus_demo.py`) — order-based assertions on server sequence numbers.
- **P1~P5 full protocol**: explore → split → execute → review → submit
  (`examples/p1_to_p5_demo.py`) — all five phases advance in order with unanimous gates.
- Real-backend E2E: `examples/consensus_openai.yaml` — a real OpenAI-compatible
  LLM generates PROPOSE/APPROVE messages autonomously (secrets via `.env` only).
- Discord observation mirror: read-only webhook flush; core never reads back.
- Phase transition hooks: explicit `PhaseManager` for v0.2 P1~P5 expansion.
- **OAuth provider-level auth**: multiple agents sharing `nous_oauth` authenticate
  only once — the token is reused across all backends for the same provider.
- **Unlimited steps by default**: `max_steps=0` means no cap; set a positive
  integer to limit total steps across all agents.

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
python examples/consensus_demo.py     # v0.1b consensus gate verification
python examples/p1_to_p5_demo.py      # v0.2 P1~P5 full protocol verification
agent-augury --config examples/p1_to_p5_protocol.yaml  # same P1~P5 flow via YAML (offline)
agent-augury --config examples/demo.yaml   # E2E demo with a fake backend
agent-augury --config examples/consensus_openai.yaml  # E2E with a real LLM (needs OPENAI_API_KEY)

# Opt-in OpenAI API smoke (incurs cost):
#   export AUGURY_RUN_OPENAI_TESTS=1 OPENAI_API_KEY=sk-...
#   pytest tests/test_integration_openai.py -m openai -v
```

## Running locally (Windows + .venv)

The quickest way to run agent-augury on Windows is from a project-local
`.venv`. The `agent-augury` console script is installed into
`.venv\Scripts\` — use that instead of a global Python install, which
lacks the project's dependencies (aiosqlite, PyYAML, etc.).

```powershell
# PowerShell — from the project root
.venv\Scripts\agent-augury

# cmd
.venv\Scripts\agent-augury.exe
```

> **Why `.venv`?** A globally installed `agent-augury` (e.g. via
> `pip install` into a system Python) has no access to the project's
> dependencies and will fail with `ModuleNotFoundError`. Always run
> through the project's `.venv`.

To run `agent-augury` from any directory, add `.venv\Scripts` to your
user environment `PATH`:

```powershell
# PowerShell (persistent)
[Environment]::SetEnvironmentVariable(
    "Path",
    "$env:USERPROFILE\IdeaProjects\agent-augury\.venv\Scripts;" +
    [Environment]::GetEnvironmentVariable("Path", "User"),
    "User"
)
```

After reopening your terminal, `agent-augury` works from anywhere.

### Interactive wizard

Running `agent-augury` without `--config` launches an interactive
wizard that builds a YAML config through a conversation:

```powershell
agent-augury
```

The wizard walks through:

1. **Max steps** — total step cap across all agents. `0` means
   *unlimited* (the default). A positive integer caps the entire
   session.
2. **Backend / provider** — choose per agent:
   - `openai` — OpenAI-compatible API
   - `nous` — Nous Portal (API key)
   - `nous_oauth` — Nous Portal (OAuth device code — no API key)
3. **Add another agent?** — repeat step 2 for multi-agent setups.

The wizard then asks for an initial task and starts the session.

> **No API keys on disk.** Only environment variable *names* are stored
> (e.g. `OPENAI_API_KEY`). Actual values are read from your environment
> or a `.env` file at runtime.

### Saved model settings

Model settings (max_steps, agent IDs, backend types, model names,
base URLs, env-var names) are persisted to
`~/.agent-augury/model_config.json`. On the next run, the wizard
detects this file and skips straight to the task prompt — no need to
re-enter backends or agent structure.

Use `--reconfigure` to discard the saved settings and re-run the full
wizard from scratch:

```powershell
agent-augury --reconfigure
```

### OAuth (nous_oauth) — one-time authentication

When you pick `nous_oauth`, the wizard opens a browser for the OAuth
device-code flow. The resulting token is stored at
`~/.agent-augury/tokens.json` (mode `0o600`) and **shared across all
agents using the same provider** — so even with 3+ agents on
`nous_oauth`, the browser opens only once.

- Token expiry → automatic refresh.
- Refresh fails → one re-authentication, then the new token is saved.

### Running from a YAML config

Skip the wizard entirely by pointing at a pre-built YAML:

```powershell
agent-augury --config examples\p1_to_p5_protocol.yaml
agent-augury --config examples\consensus_openai.yaml
```

Use `--output` to control where the wizard writes the generated YAML
(only valid without `--config`):

```powershell
agent-augury --output my_session.yaml
```

### Command-line options

| Flag | Description |
|------|-------------|
| `--config <yaml>` | Run directly from a YAML file (skips wizard) |
| `--reconfigure` | Discard saved model settings and re-run the wizard |
| `--output <path>` | Wizard output path (only valid without `--config`) |
| `--quiet` | Suppress broadcast events (currently unimplemented) |

## License

Apache-2.0. See [LICENSE](LICENSE).
