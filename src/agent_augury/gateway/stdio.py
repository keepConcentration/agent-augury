"""JSONL stdio transport for SessionGateway (M2).

Topology (Ink owns the TTY):

```text
augury-ink (Node)  --spawn-->  python -m agent_augury.gateway.hello_demo
  keyboard = process.stdin           child stdin  <- commands (JSONL)
  render   = process.stdout/stderr   child stdout -> events/results (JSONL)
```

Only JSONL Wire messages go on the child's stdout. Diagnostics use stderr.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable
from typing import TextIO

from .bus import SessionGateway, SurfaceSubscription
from .types import WireCommand, WireEvent, WireMessage, WireResult, validate_message

WriteFn = Callable[[str], None]


def encode_line(message: WireMessage) -> str:
    """Serialize a wire message as one JSONL line (no trailing spaces)."""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def decode_line(line: str) -> WireMessage:
    """Parse one JSONL line into a validated wire message."""
    raw = line.strip()
    if not raw:
        raise ValueError("empty wire line")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("wire line must be a JSON object")
    return validate_message(data)


def _default_write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


class JsonlStdioBridge:
    """Attach an interactive surface that speaks JSONL on a pair of streams."""

    def __init__(
        self,
        gateway: SessionGateway,
        *,
        surface: str = "ink",
        write: WriteFn | None = None,
    ) -> None:
        self.gateway = gateway
        self.surface = surface
        self._write = write or _default_write
        self._attached = False

    def attach(self) -> None:
        if self._attached:
            return

        def on_event(event: WireEvent) -> None:
            self._write(encode_line(event) + "\n")

        self.gateway.attach(
            SurfaceSubscription(
                name=self.surface,
                mode="interact",
                family="ui",
                on_event=on_event,
            )
        )
        self._attached = True

    def detach(self) -> None:
        if self._attached:
            self.gateway.detach(self.surface)
            self._attached = False

    def emit_result(self, result: WireResult) -> None:
        self._write(encode_line(result) + "\n")

    def handle_command(self, command: WireCommand) -> WireResult:
        result = self.gateway.dispatch(command, surface=self.surface)
        self.emit_result(result)
        return result

    def handle_line(self, line: str) -> WireResult | None:
        """Process one inbound JSONL line. Empty lines are ignored."""
        if not line.strip():
            return None
        msg = decode_line(line)
        if msg["dir"] != "cmd":
            raise ValueError(f"stdio inbound expects dir=cmd, got {msg['dir']!r}")
        return self.handle_command(msg)

    def run(
        self,
        lines: Iterable[str] | None = None,
        *,
        should_stop: Callable[[WireCommand, WireResult], bool] | None = None,
    ) -> int:
        """Pump *lines* (default: ``sys.stdin``) until EOF or *should_stop*."""
        self.attach()
        stream: Iterable[str] = sys.stdin if lines is None else lines
        stopper = should_stop or _stop_on_quit
        for line in stream:
            if not line.strip():
                continue
            try:
                msg = decode_line(line)
                if msg["dir"] != "cmd":
                    raise ValueError(
                        f"stdio inbound expects dir=cmd, got {msg['dir']!r}"
                    )
                result = self.handle_command(msg)
            except Exception as exc:  # noqa: BLE001 — keep bridge alive
                err: WireResult = {
                    "dir": "result",
                    "id": _guess_id(line),
                    "ok": False,
                    "error": str(exc),
                }
                self.emit_result(err)
                continue
            if stopper(msg, result):
                return 0
        return 0


def _stop_on_quit(command: WireCommand, result: WireResult) -> bool:
    return bool(command.get("type") == "session.quit" and result.get("ok"))


def _guess_id(line: str) -> str:
    try:
        data = json.loads(line.strip())
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
    except Exception:  # noqa: BLE001, S110
        pass
    return "?"


def run_stdio_bridge(
    gateway: SessionGateway,
    *,
    surface: str = "ink",
    stdin: TextIO | None = None,
) -> int:
    """Convenience: attach bridge and pump stdin until quit/EOF."""
    bridge = JsonlStdioBridge(gateway, surface=surface)
    source: Iterable[str] | None = None if stdin is None else stdin
    return bridge.run(source)
