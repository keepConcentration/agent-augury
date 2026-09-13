"""M7: real Core Session over JSONL stdio (Ink owns the TTY).

Topology::

    fronts/ink (Node)  --spawn-->  python -m agent_augury.gateway.session_stdio \\
                                      --config PATH [--demo]
      keyboard = process.stdin           child stdin  <- commands (JSONL)
      render   = process.stdout          child stdout -> events/results (JSONL)

Idle ``human.send`` starts/continues a Core ``session.run()`` turn (pt TUI
parity). Mid-run ``human.send`` / ``human.answer`` go through
:class:`SessionBridge` → ``Session.human_send``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from pathlib import Path
from typing import Any

from agent_augury.config import load_config
from agent_augury.session import Session

from .bridge import SessionBridge
from .stdio import JsonlStdioBridge
from .types import WireCommand, WireResult, make_event

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class _WireAuthNoticeRelay:
    """Publish OAuth device-code lines as Wire ``log`` events (never stdout).

    Ink owns stdout as JSONL; printing auth notices there corrupts the bridge.
    Until the gateway is bound, notices go to stderr.
    """

    def __init__(self) -> None:
        self._gateway: Any = None

    def bind_gateway(self, gateway: Any) -> None:
        self._gateway = gateway

    def __call__(self, user_code: str, verification_uri: str) -> None:
        lines = [
            f"To authenticate, enter code: {user_code}",
            f"Verification URL: {verification_uri}",
        ]
        gw = self._gateway
        if gw is not None:
            try:
                for line in lines:
                    gw.publish(make_event("log", text=line))
                return
            except Exception:  # noqa: BLE001, S110 — fall back to stderr
                pass
        for line in lines:
            print(line, file=sys.stderr, flush=True)


def _summary_line(session: Session, steps: int) -> str:
    n_agents = len(session.agents)
    flag = " interrupted" if session.interrupted() else ""
    return f"--- session: {steps} steps, {n_agents} agents{flag} ---"


class SessionStdioRunner:
    """Drive a Core :class:`Session` behind a JSONL stdio surface."""

    def __init__(
        self,
        session: Session,
        *,
        quiet: bool = False,
        auto_start: bool = True,
        auth_relay: _WireAuthNoticeRelay | None = None,
    ) -> None:
        self.session = session
        self.quiet = quiet
        self.auto_start = auto_start
        self.gateway = session.gateway
        self.bridge: SessionBridge = session.bridge
        self.stdio = JsonlStdioBridge(self.gateway, surface="ink")
        self._auth_relay = auth_relay
        self._next_turn: asyncio.Queue[str | None] = asyncio.Queue()
        self._quit = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def _publish_step(self, agent_id: str, result: Any) -> None:
        if self.quiet:
            return
        try:
            self.bridge.publish_core_event(
                {"type": "step", "agent_id": agent_id, "result": result}
            )
        except Exception:  # noqa: BLE001, S110 — never break Core drain for Wire
            pass

    def _install_command_router(self) -> None:
        """Idle human.send → next-turn queue; otherwise SessionBridge."""
        bridge = self.bridge
        bridge.on_quit = self._on_quit
        bridge.on_interrupt = self._on_interrupt
        bridge.install()

        def handle(cmd: WireCommand) -> dict[str, Any]:
            typ = cmd.get("type")
            if typ == "human.send" and not bridge._running:
                content = str(cmd.get("content", "")).strip()
                if not content:
                    return {"queued": False, "error": "empty content"}
                self._enqueue_turn(content)
                return {"queued": True, "as_turn": True}
            if typ == "session.start" and not bridge._running:
                # Use config task (or empty → Session.run falls back to task).
                self._enqueue_turn("")
                return {"started": True}
            return bridge.handle_command(cmd)

        self.gateway.on_command = handle

    def _enqueue_turn(self, content: str) -> None:
        loop = self._loop
        if loop is None:
            return
        def _put() -> None:
            try:
                self._next_turn.put_nowait(content)
            except asyncio.QueueFull:
                pass
        loop.call_soon_threadsafe(_put)

    def _on_quit(self) -> None:
        self.session.request_interrupt()
        self._quit.set()
        self._enqueue_turn_sentinel()

    def _on_interrupt(self) -> None:
        self.session.request_interrupt()

    def _enqueue_turn_sentinel(self) -> None:
        loop = self._loop
        if loop is None:
            return
        def _put() -> None:
            try:
                self._next_turn.put_nowait(None)
            except asyncio.QueueFull:
                pass
        loop.call_soon_threadsafe(_put)

    async def _do_run(self, prompt: str | None) -> int:
        self.bridge.set_running(True)
        try:
            # Empty string → let Session use config ``task``.
            initial = prompt if prompt else None
            return await self.session.run(initial_prompt=initial)
        finally:
            self.bridge.set_running(False)

    async def _session_loop(self) -> int:
        agent_ids = [a.agent_id for a in self.session.agents]
        self.gateway.publish(
            make_event(
                "session.started",
                surface="ink",
                note="augury gateway session (M7)",
                agents=agent_ids,
            )
        )
        if self.session.task:
            self.gateway.publish(
                make_event(
                    "log",
                    text=(
                        f"config task ready: {self.session.task!r} "
                        "(auto-start or send a message / /quit)"
                    ),
                )
            )
        else:
            self.gateway.publish(
                make_event(
                    "log",
                    text="send a message to start the session, or /quit",
                )
            )

        steps = 0
        if self.auto_start and self.session.task:
            steps = await self._do_run(None)
            await self._after_run(steps)
        elif self.auto_start:
            # No config task — wait for first human.send.
            pass

        while not self._quit.is_set():
            prompt = await self._next_turn.get()
            if prompt is None or self._quit.is_set():
                break
            steps = await self._do_run(prompt if prompt else None)
            if self._quit.is_set():
                break
            await self._after_run(steps)

        # session.quit already publishes session.ended via SessionBridge;
        # stdin EOF publishes in _stdin_pump.
        return 0

    async def _after_run(self, steps: int) -> None:
        await self.session.flush_observers()
        if not self.quiet:
            if self.session.interrupted():
                self.gateway.publish(make_event("log", text="run interrupted"))
            self.gateway.publish(make_event("log", text=_summary_line(self.session, steps)))

    async def _stdin_pump(self) -> None:
        """Read JSONL commands from stdin (thread) and dispatch on the loop."""
        line_q: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def reader() -> None:
            try:
                for line in sys.stdin:
                    asyncio.run_coroutine_threadsafe(line_q.put(line), loop).result()
            except Exception:  # noqa: BLE001, S110 — EOF / closed pipe
                pass
            finally:
                asyncio.run_coroutine_threadsafe(line_q.put(None), loop).result()

        threading.Thread(target=reader, name="ink-stdin", daemon=True).start()

        while True:
            line = await line_q.get()
            if line is None:
                # Parent closed stdin — Bridge did not see session.quit.
                self.gateway.publish(make_event("session.ended", reason="eof"))
                self._on_quit()
                break
            if not line.strip():
                continue
            try:
                self.stdio.handle_line(line)
            except Exception as exc:  # noqa: BLE001 — keep surface alive
                err: WireResult = {
                    "dir": "result",
                    "id": "?",
                    "ok": False,
                    "error": str(exc),
                }
                self.stdio.emit_result(err)
            if self._quit.is_set():
                break

    async def run(self) -> int:
        self._loop = asyncio.get_running_loop()
        self.bridge.loop = self._loop
        self.session.on_step = self._publish_step
        self._install_command_router()
        self.stdio.attach()
        if self._auth_relay is not None:
            self._auth_relay.bind_gateway(self.gateway)

        session_task = asyncio.create_task(self._session_loop())
        stdin_task = asyncio.create_task(self._stdin_pump())
        try:
            done, pending = await asyncio.wait(
                {session_task, stdin_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            self._quit.set()
            self._enqueue_turn_sentinel()
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc is not None:
                    raise exc
            return 0
        finally:
            await self.session.close()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m agent_augury.gateway.session_stdio",
        description="M7: Core Session JSONL child for Ink",
    )
    p.add_argument(
        "--config",
        required=True,
        help="path to session YAML",
    )
    p.add_argument(
        "--demo",
        action="store_true",
        default=False,
        help="allow type:fake backends",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="suppress step/summary log events",
    )
    p.add_argument(
        "--no-auto-start",
        action="store_true",
        default=False,
        help="do not auto-run config task; wait for human.send",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    from .secrets import load_gateway_secrets

    load_gateway_secrets()
    args = build_arg_parser().parse_args(argv)
    cfg_path = args.config
    if not Path(cfg_path).is_file():
        print(f"error: config not found: {cfg_path}", file=sys.stderr)
        return 1

    cfg = load_config(cfg_path, allow_fake=args.demo)
    auth_relay = _WireAuthNoticeRelay()
    session = Session.from_config(
        cfg,
        allowed_roots=[str(PROJECT_ROOT)],
        on_user_code=auth_relay,
    )
    runner = SessionStdioRunner(
        session,
        quiet=args.quiet,
        auto_start=not args.no_auto_start,
        auth_relay=auth_relay,
    )
    try:
        return asyncio.run(runner.run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
