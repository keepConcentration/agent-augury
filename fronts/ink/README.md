# @agent-augury/ink — Interactive Surface (M2/M3/M7)

Ink CLI that speaks **Augury Wire JSONL** with a Python Gateway child.

## Topology

```text
npm start  (this package, owns TTY)
  └─ python -m agent_augury.gateway.hello_demo          # M2/M3 (--ink-hello)
  └─ python -m agent_augury.gateway.session_stdio ...   # M7 (--config)
       stdin  <- commands (JSONL)
       stdout -> events / results (JSONL)
```

## Setup

**End users (`pip install agent-augury`):** install Node.js >= 22, then run
`agent-augury`. The wheel ships these sources; the CLI copies them to a user
cache and runs `npm install` on first launch. Override with `AUGURY_INK_DIR`.

**From this repo** (venv active). **Ink is the default session UI** when Node is available:

```bash
agent-augury --config examples/consensus_openai.yaml
agent-augury --demo --config examples/demo.yaml
agent-augury          # wizard, then Ink
```

Hello-only demo:

```bash
cd fronts/ink && npm install && npm start
# or: agent-augury --ink-hello
```

Env (set by CLI, or manually for `npm start`):

| Env | Meaning |
|-----|---------|
| `AUGURY_GATEWAY_MODE` | `hello` (default) or `session` |
| `AUGURY_CONFIG` | session YAML path (required when mode=session) |
| `AUGURY_DEMO` | `1` to allow `type: fake` backends |
| `AUGURY_PYTHON` | Python binary if `.venv` is not found |
| `AUGURY_NO_AUTO_START` | `1` to wait for first `human.send` instead of config task |
| `AUGURY_INK_DIR` | Override Ink front directory (CLI) |
| `AUGURY_PROJECT_ROOT` | Repo root override (CLI / Gateway) |

## Keys (HITL)

- `1` / `2` / … → `human.answer` (option text resolved server-side)
- Free text while pending → `human.answer` as free reply
- Free text while idle → next `session.run` turn (M7)
- `@agent-id …` → directed `human.send` / `human.answer` (`mentions`); no `@` → broadcast
- `/skip` → dismiss question
- Ctrl+C → `session.interrupt` (first); again within 1s → `session.quit`
- `/quit` → `session.quit`

Design: `docs/architecture/MULTI_FRONT_DESIGN.md` (M2/M3/M7).
