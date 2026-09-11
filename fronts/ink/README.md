# @agent-augury/ink — M2/M3 hello Surface

Ink CLI that speaks **Augury Wire JSONL** with a Python Gateway child.

## Topology

```text
npm start  (this package, owns TTY)
  └─ python -m agent_augury.gateway.hello_demo
       stdin  <- commands (JSONL)
       stdout -> events / results (JSONL)
```

## Setup

```bash
# from repo root, with venv active for the Python child
cd fronts/ink
npm install
npm start
# or: agent-augury --ink-hello
```

Optional: `AUGURY_PYTHON=/path/to/python` if the default resolver cannot find `.venv`.

## Keys (M3 HITL parity)

- Startup publishes a demo `human.question` (ask_user)
- `1` / `2` / … → `human.answer` (option text resolved server-side)
- Free text while pending → `human.answer` as free reply
- `/skip` → dismiss question
- Ctrl+C → `session.interrupt` (first); again within 1s → `session.quit`
- `/quit` → `session.quit`

Design: `docs/architecture/MULTI_FRONT_DESIGN.md` (M2/M3).
