"""Headless Core runner — Ink 없이 Session + chat surfaces 기동.

Launcher/daemon mode (not a human Surface). Chat channels (Discord/Slack)
remain the human window; this process only boots Core and keeps it alive.

Idle ``human.send`` (e.g. Discord inbound) starts the next ``session.run()``
turn — same routing pattern as :class:`SessionStdioRunner`, without JSONL stdio.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path
from typing import Any

from agent_augury.channel.discord_bot import DiscordBotError
from agent_augury.config import load_config
from agent_augury.ink_front import resolve_project_root
from agent_augury.session import Session

from .bridge import SessionBridge
from .bus import SessionGateway, SurfaceSubscription
from .types import WireCommand, WireEvent, make_event

PROJECT_ROOT = resolve_project_root() or Path.cwd()


def _summary_line(session: Session, steps: int) -> str:
    n_agents = len(session.agents)
    flag = " interrupted" if session.interrupted() else ""
    return f"--- session: {steps} steps, {n_agents} agents{flag} ---"


def emit_startup_warnings(session: Session, *, file: Any = None) -> None:
    """Best-effort stderr warnings for headless misconfiguration."""
    out = sys.stderr if file is None else file
    has_channel = bool(
        session.bot_manager is not None
        or session.mirror is not None
        or session.slack_mirror is not None
    )
    if not has_channel:
        print(
            "warning: headless has no Discord/Slack/mirror channel — "
            "nothing will observe or accept HITL input",
            file=out,
        )
    if not session.has_interact_surface():
        print(
            "warning: no interact surface (e.g. bots[].inbound: true) — "
            "tool approvals that require human grant will deny "
            "(no_approval_channel)",
            file=out,
        )


class HeadlessRunner:
    """Drive a Core :class:`Session` without Ink / stdio."""

    def __init__(
        self,
        session: Session,
        *,
        quiet: bool = False,
        auto_start: bool = True,
    ) -> None:
        self.session = session
        self.quiet = quiet
        self.auto_start = auto_start
        self.gateway: SessionGateway = session.gateway
        self.bridge: SessionBridge = session.bridge
        self._next_turn: asyncio.Queue[str | None] = asyncio.Queue()
        self._quit = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._exit_reason = "signal"

    def _log(self, text: str) -> None:
        if self.quiet:
            return
        print(text, file=sys.stderr, flush=True)

    def _on_wire_event(self, event: WireEvent) -> None:
        if self.quiet:
            return
        etype = event.get("type")
        if etype == "log" and event.get("text"):
            print(str(event["text"]), file=sys.stderr, flush=True)
            return
        if etype == "agent.step":
            result = event.get("result") or {}
            text = result.get("text") if isinstance(result, dict) else None
            if text:
                agent = event.get("agent_id") or "?"
                print(f"[{agent}] {text}", file=sys.stderr, flush=True)

    def _attach_stderr_surface(self) -> None:
        name = "headless-stderr"
        if name in self.gateway.surfaces():
            return
        self.gateway.attach(
            SurfaceSubscription(
                name=name,
                mode="observe",
                family="ui",
                on_event=self._on_wire_event,
                event_types=frozenset({"log", "agent.step", "error", "session.ended"}),
            )
        )

    def _publish_step(self, agent_id: str, result: Any) -> None:
        if self.quiet:
            return
        try:
            self.bridge.publish_core_event(
                {"type": "step", "agent_id": agent_id, "result": result}
            )
        except Exception:  # noqa: BLE001, S110 — never break Core for logging
            pass

    def _install_command_router(self) -> None:
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
                self._enqueue_turn("")
                return {"started": True}
            if typ == "session.quit":
                self._exit_reason = "quit"
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

    def _on_quit(self) -> None:
        self.session.request_interrupt()
        self._quit.set()
        self._enqueue_turn_sentinel()

    def _on_interrupt(self) -> None:
        self.session.request_interrupt()

    def _install_signal_handlers(self) -> None:
        loop = self._loop
        if loop is None:
            return

        def _handle() -> None:
            self._exit_reason = "signal"
            self._on_quit()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle)
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows / restricted environments — KeyboardInterrupt path.
                pass

    async def _do_run(self, prompt: str | None) -> int:
        self.bridge.set_running(True)
        try:
            initial = prompt if prompt else None
            return await self.session.run(initial_prompt=initial)
        finally:
            self.bridge.set_running(False)

    async def _after_run(self, steps: int) -> None:
        await self.session.flush_observers()
        if not self.quiet:
            if self.session.interrupted():
                self.gateway.publish(make_event("log", text="run interrupted"))
            self.gateway.publish(
                make_event("log", text=_summary_line(self.session, steps))
            )

    async def _session_loop(self) -> int:
        # Bots/mirrors must be up while idle (waiting for Discord HITL).
        await self.session._setup()

        agent_ids = [a.agent_id for a in self.session.agents]
        self.gateway.publish(
            make_event(
                "session.started",
                surface="headless",
                note="augury headless core",
                agents=agent_ids,
            )
        )
        if self.session.task:
            self.gateway.publish(
                make_event(
                    "log",
                    text=(
                        f"config task ready: {self.session.task!r} "
                        "(auto-start or Discord/Slack human.send; Ctrl+C to stop)"
                    ),
                )
            )
        else:
            self.gateway.publish(
                make_event(
                    "log",
                    text="waiting for human.send (Discord inbound) or Ctrl+C",
                )
            )

        steps = 0
        if self.auto_start and self.session.task:
            steps = await self._do_run(None)
            await self._after_run(steps)

        while not self._quit.is_set():
            prompt = await self._next_turn.get()
            if prompt is None or self._quit.is_set():
                break
            steps = await self._do_run(prompt if prompt else None)
            if self._quit.is_set():
                break
            await self._after_run(steps)

        try:
            self.gateway.publish(
                make_event("session.ended", reason=self._exit_reason)
            )
        except Exception:  # noqa: BLE001, S110
            pass
        return 0

    async def run(self) -> int:
        self._loop = asyncio.get_running_loop()
        self.bridge.loop = self._loop
        self.session.on_step = self._publish_step
        self._attach_stderr_surface()
        self._install_command_router()
        self._install_signal_handlers()
        emit_startup_warnings(self.session)

        try:
            return await self._session_loop()
        finally:
            await self.session.close()


def run_headless_session(
    config: str,
    *,
    demo: bool = False,
    quiet: bool = False,
    auto_start: bool = True,
) -> int:
    """Load YAML and run :class:`HeadlessRunner` (CLI entry)."""
    cfg_path = Path(config).expanduser().resolve()
    if not cfg_path.is_file():
        print(f"error: config not found: {cfg_path}", file=sys.stderr)
        return 1

    cfg = load_config(str(cfg_path), allow_fake=demo)
    session = Session.from_config(
        cfg,
        allowed_roots=[str(PROJECT_ROOT)],
        approval_bypass=bool(demo),
    )
    runner = HeadlessRunner(
        session,
        quiet=quiet,
        auto_start=auto_start,
    )
    try:
        return asyncio.run(runner.run())
    except KeyboardInterrupt:
        return 130
    except DiscordBotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
