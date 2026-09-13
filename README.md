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

### Wizard

```bash
agent-augury
```

Requires Node.js >= 22 for the Ink Surface (`npm` must be on `PATH`).

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

---

## Command-line options

| Flag | Description |
|------|-------------|
| `--config <yaml>` | Run from YAML (skips wizard) |
| `--demo` | Allow `type: fake` backends (offline / tests) |
| `--reconfigure` | Discard saved model settings and re-run the wizard |
| `--output <path>` | Wizard output path (only without `--config`) |
| `--quiet` | Suppress live event noise |
| `--ink` | Ink Surface (default when available) |
| `--ink-hello` | Ink hello against the Gateway (no full session) |

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

## Attribution

Non-blocking teammate-inbox messaging is related to ideas explored by
[AgentRadio](https://github.com/Coral-Protocol/AgentRadio)
(Apache-2.0; arXiv:2607.28430). agent-augury is an independent implementation.
See [`NOTICE`](NOTICE).

---

## License

Apache-2.0
