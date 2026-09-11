# Augury Wire Protocol schemas (M0)

Canonical JSON shapes shared by Interactive UI (Ink/Desktop/Web) and
Chat Channel adapters (Discord/Slack).

Authoritative design: [`docs/architecture/MULTI_FRONT_DESIGN.md`](../docs/architecture/MULTI_FRONT_DESIGN.md).

| File | Purpose |
|------|---------|
| `envelope.schema.json` | Framing: `dir` = event / cmd / result |
| `events.schema.json` | Server → surfaces |
| `commands.schema.json` | Surfaces → server |

Python runtime types: `agent_augury.gateway.types`.

JSONL stdio bridge (M2): `agent_augury.gateway.stdio` — Ink owns the TTY;
Python Gateway child speaks Wire on its stdin/stdout (`python -m agent_augury.gateway`).
