# agent-augury

<p align="center">
  <img src="docs/images/agent-augury.png" alt="agent-augury — many agents interpreting one signal" width="100%">
</p>

<p align="center">
  <strong>Many agents. One signal. Collective awareness.</strong>
</p>

<p align="center">
  Model-agnostic passive awareness runtime for multi-agent systems.
</p>

<p align="center">
  <a href="https://pypi.org/project/agent-augury/"><img src="https://img.shields.io/pypi/v/agent-augury.svg" alt="PyPI"></a>
  <a href="https://github.com/keepConcentration/agent-augury/actions"><img src="https://github.com/keepConcentration/agent-augury/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/keepConcentration/agent-augury/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License"></a>
</p>

---

## What is Augury?

**Augury** is the practice of observing signs and interpreting what they might mean.

An augur does not receive an answer directly.

They observe.

They interpret.

They compare what they see with what others have seen.

And from many incomplete signals, they form a conclusion.

**agent-augury applies the same idea to multi-agent systems.**

Agents work independently while remaining passively aware of what their teammates discover.

A message from another agent does not interrupt the current work.

It becomes a signal waiting to be absorbed at the next step.

```text
                    ┌──────────────┐
                    │    SIGNAL    │
                    └──────┬───────┘
                           │
             ┌─────────────┼─────────────┐
             │             │             │
             ▼             ▼             ▼
          Agent A        Agent B       Agent C
          observes       observes      observes
             │             │             │
             └─────────────┼─────────────┘
                           │
                           ▼
                     interpretation
                           │
                           ▼
                      consensus
                           │
                           ▼
                         action
```

> **The signal is shared. The interpretation is independent.**

---

## The idea

Most multi-agent systems treat communication as something agents explicitly wait for.

That creates a choice:

- stop working and listen
- or keep working and miss what others discovered

**Passive awareness removes that choice.**

An agent keeps working.

Meanwhile, messages from teammates are pushed into its inbox.

When the agent reaches its next `step()`, those messages are automatically absorbed into its context.

```text
Agent A                         Agent B
  │                                │
  │────── working ────────────────►│
  │                                │
  │                         discovers something
  │                                │
  │◄──────── signal ────────────────│
  │       (inbox push)              │
  │                                │
  │────── keeps working ───────────►│
  │                                │
  │       next step()               │
  │       ↓                         │
  │       absorbs signal            │
  │       ↓                         │
  │       adapts reasoning          │
```

Communication becomes **background awareness**, rather than a blocking operation.

---

## Why "Augury"?

The name describes the role of the agents.

An **oracle** gives you an answer.

An **augur** interprets signs.

agent-augury is designed around the latter.

Each agent sees only part of the picture.

Each agent may interpret the same signal differently.

No single agent needs to know everything.

The system becomes useful when those independent observations can be shared, challenged, reviewed, and eventually combined.

---

## Core primitives

The runtime is built around three simple primitives:

| Primitive | Behavior |
|---|---|
| `create_thread(name, participants)` | Create a named conversation thread |
| `send_message(thread, content, mentions)` | Send a message immediately; fire-and-forget |
| `read_resource()` | Explicitly read the shared state for recovery or aggregation |

The important part is not the API itself.

It is what happens **between** `send_message()` and the next `step()`.

The sender does not wait.

The receiver does not block.

The signal simply becomes part of the receiver's next working context.

---

## Roles (assignable personas)

Each agent can be given a **role** that shapes how it interprets and contributes.

Roles are user-defined in the config, so you can compose your own team:

```yaml
roles:
  orchestrator:
    prompt: |
      You are the orchestrator. Decompose work, coordinate, and integrate results.
  architect:
    prompt: |
      You are the architect. Design structure, tech stack, and trade-offs.
  backend-developer:
    prompt: |
      You are the backend developer. Design and implement APIs and data.
  frontend-developer:
    prompt: |
      You are the frontend developer. Build client logic and interactions.

agents:
  - id: agent-1
    role: orchestrator
    backend: { ... }
```

- `role: <name>` references a preset above; `role_custom: "..."` defines one inline.
- A role without a matching preset, or using both `role` and `role_custom`, is rejected at load time.
- Agents without a role behave exactly as before (fully backward compatible).

The orchestrator/architect/backend/frontend split above is just one example — roles are free-form and reusable across configs.

---

## Human-in-the-loop

agent-augury models **you** as a first-class participant.

Add a `human:` section to the config and agents gain an `ask_user` tool, so they can ask you questions or request confirmation mid-session — while continuing to work (fire-and-forget, matching the passive philosophy).

```yaml
human:
  id: human
  interface: cli     # cli (plain input) | tui (always-on prompt) | discord | file
  tui:               # used when interface: tui
    multiline: true
    history_file: ~/.agent-augury/human_history.txt
```

- `ask_user(question, options)` — agent asks; your reply arrives as a `[radio]` block on its next `step()`.
- `human_send()` — the runtime injects your message into the same inbox path as any agent message.
- The reserved name `human` is case-insensitively blocked from agent ids, so a user message can never be mistaken for an agent message.

### Always-on input (TUI)

Set `interface: tui` to get a **persistent input bar** (powered by prompt_toolkit). It is enabled automatically whenever a `human:` section is present — no extra flag needed.

- an input line pinned to the bottom that never scrolls away — type anytime, even while agents work
- `ask_user` questions and options pinned in a bottom toolbar, so they don't disappear into the log
- answer options by number (`1`, `2`, …) or plain text
- multi-line paste, history, and Korean IME support

```bash
agent-augury --config session.yaml
```

---

## Collaboration protocol

agent-augury also provides a five-phase collaboration protocol:

```text
P1  EXPLORE
 │
 ▼
P2  SPLIT ──────► unanimous approval
 │
 ▼
P3  EXECUTE
 │
 ▼
P4  REVIEW ─────► unanimous approval
 │
 ▼
P5  SUBMIT ─────► unanimous approval
```

Each phase is explicitly gated.

Agents can independently explore and execute, while shared protocol state coordinates when the group should move forward.

This makes passive awareness useful beyond simple message passing:

**agents can remain independent without becoming isolated.**

---

## Architecture

```text
                         User
                          │
                    CLI / YAML config
                          │
                          ▼
                    ┌───────────┐
                    │  Session  │
                    └─────┬─────┘
                          │
             ┌────────────┼────────────┐
             │            │            │
             ▼            ▼            ▼
         Agent A       Agent B       Agent N
         Model A       Model B       Model C
             │            │            │
             └────────────┼────────────┘
                          │
                          ▼
                 ┌─────────────────┐
                 │ Message Server  │
                 │      SSOT       │
                 └────────┬────────┘
                          │
                    read-only mirrors
                          │
                          ▼
                    Discord / CLI
```

The internal message server is the **single source of truth**.

External channels such as Discord and the human input bar are observation surfaces, not protocol state.

This keeps the core runtime independent from any particular messaging platform.

---

## Model agnostic

agent-augury does not require a specific model.

Backends are isolated behind a common interface, allowing different agents to use different providers.

Currently supported:

- OpenAI-compatible APIs
- Nous Portal (API key)
- Nous Portal (OAuth device code)
- Fake backends for deterministic testing

The runtime cares about **how agents communicate**, not which model produces their reasoning.

---

## Quick start

### Install

```bash
pip install agent-augury
```

### Start the interactive wizard

```bash
agent-augury
```

The wizard walks you through:

1. the maximum number of steps (`0` = unlimited)
2. the backend/provider for each agent
3. the number of agents
4. the initial task (multi-line paste supported)

### Run an offline demo

```bash
agent-augury --demo --config examples/demo.yaml
```

### Run the P1–P5 protocol demo

```bash
agent-augury --demo --config examples/p1_to_p5_protocol.yaml
```

### Run with a real OpenAI-compatible model

```bash
agent-augury --config examples/consensus_openai.yaml
```

### Run with human-in-the-loop TUI

```bash
agent-augury --demo --config examples/human_tui_demo.yaml
```

---

## Command-line options

| Flag | Description |
|------|-------------|
| `--config <yaml>` | Run directly from a YAML file (skips wizard) |
| `--demo` | Allow `type: fake` backends (offline demo/benchmark) |
| `--reconfigure` | Discard saved model settings and re-run the wizard |
| `--output <path>` | Wizard output path (only valid without `--config`) |
| `--quiet` | Suppress broadcast event output (only show final summary) |
| `--repl` | REPL mode — keep conversation context across multiple questions |
| `--no-interactive` | Disable human-in-the-loop input (on by default when a `human:` section is configured) |

---

## Fake demo vs. real collaboration

The repository includes both deterministic protocol tests and real LLM collaboration.

| | Fake backend | Real backend |
|---|---|---|
| Model | scripted | OpenAI-compatible LLM |
| Messages | predetermined | generated by the model |
| API key | not required | environment variable |
| Purpose | protocol verification | end-to-end collaboration |

Both execute the same collaboration protocol.

The fake backend exists to make protocol behavior deterministic and testable.

---

## Design principles

1. **Agents should not have to stop working to listen.** Communication should become awareness, not interruption.

2. **No single agent should be the source of truth.** The system should allow independent observations and interpretations.

3. **Protocol state belongs to the runtime.** Messaging platforms are views into the system, not the system itself.

4. **Models should be replaceable.** The runtime should not depend on one model provider.

5. **Collaboration should be observable.** A multi-agent system should make it possible to see how information moves through the group.

6. **The human is a participant, not an afterthought.** You can be asked, answer, and step in mid-session — without breaking the passive model.

---

## Status

Current implementation includes:

- 3+ concurrent agents, each independently configurable
- in-process asyncio message server (SSOT)
- passive inbox awareness (send → push → `step()` auto-drain)
- fire-and-forget messaging with thread and mention primitives
- assignable **roles** (orchestrator, architect, backend, frontend, …)
- **human-in-the-loop** — `ask_user`, `human_send`, human approval path
- **always-on input TUI** — persistent prompt + pinned options (prompt_toolkit)
- multi-line task input, wizard model-settings persistence
- unanimous consensus gates
- full P1–P5 collaboration protocol
- OpenAI-compatible real-backend E2E example
- Nous Portal authentication (API key + OAuth device code)
- Discord observation mirror
- unlimited steps by default (`max_steps: 0`)
- deterministic fake backend demos

---

## Origin

agent-augury is an independent open-source reimplementation of the **passive awareness** concept explored by [Coral-Protocol/AgentRadio](https://github.com/Coral-Protocol/AgentRadio).

The project inherits the underlying idea, not the original runtime.

AgentRadio explored multi-agent collaboration through a shared communication channel.

agent-augury asks a different question:

> **What if passive awareness were a model-agnostic runtime primitive?**

The result is a standalone Python runtime designed to run locally, independently of a particular model, cloud runtime, or messaging platform.

See [`DESIGN.md`](DESIGN.md) for the detailed design decisions and implementation history.

---

## License

Apache-2.0
