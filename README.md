# agent-augury

<p align="center">
  <img src="docs/images/agent-augury.png" alt="agent-augury" width="100%">
</p>

<p align="center">
  <strong>Local multi-agent collaboration — shared threads, roles, and human-in-the-loop.</strong>
</p>

<p align="center">
  Model-agnostic runtime for teams of LLM agents that keep working while teammate messages arrive.
</p>

<p align="center">
  <a href="https://pypi.org/project/agent-augury/"><img src="https://img.shields.io/pypi/v/agent-augury.svg" alt="PyPI"></a>
  <a href="https://github.com/keepConcentration/agent-augury/actions"><img src="https://github.com/keepConcentration/agent-augury/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/keepConcentration/agent-augury/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License"></a>
</p>

---

## What it is

**agent-augury** is a local Python runtime for running several LLM agents as a team:

- shared conversation **threads** with `@mentions` and broadcast
- **non-blocking** delivery — agents keep working; messages land at the next `step()`
- assignable **roles**, optional **P1–P5** collaboration protocol
- **you** as a first-class participant (`ask_user`, Ink UI, Discord/Slack mirrors)
- **model-agnostic** backends (OpenAI-compatible, Nous API key / OAuth)

Install it, configure agents in YAML (or the wizard), and run a session from the terminal.

```text
                         You (Ink / Discord / …)
                                │
                          Session + Gateway
                                │
               ┌────────────────┼────────────────┐
               ▼                ▼                ▼
           Agent A          Agent B          Agent N
           (any model)      (any model)      (any model)
               │                │                │
               └────────────────┼────────────────┘
                                ▼
                       Message Server (SSOT)
```

---

## How teammates communicate

Agents talk through three primitives:

| Primitive | Behavior |
|---|---|
| `create_thread(name, participants)` | Open a named thread |
| `send_message(thread, content, mentions)` | Fire-and-forget post (`mentions` empty = broadcast) |
| `read_resource()` | Snapshot threads/messages when needed |

Messages are pushed into each target's inbox. On the next `step()`, the runtime drains that inbox into the agent's context — no blocking “wait for reply” loop.

In the Ink Surface you can direct a mid-session note with `@agent-id …` (omit `@` to broadcast).

---

## Roles

Give each agent a persona from config:

```yaml
roles:
  orchestrator:
    prompt: |
      You are the orchestrator. Decompose work, coordinate, and integrate results.
  architect:
    prompt: |
      You are the architect. Design structure, tech stack, and trade-offs.

agents:
  - id: agent-1
    role: orchestrator
    backend: { ... }
```

- `role: <name>` uses a preset; `role_custom: "..."` is inline.
- Agent ids must be unique (case-insensitive). The id `human` is reserved.

---

## Human-in-the-loop

You are a first-class participant:

- Agents can call `ask_user(question, options?)` mid-session
- Your reply (and free-form `@agent-id` notes) use the same inbox path as agent messages
- On a TTY with Node.js, **Ink** is the interactive Surface (always-on input)

```bash
agent-augury --config session.yaml
```

Optional Discord bots / webhook mirrors and Slack webhooks are observe (or opt-in inbound) surfaces — protocol state stays in the message server.

---

## Collaboration protocol (optional)

A five-phase flow is available when you enable `protocol:` in config:

```text
P1 EXPLORE → P2 SPLIT → P3 EXECUTE → P4 REVIEW → P5 SUBMIT
```

Gates require explicit group approval before advancing. Use it when you want structured team work; omit it for free-form multi-agent sessions.

---

## Model agnostic

Backends share one interface. Mix providers per agent:

- OpenAI-compatible APIs (including OpenRouter)
- Nous Portal (API key)
- Nous Portal (OAuth device code)
- `type: fake` only with `--demo` (tests / offline examples)

Secrets stay in environment variables; YAML stores env **names** only.

---

## Quick start

### Install

```bash
pip install agent-augury
```

**Ink Surface (required for the interactive TUI):**

1. Install **Node.js >= 22** so `npm` is on your `PATH` ([nodejs.org](https://nodejs.org/)).
2. Run `agent-augury` — the wheel bundles `fronts/ink` sources; on first launch they are copied to a user cache and `npm install` runs there.
3. Optional overrides:
   - `AUGURY_INK_DIR` — use a specific Ink front directory (e.g. a git checkout of `fronts/ink`)
   - `AUGURY_PROJECT_ROOT` — repo root when developing from a clone
   - `AUGURY_CACHE_DIR` — base directory for the Ink cache (default: platform cache)

Developers working from this repository can use an editable install (`uv sync` / `pip install -e .`); the checkout’s `fronts/ink` is picked up automatically.

### Wizard

```bash
agent-augury
```

### Run a session

```bash
agent-augury --config examples/consensus_openai.yaml
```

Offline / CI-style example (scripted backends):

```bash
agent-augury --demo --config examples/demo.yaml
```

Protocol example:

```bash
agent-augury --demo --config examples/p1_to_p5_protocol.yaml
```

### Headless (no Ink)

Boot Core without the Ink TUI — useful when Discord/Slack is the human window
(or for CI). This is a **launcher**, not another chat Surface:

```bash
agent-augury --headless
agent-augury --headless --reconfigure   # re-run wizard, then headless
agent-augury --headless --config session.yaml   # explicit path
```

With no `--config`, uses the wizard default
(`~/.agent-augury/agent-augury-session.yaml`). With `bots[].inbound: true`,
channel messages start the next turn after idle. Stop with Ctrl+C.
Use `--no-auto-start` to wait for the first inbound message instead of running
the config `task` immediately.

---

## Tool approval (shell / file write)

Dangerous local tools can require a human grant before side effects run
(fail-closed; no blocking await — the agent gets `pending_approval` and continues).

Defaults (override under `tools.approval` in YAML):

| Class | Tools | Default |
|-------|--------|---------|
| `shell` | `run_command` | `dangerous` (Hermes-like: only destructive patterns) |
| `file_write` | `write_file` / `edit_file` / `append_file` | `off` (`allowed_roots` still applies) |
| `web` | `web_search` / `fetch_url` | `off` |

```yaml
tools:
  approval:
    shell: dangerous        # require | dangerous | off
    file_write: off         # require | off  (dangerous ≡ require for file_write)
    web: off
    ttl_seconds: 600
    # bypass: true          # tests only — never in production
```

- **`require`**: every call in that class needs Approve/Deny
- **`dangerous`** (shell): only patterns like `rm -rf /`, `curl|sh`, `dd of=/dev/…`, force-push, etc.
- **`off`**: no approval prompts (still subject to shell allow/block and `allowed_roots`)
- **Ink**: approval card → type `1`/`approve` or `2`/`deny`
- **Discord inbound** (`bots[].inbound: true`): Approve/Deny **buttons** per request (preferred); text `1`/`2` = oldest pending only
- **`--demo`**: bypasses approval (scripted / CI runs)
- Observe-only mirrors are **not** an approval channel; with no interact surface, gated tools are denied (`no_approval_channel`)

Design notes: [`docs/architecture/TOOL_HUMAN_APPROVAL_DESIGN.md`](docs/architecture/TOOL_HUMAN_APPROVAL_DESIGN.md)

---

## Command-line options

| Flag | Description |
|------|-------------|
| `--config <yaml>` | Run from YAML (skips wizard) |
| `--demo` | Allow `type: fake` backends (offline / tests) |
| `--reconfigure` | Re-run wizard (with `--headless`: then boot Core without Ink) |
| `--output <path>` | Wizard output path (only without `--config`) |
| `--quiet` | Suppress live event noise |
| `--ink` | Ink Surface (default when available) |
| `--ink-hello` | Ink hello against the Gateway (no full session) |
| `--headless` | Boot Core without Ink (default: wizard session YAML) |
| `--no-auto-start` | With `--headless`: wait for `human.send` before first run |

---

## Design principles

1. **Keep working while listening** — teammate traffic must not force a blocking wait.
2. **No single agent is SSOT** — the message server owns shared state.
3. **Surfaces are views** — Discord/Slack/Ink observe or interact; they do not own protocol state.
4. **Models are swappable** — communication rules live in the runtime, not one vendor SDK.
5. **Humans participate** — ask, answer, and `@mention` mid-session without special-casing the protocol.
6. **Stay observable** — tools, steps, and messages stream on the Wire bus for UIs and mirrors.

See [`DESIGN.md`](DESIGN.md) for implementation history and protocol details.

---

## License

Apache-2.0
