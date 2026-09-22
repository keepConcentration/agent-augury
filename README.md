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

Package layout (import paths; no legacy shims):

```text
agent_augury.core.*          # Session, MessageServer, agent/, protocol/, checkpoint
agent_augury.channels.*      # discord/, slack/, chat formatting
agent_augury.gateway.*       # Wire bus, Ink/headless bridges
```

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

Gates require explicit group approval (`PROPOSE:` / `APPROVE:` via `send_message` on the gate thread — prose alone does not count) before advancing. Use it when you want structured team work; omit it for free-form multi-agent sessions.

When `protocol:` is enabled, the runtime **pre-opens** gate threads (`plan`, `execution`, …) and `human`. Agents should **reuse those thread ids**; inventing new thread names during P1–P5 is rejected.

Optional token-saving **attention budget** (off by default):

```yaml
attention:
  enabled: true          # default false
  # display.chat / surfaces.* are separate (chat UI density)
```

See [`docs/architecture/AGENT_RELEVANCE_BUDGET_DESIGN.md`](docs/architecture/AGENT_RELEVANCE_BUDGET_DESIGN.md).

Optional **human phase approval** (after agents agree, wait for you):

```yaml
protocol:
  gates:
    P2_SPLIT: plan
    P3_EXECUTE: execution
    P4_REVIEW: review
    P5_SUBMIT: submission
  human_approval:
    P5_SUBMIT: true   # defaults: all phases false
```

Flow: agents `APPROVE:` among themselves → Wire `session.human_approval_pending` → you reply `APPROVE:` / `REJECT:` (Ink or Discord inbound). Separate from tool-approval buttons. See `docs/architecture/HUMAN_APPROVAL_GATE_DESIGN.md`.

**Dynamic roster** (0.7.2+) — the agents you configure are a *pool*; how many actually run is decided per turn:

```yaml
protocol:
  participants: [agent-1, agent-2, agent-3, agent-4]   # the pool
  roster:
    start: 2     # how many explore in P1 (default min(2, pool)). -1 = all
    max: 8       # cap per phase (default: pool size)
```

P1 starts with `start` agents; P2's `ASSIGN` / `SPLIT: none` lines then set who runs P3–P5 — naming a pooled agent that is not yet active wakes it. Benched agents get no task and cost nothing.

> **Behaviour change in 0.7.2:** with a `protocol:` section and no `roster:`, `start` defaults to 2 — previously every configured agent ran every turn. Set `start: -1` to keep the old behaviour. Research backing and design: [`docs/architecture/DYNAMIC_ROSTER_DESIGN.md`](docs/architecture/DYNAMIC_ROSTER_DESIGN.md).

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
   - `AUGURY_FILE_ROOT` — directory the agents' file tools may read/write
     (default: the directory you launched from). The session prints the
     effective root on startup; a path outside it is refused and logged.
   - `AUGURY_INK_DEBUG` — `1` dumps every Wire line (both directions, sensitive
     values masked) to `.augury-ink-debug.log` under `AUGURY_FILE_ROOT`; set it
     to a path to choose the file. Off by default.

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

### Session resume (checkpoint)

Interrupt + process exit used to wipe agent memory. Checkpoints now persist under
`~/.agent-augury/sessions/<id>/` (conversation, protocol phase, MessageServer DB,
pending approvals).

- **Default on** for headless and Ink (`--demo` disables)
- Reboot → hydrate → **idle-wait**; next message is **appended** (even if identical)
- Fresh work: `--new-session` or `AGENT_AUGURY_NEW_SESSION=1`
- Pin id: `--session <id>` / `AGENT_AUGURY_SESSION`
- Manage: `agent-augury sessions list|show|rm` · corrupt → `sessions/quarantine/`

```yaml
session:
  checkpoint:
    enabled: true
    resume: auto          # auto | ask | never
    dir: ~/.agent-augury/sessions
    approvals_persist: true
    compact:
      enabled: true
      soft_limit_chars: 200000
      llm_summary: false   # optional: summarize via agent backend on async flush
```

```bash
agent-augury sessions list
agent-augury sessions show <id>
agent-augury sessions rm <id> --yes
agent-augury sessions quarantine list
```

---

## Tool approval (shell / file write)

Dangerous local tools can require a human grant before side effects run
(fail-closed; no blocking await — the agent gets `pending_approval` and continues).

Defaults (override under `tools.approval` in YAML):

| Class | Tools | Default |
|-------|--------|---------|
| `shell` | `run_command` | `dangerous` (Hermes-like: only destructive patterns) |
| `file_write` | `write_file` / `edit_file` / `append_file` | `dangerous` (paths that arm later execution; `allowed_roots` still applies) |
| `web` | `web_search` / `fetch_url` | `off` |

```yaml
tools:
  approval:
    shell: dangerous        # require | dangerous | off
    file_write: dangerous   # require | dangerous | off
    web: off
    ttl_seconds: 600
    # bypass: true          # tests only — never in production
```

`dangerous` for `file_write` gates writes that turn a later innocuous action
into code execution — `.git/` internals (untracked, so `git diff` never shows
them), CI configs, `.envrc`/`.env`, shell rc files — and lets ordinary project
writes through unattended. Use `require` to gate every write.


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
| `--ink-hello` | Ink hello against the Gateway (no full session) |
| `--headless` | Boot Core without Ink (default: wizard session YAML) |
| `--no-auto-start` | With `--headless`: wait for `human.send` before first run |
| `--new-session` | Ignore LATEST checkpoint; start fresh |
| `--session <id>` | Resume or bind to this session id |

> **Removed in 0.7.4:** `--ink`. It never changed anything — Ink is the default
> whenever a TTY and Node are available; use `--headless` to opt out. Drop it
> from scripts: the CLI now rejects unknown flags instead of abbreviating them.

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
