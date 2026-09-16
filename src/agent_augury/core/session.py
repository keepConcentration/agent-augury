"""Session = agent bundle + internal message server + lifecycle (§3.2).

Run loop: parallel independent asyncio tasks; each agent runs one step()
per iteration and yields cooperatively (``await asyncio.sleep(0)``). An
agent is finished when its completion produced neither text nor tool
calls, or when its scripted backend runs dry (IndexError).

v0.2: integrates the P1~P5 collaboration protocol. When a protocol is
configured, the session drives phase transitions and injects phase context
into each agent's system prompt.

v0.7 (AGENT_TOOLS_EXPANSION_DESIGN.md v4.1):
- global ``tools:`` section → ``ToolPolicy`` (default = new tools enabled
  + built-in safety); per-agent ``tools:`` deep-merged (§4.7).
- web_search is injected as a LocalTool (track B) backed by
  ``build_search_provider``; provider clients are aclosed at session end.

v1.1: ``on_user_code`` is forwarded to backends so OAuth device-code notices
can be routed to stderr / Surface logs instead of printing over the UI.

Gate-wait park (PROTOCOL_GATE_WAIT_PARK_DESIGN.md): under an active P1–P5
gate wait, idle agents (no tools, empty inbox) park instead of exiting or
re-calling the model; they wake on inbox or phase/gate change.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

# .env 자동 로딩 — 셸 export 최우선; 파일끼리는 ~/.agent-augury/.env 가 cwd/프로젝트보다 우선
try:
    from ..bot_token_env import load_merged_dotenv_into_environ

    load_merged_dotenv_into_environ()
except ImportError:
    pass

from ..auth.token_store import TokenStore
from ..backends_factory import build_backend
from ..channels.discord.bot import BotManager, DiscordBotAdapter
from ..channels.discord.inbound import attach_discord_inbound
from ..channels.discord.mirror import mirror_from_config
from ..channels.discord.observe import attach_discord_bots, attach_discord_mirror
from ..channels.display import resolve_chat_display_policy
from ..channels.slack.mirror import slack_from_config
from ..channels.slack.observe import attach_slack_mirror
from ..gateway import SessionBridge, SessionGateway
from .agent.approval import ApprovalStore, approval_notice_body
from .agent.loop import AgentLoop, LocalTool
from .agent.policy import ToolPolicy
from .agent.web import build_search_provider
from .protocol.approval import ConsensusGate
from .protocol.collaboration import CollaborationProtocol
from .protocol.phases import (
    COMPLETED,
    P1_EXPLORE,
    P2_SPLIT,
    P3_EXECUTE,
    P4_REVIEW,
    P5_SUBMIT,
    REJECTED,
    Phase,
)
from .server import MessageServer

OnStep = Callable[[str, Any], None]
OnToolEvent = Callable[[dict[str, Any]], None]

# HITL / Discord mid-run replies land here when agents invent bad thread ids.
HUMAN_CHAT_THREAD_NAME = "human"

# LocalTool 트랙 B로 주입하는 web_search tool spec (AGENT_TOOLS §3.2/§4.3.2).
_WEB_SEARCH_TOOL_SPEC = {
    "name": "web_search",
    "description": (
        "Search the web for a query; returns title/url/snippet metadata "
        "(max results configurable, default 5). Use fetch_url to read full pages."
    ),
    "schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "search query"},
            "max_results": {
                "type": "integer",
                "description": "max result count (default: tools.web.max_results)",
            },
        },
        "required": ["query"],
    },
}


def _build_local_tools(policy: ToolPolicy) -> tuple[list[LocalTool], Any | None]:
    """Build agent-local tools (track B) from a resolved policy.

    Currently only ``web_search`` (provider-backed). Returns
    ``(tools, provider)`` where *provider* must be aclosed at session end
    (None when no provider was created).
    """
    if not policy.web_enabled:
        return [], None
    provider = build_search_provider(
        policy.web_search_provider, timeout=policy.web_timeout
    )
    if provider is None:
        # 요청한 provider의 API 키가 없으면 web_search 미노출 (config 검증 경고).
        return [], None

    async def _search(args: dict[str, Any]) -> dict[str, Any]:
        query = (args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        max_results = int(args.get("max_results") or policy.web_max_results)
        results = await provider.search(query, max_results)
        return {"results": results}

    tool = LocalTool(
        name=_WEB_SEARCH_TOOL_SPEC["name"],
        description=_WEB_SEARCH_TOOL_SPEC["description"],
        schema=_WEB_SEARCH_TOOL_SPEC["schema"],
        handler=_search,
    )
    return [tool], provider


class Session:
    def __init__(
        self,
        server: MessageServer,
        agents: list[AgentLoop],
        *,
        task: str | None = None,
        max_steps: int = 0,
        bot_manager: BotManager | None = None,
        has_human: bool = False,
    ) -> None:
        self.server = server
        self.agents = agents
        self.task = task
        self.max_steps = max_steps
        self.has_human = has_human
        self.on_step: OnStep | None = None
        self.on_tool_event: OnToolEvent | None = None
        self.gate: ConsensusGate | None = None
        self.mirror: Any = None
        self.slack_mirror: Any = None
        # v0.2: P1~P5 collaboration protocol
        self.protocol: CollaborationProtocol | None = None
        # v0.3: Discord bot manager (N개 Client)
        self.bot_manager = bot_manager
        # M4: Session Gateway + bridge (Discord observe attaches as surfaces)
        self.gateway = SessionGateway()
        self.bridge = SessionBridge(gateway=self.gateway, session=self)
        self.bridge.install()
        # P0/M1: fail-closed tool approval (store may be replaced in from_config)
        self.approvals = ApprovalStore()
        # v0.7: local-tool providers (web_search) to aclose at session end
        self._local_providers: list[Any] = []
        # Unified output queue for all display events (tools, steps, read_resource)
        self._output_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._output_task: asyncio.Task | None = None
        self._setup_done: bool = False
        self._closed: bool = False
        # Ctrl+C interrupt: cooperative stop + cancel in-flight agent tasks
        self._interrupt = asyncio.Event()
        self._agent_tasks: list[asyncio.Task[None]] = []
        # P1: one READY reminder per agent before gate-wait park
        self._ready_nudged: set[str] = set()
        # Gated phases (P2+): one reminder of the bound gate thread id per agent/phase
        self._gate_thread_nudged: set[tuple[str, str]] = set()
        # Checkpoint / resume (SESSION_RESUME_DESIGN)
        self.session_id: str | None = None
        self._checkpoint_store: Any = None
        self._checkpoint_enabled: bool = False
        self._checkpoint_fingerprint: str = ""
        self._checkpoint_created_at: float | None = None
        self._pending_bootstrap: Any = None
        self._bindings: Any = None  # BindingsSnapshot | None (A5)
        self._resuming: bool = False
        self._flush_task: asyncio.Task | None = None
        self._flush_interval_task: asyncio.Task | None = None
        self._flush_debounce_ms: int = 1000
        self._flush_interval_s: float = 30.0
        self._exit_reason: str | None = None
        self._checkpoint_lock = asyncio.Lock()
        self._approvals_persist: bool = True
        self._compact_opts: Any = None
        self._compactions_this_flush: list[dict[str, Any]] = []
        self._restored_pending_approvals: list[Any] = []
        # protocol.human_approval (after_agents); defaults empty/false
        self._human_approval: dict[str, bool] = {}
        self._human_approval_needs_interact: bool = False

    def has_interact_surface(self) -> bool:
        """True when an Interactive Surface can answer approval prompts."""
        return self.gateway.has_interact_surface()

    def _on_approval_request(self, rec: Any) -> None:
        """Publish Wire ``approval.request`` (best-effort)."""
        from ..gateway.types import make_event

        try:
            self.gateway.publish(
                make_event(
                    "approval.request",
                    approval_id=rec.approval_id,
                    agent_id=rec.agent_id,
                    tool=rec.tool,
                    args_preview=dict(rec.args_snapshot),
                    ttl_seconds=max(0.0, rec.expires_at - rec.created_at),
                )
            )
        except Exception:  # noqa: BLE001, S110 — surface must not break tool path
            pass
        try:
            self.bridge.track_approval_request(
                approval_id=rec.approval_id,
                agent_id=rec.agent_id,
                tool=rec.tool,
                args_preview=dict(rec.args_snapshot),
                ttl_seconds=max(0.0, rec.expires_at - rec.created_at),
            )
        except Exception:  # noqa: BLE001, S110
            pass

    async def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Grant/deny a pending tool approval; on grant, run side effects now."""
        from ..gateway.types import make_event

        if decision not in ("granted", "denied"):
            raise ValueError("decision must be 'granted' or 'denied'")
        try:
            rec = self.approvals.resolve(approval_id, decision, reason=reason)  # type: ignore[arg-type]
        except KeyError:
            return {"ok": False, "error": f"unknown approval_id: {approval_id}"}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        agent = next((a for a in self.agents if a.agent_id == rec.agent_id), None)
        if agent is None:
            return {"ok": False, "error": f"unknown agent: {rec.agent_id}"}

        if rec.state == "denied":
            body = approval_notice_body(
                approval_id=rec.approval_id,
                decision="denied",
                tool=rec.tool,
                reason=rec.reason or "user",
            )
            self.server.inject_agent_notice(rec.agent_id, body)
            try:
                self.gateway.publish(
                    make_event(
                        "approval.resolved",
                        approval_id=rec.approval_id,
                        decision="denied",
                        reason=rec.reason or "user",
                        agent_id=rec.agent_id,
                        tool=rec.tool,
                    )
                )
            except Exception:  # noqa: BLE001, S110
                pass
            self.bridge.clear_approval(rec.approval_id)
            return {"ok": True, "state": "denied", "approval_id": rec.approval_id}

        # granted — digest check then execute snapshot args
        if not self.approvals.digest_matches(
            rec.approval_id, rec.tool, rec.args_snapshot
        ):
            rec.state = "denied"
            rec.reason = "digest_mismatch"
            body = approval_notice_body(
                approval_id=rec.approval_id,
                decision="denied",
                tool=rec.tool,
                reason="digest_mismatch",
            )
            self.server.inject_agent_notice(rec.agent_id, body)
            self.bridge.clear_approval(rec.approval_id)
            return {"ok": False, "error": "digest_mismatch", "approval_id": rec.approval_id}

        result_json = await agent._run_tool_body(rec.tool, dict(rec.args_snapshot))
        self.approvals.mark_executed(rec.approval_id)
        summary = result_json if len(result_json) <= 500 else result_json[:500] + "…"
        body = approval_notice_body(
            approval_id=rec.approval_id,
            decision="granted",
            tool=rec.tool,
            result_summary=f"RESULT {summary}",
        )
        self.server.inject_agent_notice(rec.agent_id, body)
        try:
            self.gateway.publish(
                make_event(
                    "approval.resolved",
                    approval_id=rec.approval_id,
                    decision="granted",
                    agent_id=rec.agent_id,
                    tool=rec.tool,
                )
            )
            self.gateway.publish(
                make_event(
                    "approval.granted",
                    approval_id=rec.approval_id,
                    agent_id=rec.agent_id,
                    tool=rec.tool,
                )
            )
        except Exception:  # noqa: BLE001, S110
            pass
        self.bridge.clear_approval(rec.approval_id)
        return {
            "ok": True,
            "state": "executed",
            "approval_id": rec.approval_id,
            "result": result_json,
        }

    def expire_approvals(self, *, now: float | None = None) -> list[str]:
        """Expire overdue pending tokens and push DENIED radio notices."""
        from ..gateway.types import make_event

        expired = self.approvals.expire_due(now=now)
        ids: list[str] = []
        for rec in expired:
            body = approval_notice_body(
                approval_id=rec.approval_id,
                decision="denied",
                tool=rec.tool,
                reason="expired",
            )
            self.server.inject_agent_notice(rec.agent_id, body)
            ids.append(rec.approval_id)
            try:
                self.gateway.publish(
                    make_event(
                        "approval.expired",
                        approval_id=rec.approval_id,
                        agent_id=rec.agent_id,
                        tool=rec.tool,
                    )
                )
            except Exception:  # noqa: BLE001, S110
                pass
            self.bridge.clear_approval(rec.approval_id)
        return ids

    # -- assembly ------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        cfg: dict[str, Any],
        on_step=None,
        on_tool_event=None,
        allowed_roots: list[str] | None = None,
        token_store: TokenStore | None = None,
        on_user_code: Callable[[str, str], None] | None = None,
        approval_bypass: bool = False,
        db_path: str | None = None,
    ) -> Session:
        server = MessageServer(db_path=db_path)
        agents: list[AgentLoop] = []
        pending_providers: list[Any] = []
        # Shared token store so all backends use the same OAuth tokens
        shared_token_store = token_store or TokenStore()
        approvals = ApprovalStore()

        # v0.7: 전역 tools: 섹션 → ToolPolicy (기본 = 신규 도구 전부 활성 + 안전장치)
        tools_cfg = dict(cfg.get("tools") or {})
        if approval_bypass:
            approval_cfg = dict(tools_cfg.get("approval") or {})
            approval_cfg["bypass"] = True
            tools_cfg["approval"] = approval_cfg
        global_policy = ToolPolicy.from_config(tools_cfg, allowed_roots=allowed_roots)

        # Human-in-the-loop: 항상 내장 (v1.0)
        # config에 human 섹션이 있든 없든, 항상 켜져 있음
        has_human = True
        server.register_human()

        # Rebound to the Session instance after construction (gateway surfaces).
        interact_holder: dict[str, Callable[[], bool]] = {"fn": lambda: False}
        request_holder: dict[str, Callable[[Any], None]] = {"fn": lambda _rec: None}

        for spec in cfg["agents"]:
            server.register_agent(spec["id"])
            # role 처리: role → roles 프리셋의 prompt 사용, role_custom → 직접 사용
            role_prompt = _resolve_role_prompt(spec, cfg.get("roles"))
            # v0.7: 에이전트별 tools: 딥 병합 (agent-2, §4.7-6)
            agent_policy = global_policy.merge(spec.get("tools"))
            local_tools, provider = _build_local_tools(agent_policy)
            # V1 relevance budget: attention 딥머지 + RelevancePolicy 생성
            # 전역 _attention_normalized + per-agent _attention 오버라이드
            from .attention import RelevancePolicy as _RelevancePolicy
            _global_attn = cfg.get("_attention_normalized")
            _agent_attn = spec.get("_attention")
            if _agent_attn is not None and isinstance(_global_attn, dict):
                from .attention import _deep_merge
                _merged_attn = _deep_merge(dict(_global_attn), _agent_attn)
            else:
                _merged_attn = _global_attn if isinstance(_global_attn, dict) else None
            _attn_policy = _RelevancePolicy.from_config(_merged_attn, server=server)
            agents.append(
                AgentLoop(
                    agent_id=spec["id"],
                    server=server,
                    backend=build_backend(
                        spec["backend"],
                        token_store=shared_token_store,
                        on_user_code=on_user_code,
                    ),
                    allowed_roots=list(agent_policy.allowed_roots) or None,
                    policy=agent_policy,
                    local_tools=local_tools,
                    role_prompt=role_prompt,
                    has_human=has_human,
                    approvals=approvals,
                    has_interact_surface=lambda: interact_holder["fn"](),
                    on_approval_request=lambda rec: request_holder["fn"](rec),
                    on_tool_call=lambda agent_id, tool, args, result, _server=server: (
                        _server._emit_event({
                            "type": "tool",
                            "agent_id": agent_id,
                            "tool": tool,
                            "args": args,
                            "result": result,
                            "timestamp": __import__("time").time(),
                        })
                    ),
                    attention_policy=_attn_policy,
                    attention_config=_merged_attn,
                )
            )
            if provider is not None:
                pending_providers.append(provider)

        # v0.3: bots 섹션 파싱 → BotManager 구성
        bot_manager: BotManager | None = None
        bots_spec = cfg.get("bots")
        if bots_spec:
            bot_manager = BotManager()
            for bot_entry in bots_spec:
                token_env = bot_entry["token_env"]
                from ..bot_token_env import normalize_discord_token

                token = normalize_discord_token(os.environ.get(token_env, ""))
                adapter = DiscordBotAdapter(
                    agent_id=bot_entry["agent_id"],
                    token=token,
                    channel_id=int(bot_entry["channel_id"]),
                    inbound=bool(bot_entry.get("inbound", False)),
                    token_env=str(token_env),
                )
                bot_manager.register(adapter)

        session = cls(
            server=server,
            agents=agents,
            task=cfg.get("task"),
            max_steps=int(cfg.get("max_steps", 0)),
            bot_manager=bot_manager,
            has_human=has_human,
        )
        session.approvals = approvals
        interact_holder["fn"] = session.has_interact_surface
        request_holder["fn"] = session._on_approval_request
        # v0.7: provider clients are instance-owned (closed at session end)
        session._local_providers.extend(pending_providers)
        session.on_step = on_step
        session.on_tool_event = on_tool_event
        gate_spec = cfg.get("gate")
        if gate_spec:
            session.gate = ConsensusGate(server, thread_name=gate_spec["thread_name"])
            server.subscribe(session.gate.on_message)
            # gate-aware: when gate opens, mark all agents as gate_open=True
            def _on_gate_open() -> None:
                for agent in session.agents:
                    agent.gate_open = True
                    agent.gate_thread_id = session.gate.thread_id
            session.gate.on_open(_on_gate_open)
        # v0.2: P1~P5 collaboration protocol
        protocol_spec = cfg.get("protocol")
        if protocol_spec:
            from ..config import ConfigError
            from .protocol.human_approval import (
                any_human_approval,
                has_discord_inbound,
                normalize_human_approval,
            )

            participant_ids = [a.agent_id for a in agents]
            ha_map = normalize_human_approval(
                protocol_spec, config_error=ConfigError
            )
            protocol_spec["human_approval"] = ha_map
            # D4: interact required when any phase enabled (Ink attaches later;
            # Discord inbound counted here; bypass for demo/tests).
            if (
                any_human_approval(ha_map)
                and not approval_bypass
                and not has_discord_inbound(cfg)
                and not bool(cfg.get("_allow_human_approval_without_interact"))
            ):
                session._human_approval_needs_interact = True
            else:
                session._human_approval_needs_interact = False
            session._human_approval = dict(ha_map)
            for agent in agents:
                agent._human_approval_phases = [
                    p for p, on in ha_map.items() if on
                ]

            session.protocol = CollaborationProtocol(
                server=server,
                participants=protocol_spec.get("participants", participant_ids),
                mode=str(protocol_spec.get("mode", "full")),
            )
            # Wire up gates for each phase
            for phase_name, thread_name in protocol_spec.get("gates", {}).items():
                phase = _phase_from_string(phase_name)
                # What must exist on the thread before votes can open the gate.
                require_proposal, entry_prefix = _PHASE_GATE_ENTRY.get(
                    phase, (False, "")
                )
                await_human = bool(ha_map.get(phase_name, False))
                gate = session.protocol.bind_gate(
                    phase,
                    thread_name,
                    require_proposal=require_proposal,
                    entry_prefix=entry_prefix or "PROPOSE:",
                    await_human_after_agents=await_human,
                )
                if await_human:
                    gate.on_human_pending(
                        lambda g=gate, ph=phase: _publish_human_approval_pending(
                            session, ph, g
                        )
                    )
            # C0a: gate vote snapshots on the Wire bus. Subscribed AFTER every
            # bind_gate() above so each gate has already counted the message
            # we are about to snapshot.
            server.subscribe(lambda _m: _publish_session_gate(session))
            # Auto-advance on gate open
            session.protocol.on_gate_open(
                lambda phase: _on_protocol_gate_open(session, phase)
            )
            # B1: expose phase transitions on the Wire bus for surfaces.
            session.protocol.on_phase_change(
                lambda _frm, to: (
                    _publish_session_phase(session, to),
                    _publish_session_gate(session),
                )
            )
        discord_display = resolve_chat_display_policy(cfg, "discord")
        slack_display = resolve_chat_display_policy(cfg, "slack")
        session.mirror = mirror_from_config(cfg.get("mirror"))
        if session.mirror is not None:
            # M4: Gateway observe surface (replaces server.subscribe(mirror.on_message))
            attach_discord_mirror(
                session.gateway, session.mirror, display=discord_display
            )
        session.slack_mirror = slack_from_config(cfg.get("slack"))
        if session.slack_mirror is not None:
            # M6: Slack Incoming Webhook observe surface
            attach_slack_mirror(
                session.gateway, session.slack_mirror, display=slack_display
            )
        if session.bot_manager is not None:
            # M4: Gateway observe surface (replaces inline route in _on_server_event)
            attach_discord_bots(
                session.gateway, session.bot_manager, display=discord_display
            )
            # M5: opt-in inbound → interact surface + on_message → human.*
            attach_discord_inbound(
                session.gateway, session.bridge, session.bot_manager
            )
        # Subscribe server events to unified output queue (+ Gateway fan-out)
        server.subscribe_events(session._on_server_event)
        return session

    @classmethod
    def open_from_config(
        cls,
        cfg: dict[str, Any],
        *,
        config_path: str | None = None,
        demo: bool = False,
        new_session: bool = False,
        cli_session_id: str | None = None,
        on_step=None,
        on_tool_event=None,
        allowed_roots: list[str] | None = None,
        token_store: TokenStore | None = None,
        on_user_code: Callable[[str, str], None] | None = None,
        approval_bypass: bool = False,
    ) -> Session:
        """Build a Session with optional checkpoint resume (preferred entry)."""
        from .checkpoint import (
            CheckpointStore,
            bootstrap_session,
            config_fingerprint,
            parse_checkpoint_config,
        )

        boot = bootstrap_session(
            cfg,
            config_path=config_path,
            demo=demo,
            new_session=new_session,
            cli_session_id=cli_session_id,
        )
        opts = parse_checkpoint_config(
            cfg,
            demo=demo,
            new_session=new_session,
            cli_session_id=cli_session_id,
        )
        db_path = str(boot.db_path) if boot.enabled else None
        session = cls.from_config(
            cfg,
            on_step=on_step,
            on_tool_event=on_tool_event,
            allowed_roots=allowed_roots,
            token_store=token_store,
            on_user_code=on_user_code,
            approval_bypass=approval_bypass or bool(demo),
            db_path=db_path,
        )
        session.session_id = boot.session_id
        session._checkpoint_enabled = boot.enabled
        session._checkpoint_fingerprint = config_fingerprint(
            cfg, config_path=config_path
        )
        session._flush_debounce_ms = opts.flush_debounce_ms
        session._flush_interval_s = opts.flush_interval_s
        session._approvals_persist = opts.approvals_persist
        session._compact_opts = opts.compact
        session._pending_bootstrap = boot
        if boot.enabled:
            session._checkpoint_store = CheckpointStore(boot.session_dir, boot.session_id)
            session._checkpoint_store.ensure_dir()
            from .external_binding import (
                BINDINGS_FILENAME,
                BindingsSnapshot,
                load_bindings,
            )

            bindings_path = boot.session_dir / BINDINGS_FILENAME
            session._bindings = load_bindings(bindings_path) or BindingsSnapshot()
        return session

    # -- lifecycle -----------------------------------------------------------

    async def _setup(self) -> None:
        """One-time initialization: start bots, bind gates, start protocol.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._setup_done:
            return
        self._setup_done = True

        boot = self._pending_bootstrap
        if boot is not None and boot.resumed:
            await self.server.load()
            self._apply_resume_payload(boot)
            self._resuming = True
            self._publish_resume_events(boot)
        elif boot is not None and boot.resume_failed:
            self._publish_resume_failed(boot.resume_failed)

        # Start unified output consumer task
        self._output_task = asyncio.create_task(self._output_consumer())
        if self._checkpoint_enabled and self._flush_interval_s > 0:
            self._flush_interval_task = asyncio.create_task(self._checkpoint_interval_loop())

        # v0.3: start bots (login to Discord) — same asyncio loop
        if self.bot_manager:
            await self.bot_manager.start_all()

        # gate-aware: inject gate state into agents
        if self.gate:
            if self._resuming and self.gate.thread_id:
                for agent in self.agents:
                    agent.gate_open = self.gate.is_open
                    agent.gate_thread_id = self.gate.thread_id
            else:
                participant_ids = [a.agent_id for a in self.agents]
                tid = await self.server.create_thread(
                    self.gate.thread_name,
                    participants=participant_ids,
                    bootstrap=True,
                )
                self.gate.bind_to_thread(tid)
                for agent in self.agents:
                    agent.gate_open = self.gate.is_open
                    agent.gate_thread_id = self.gate.thread_id

        # v0.2: start the collaboration protocol
        if self.protocol:
            if self._resuming:
                for agent in self.agents:
                    agent.current_phase = self.protocol.phase
                    _inject_protocol_gate_state(agent, self.protocol)
            else:
                for gate in self.protocol._gates.values():
                    if gate is not None:
                        tid = await self.server.create_thread(
                            gate.thread_name,
                            participants=self.protocol.participants,
                            bootstrap=True,
                        )
                        gate.bind_to_thread(tid)
                self.protocol.start()
                for agent in self.agents:
                    agent.current_phase = self.protocol.phase
                    _inject_protocol_gate_state(agent, self.protocol)

        # Always have a durable chat thread for human.send (Discord / Ink mid-run).
        human_tid = await self.ensure_human_thread()
        if self._bindings is not None and self.bridge is not None:
            from .external_binding import apply_to_bridge

            apply_to_bridge(self.bridge, self._bindings)
        if self.bridge is not None and not self.bridge.recent_thread:
            self.bridge._recent_thread = human_tid

        # M4a: re-publish pending approvals after surfaces/bots are up
        if self._resuming and self._restored_pending_approvals:
            self._republish_pending_approvals(self._restored_pending_approvals)
            self._restored_pending_approvals = []

    def _apply_resume_payload(self, boot: Any) -> None:
        """Inject conversations / protocol / inbox / approvals from a checkpoint."""
        conversations = boot.conversations or {}
        for agent in self.agents:
            blob = conversations.get(agent.agent_id) or {}
            conv = blob.get("conversation")
            if isinstance(conv, list) and conv:
                agent.conversation = list(conv)
            threads = blob.get("created_threads")
            if isinstance(threads, list):
                agent.created_threads = list(threads)
            lang = blob.get("language")
            if isinstance(lang, str):
                agent.language = lang
        proto = boot.protocol or {}
        if self.protocol and proto:
            self.protocol.restore(proto)
        if self.gate and isinstance(proto.get("legacy_gate"), dict):
            try:
                self.gate.restore_state(proto["legacy_gate"])
            except KeyError:
                pass
        if boot.inbox:
            self.server.restore_inbox_ids(boot.inbox)
        if boot.meta:
            self._checkpoint_created_at = boot.meta.get("created_at")

        if getattr(boot, "approvals_corrupt", False):
            try:
                from ..gateway.types import make_event

                self.gateway.publish(
                    make_event(
                        "log",
                        text="approvals.json corrupt — pending approvals dropped",
                    )
                )
            except Exception:  # noqa: BLE001, S110
                pass
        elif self._approvals_persist and boot.approvals:
            alive, expired = self.approvals.import_pending(list(boot.approvals))
            self._restored_pending_approvals = list(alive)
            for rec in expired:
                try:
                    from ..gateway.types import make_event

                    self.gateway.publish(
                        make_event(
                            "approval.expired",
                            approval_id=rec.approval_id,
                            agent_id=rec.agent_id,
                            tool=rec.tool,
                        )
                    )
                except Exception:  # noqa: BLE001, S110
                    pass

    def _republish_pending_approvals(self, records: list[Any]) -> None:
        """Re-emit Wire approval.request for restored pending tokens (same ids)."""
        for rec in records:
            try:
                self._on_approval_request(rec)
            except Exception:  # noqa: BLE001, S110
                pass

    def _publish_resume_events(self, boot: Any) -> None:
        from ..gateway.types import make_event

        phase = None
        if self.protocol:
            phase = self.protocol.phase
        try:
            self.gateway.publish(
                make_event(
                    "session.resumed",
                    session_id=self.session_id,
                    phase=phase,
                    agents=[a.agent_id for a in self.agents],
                )
            )
            self.gateway.publish(
                make_event(
                    "log",
                    text=(
                        f"resumed session {self.session_id} "
                        f"(phase={phase or 'n/a'})"
                    ),
                )
            )
        except Exception:  # noqa: BLE001, S110
            pass

    def _publish_resume_failed(self, reason: str) -> None:
        from ..gateway.types import make_event

        try:
            self.gateway.publish(
                make_event("session.resume_failed", reason=reason)
            )
            self.gateway.publish(
                make_event("log", text=f"resume failed — fresh session: {reason}")
            )
        except Exception:  # noqa: BLE001, S110
            pass

    def _maybe_compact_conversations(self) -> list[dict[str, Any]]:
        """Apply M4b rule compact in-place (sync; interrupt/close path)."""
        opts = self._compact_opts
        if opts is None or not getattr(opts, "enabled", False):
            return []
        from .compact import compact_conversation

        phase = self.protocol.phase if self.protocol else ""
        metas: list[dict[str, Any]] = []
        for agent in self.agents:
            new_conv, meta = compact_conversation(
                agent.conversation,
                soft_limit_chars=int(opts.soft_limit_chars),
                keep_tail_chars=int(opts.keep_tail_chars),
                keep_tail_messages=int(opts.keep_tail_messages),
                agent_id=agent.agent_id,
                phase=str(phase or ""),
            )
            if meta is not None:
                agent.conversation = new_conv
                metas.append(meta)
        return metas

    async def _maybe_compact_conversations_async(self) -> list[dict[str, Any]]:
        """M4b/M4e compact; uses LLM when ``compact.llm_summary`` is true."""
        opts = self._compact_opts
        if opts is None or not getattr(opts, "enabled", False):
            return []
        from .compact import compact_conversation_async

        phase = self.protocol.phase if self.protocol else ""
        use_llm = bool(getattr(opts, "llm_summary", False))
        metas: list[dict[str, Any]] = []
        for agent in self.agents:
            new_conv, meta = await compact_conversation_async(
                agent.conversation,
                soft_limit_chars=int(opts.soft_limit_chars),
                keep_tail_chars=int(opts.keep_tail_chars),
                keep_tail_messages=int(opts.keep_tail_messages),
                agent_id=agent.agent_id,
                phase=str(phase or ""),
                llm_summary=use_llm,
                backend=agent.backend if use_llm else None,
            )
            if meta is not None:
                agent.conversation = new_conv
                metas.append(meta)
        return metas

    def _collect_conversations(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for agent in self.agents:
            out[agent.agent_id] = {
                "conversation": list(agent.conversation),
                "created_threads": list(agent.created_threads),
                "language": agent.language,
            }
        return out

    def _collect_protocol_snapshot(self) -> dict[str, Any]:
        snap: dict[str, Any] = {}
        if self.protocol is not None:
            snap = self.protocol.snapshot()
        if self.gate is not None:
            snap["legacy_gate"] = self.gate.snapshot()
        return snap

    def _persist_bindings_sync(self) -> None:
        """Write ``bindings.json`` from bridge + in-memory platform map (A5)."""
        if self._bindings is None or self._checkpoint_store is None:
            return
        from .external_binding import (
            BINDINGS_FILENAME,
            capture_from_bridge,
            merge_snapshots,
            save_bindings,
        )

        if self.bridge is not None:
            cap = capture_from_bridge(self.bridge)
        else:
            from .external_binding import BindingsSnapshot

            cap = BindingsSnapshot()
        self._bindings = merge_snapshots(self._bindings, cap)
        path = self._checkpoint_store.session_dir / BINDINGS_FILENAME
        try:
            save_bindings(path, self._bindings)
        except Exception:  # noqa: BLE001, S110 — never break checkpoint for bindings
            pass

    def flush_checkpoint_sync(
        self,
        *,
        exit_reason: str | None = None,
        skip_compact: bool = False,
        compactions: list[dict[str, Any]] | None = None,
    ) -> None:
        """Best-effort synchronous checkpoint write (interrupt / close)."""
        if not self._checkpoint_enabled or self._checkpoint_store is None:
            return
        from .checkpoint import write_latest

        self._persist_bindings_sync()

        reason = exit_reason or self._exit_reason
        phase = self.protocol.phase if self.protocol else None
        if compactions is None:
            compactions = []
            if not skip_compact:
                compactions = self._maybe_compact_conversations()
        approvals = (
            self.approvals.export_pending() if self._approvals_persist else []
        )
        try:
            self._checkpoint_store.save(
                fingerprint=self._checkpoint_fingerprint,
                conversations=self._collect_conversations(),
                protocol=self._collect_protocol_snapshot(),
                inbox=self.server.export_inbox_ids(),
                exit_reason=reason,
                phase=phase,
                created_at=self._checkpoint_created_at,
                approvals=approvals,
                compactions=compactions,
                pending_approvals=len(approvals),
            )
            write_latest(
                self._checkpoint_store.session_dir.parent,
                self.session_id or self._checkpoint_store.session_id,
                self._checkpoint_fingerprint,
            )
            if self._checkpoint_created_at is None:
                self._checkpoint_created_at = __import__("time").time()
        except Exception as exc:  # noqa: BLE001 — never break Core for checkpoint
            try:
                from ..gateway.types import make_event

                self.gateway.publish(
                    make_event("log", text=f"checkpoint save failed: {exc}")
                )
            except Exception:  # noqa: BLE001, S110
                pass

    async def flush_checkpoint(self, *, exit_reason: str | None = None) -> None:
        async with self._checkpoint_lock:
            # Prefer async compact (M4e LLM) then write without re-compacting.
            metas = await self._maybe_compact_conversations_async()
            self.flush_checkpoint_sync(
                exit_reason=exit_reason,
                skip_compact=True,
                compactions=metas,
            )
            if self._checkpoint_enabled:
                try:
                    from ..gateway.types import make_event

                    self.gateway.publish(
                        make_event(
                            "session.checkpoint",
                            session_id=self.session_id,
                        )
                    )
                except Exception:  # noqa: BLE001, S110
                    pass

    def schedule_checkpoint(self) -> None:
        """Debounced async checkpoint (after steps)."""
        if not self._checkpoint_enabled:
            return
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()

        async def _debounced() -> None:
            try:
                delay = max(0, self._flush_debounce_ms) / 1000.0
                if delay:
                    await asyncio.sleep(delay)
                await self.flush_checkpoint()
            except asyncio.CancelledError:
                return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush_checkpoint_sync()
            return
        self._flush_task = loop.create_task(_debounced())

    async def _checkpoint_interval_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self._flush_interval_s)
                if self._closed:
                    break
                await self.flush_checkpoint()
        except asyncio.CancelledError:
            return

    # -- lifecycle (run) -----------------------------------------------------

    async def ensure_human_thread(self) -> str:
        """Create or reuse the ``human`` collaboration thread (all agents)."""
        participants = [a.agent_id for a in self.agents]
        return await self.server.create_thread(
            HUMAN_CHAT_THREAD_NAME,
            participants=participants,
            bootstrap=True,
        )

    async def human_send(
        self,
        thread_id: str,
        content: str,
        *,
        mentions: list[str] | None = None,
        source: dict[str, Any] | None = None,
    ) -> str:
        """Inject a message from the human participant into the session.

        Convenience passthrough to ``server.human_send`` with ``author="human"``.
        Raises if no human is configured (``has_human`` is False). The reply is
        pushed to agent inboxes and absorbed as a ``[radio]`` block on their
        next ``step()``.

        If *thread_id* is unknown (agents often invent ids for ``ask_user``),
        resolve by name or fall back to the durable ``human`` chat thread.
        """
        if not self.has_human:
            raise RuntimeError("human-in-the-loop is not enabled (no 'human:' section in config)")
        resolved = self.server.resolve_thread_id(thread_id)
        if resolved is None:
            resolved = await self.ensure_human_thread()
            if self.bridge is not None:
                self.bridge._recent_thread = resolved
        if self._bindings is not None:
            from .external_binding import record_platform_thread

            record_platform_thread(self._bindings, source, resolved)
        return await self.server.human_send(
            resolved,
            author="human",
            content=content,
            mentions=mentions,
            source=source,
        )

    def lookup_external_thread(self, source: dict[str, Any] | None) -> str | None:
        """Resolve augury ``thread_id`` from a platform ``source`` (A5)."""
        if self._bindings is None:
            return None
        from .external_binding import lookup_platform_thread

        return lookup_platform_thread(self._bindings, source)

    def request_interrupt(self) -> None:
        """Ask the current ``run()`` to stop (Ctrl+C / quit while agents work).

        Sets a cooperative flag and cancels in-flight agent tasks so a stuck
        ``step()`` (model HTTP / long tool) can unwind. Safe to call when no
        run is active. Cleared automatically at the start of the next ``run()``.
        """
        self._exit_reason = "interrupted"
        self._interrupt.set()
        for task in list(self._agent_tasks):
            if not task.done():
                task.cancel()
        self.flush_checkpoint_sync(exit_reason="interrupted")

    def interrupted(self) -> bool:
        """True if ``request_interrupt()`` was called for the current/last run."""
        return self._interrupt.is_set()

    async def flush_observers(self) -> None:
        """Flush Discord/Slack observe outboxes (best-effort)."""
        if self.bot_manager is not None:
            flush = getattr(self.bot_manager, "flush", None)
            if flush is not None:
                await flush()
        if self.mirror is not None:
            await self.mirror.flush()
        if self.slack_mirror is not None:
            await self.slack_mirror.flush()

    async def run(self, initial_prompt: str | None = None) -> int:
        """Parallel steps until every agent finishes or max_steps is hit.

        Each agent runs as an independent ``asyncio.Task``. All agents share
        a global step budget (``max_steps``); the sum of every agent's steps
        is capped. An agent finishes when it produces neither text nor tool
        calls and has no pending inbox messages — same rule as the prior
        round-robin loop.

        Output events (step summaries, tool calls) are pushed to the unified
        ``_output_queue``; the single consumer task renders them in arrival
        order, so tool logs stream in the order they actually fire.

        v0.3: bots are started (Discord login) at the beginning of the session
        and stopped (connection cleanup) at the end, in the same asyncio loop.

        Reusable: call run() multiple times to continue the conversation.
        The first call initializes bots/gates/protocol; subsequent calls
        reuse them. Call close() when done to release resources.

        ``request_interrupt()`` stops a run early; the next ``run()`` starts clean.
        """
        await self._setup()
        self._enforce_human_approval_interact()
        return await self._run_impl(initial_prompt)

    def _enforce_human_approval_interact(self) -> None:
        """D4: human_approval requires Ink/Discord interact (or test bypass)."""
        if not self._human_approval_needs_interact:
            return
        if self.has_interact_surface():
            return
        from ..config import ConfigError

        raise ConfigError(
            "protocol.human_approval requires an interact surface "
            "(Ink UI or bots[].inbound: true)"
        )

    async def _run_impl(self, initial_prompt: str | None = None) -> int:
        """Core run logic (separated so start/stop wraps it cleanly)."""
        self._interrupt.clear()

        # Broadcast the initial task to ALL agents (not just agents[0]), so
        # every worker gets the same user prompt and acts on it per its role.
        user_text = initial_prompt or self.task or ""
        if user_text:
            for agent in self.agents:
                agent.conversation.append({"role": "user", "content": user_text})

        # v0.3: detect user language from initial prompt → inject into all agents
        from .agent.system_prompt import detect_language
        detected_lang = detect_language(user_text)
        if detected_lang:
            for agent in self.agents:
                agent.language = detected_lang

        # Global step counter. asyncio is single-threaded, so += is atomic;
        # the cap is checked at the top of each agent loop iteration.
        # Use a one-element list so park wakeup closures see live updates (B023).
        total_steps = [0]

        async def run_agent(agent: AgentLoop) -> None:
            """Run one agent's step loop as long as it makes progress and the
            global budget allows."""
            idle_streak = 0
            while True:
                if self._interrupt.is_set():
                    break

                # Global budget gate — checked before every step.
                if self.max_steps and total_steps[0] >= self.max_steps:
                    break

                # B: protocol terminal — end run() without waiting on idle text.
                if self.protocol and self.protocol.phase in (COMPLETED, REJECTED):
                    break

                # Inject current gate state before each step.
                if self.gate:
                    agent.gate_open = self.gate.is_open
                    agent.gate_thread_id = self.gate.thread_id
                    agent.gate_thread_name = self.gate.thread_name
                # v0.2: inject current protocol phase + gate state.
                if self.protocol:
                    agent.current_phase = self.protocol.phase
                    _inject_protocol_gate_state(agent, self.protocol)
                    # Done-set: waiting at a gate AND this agent already
                    # signalled. Computed here so the loop never re-derives it.
                    agent.protocol_done = (
                        self._is_gate_waiting()
                        and self.protocol.is_agent_done(agent.agent_id)
                    )
                # V1 relevance budget: inject phase_floor for attention decisions
                # (§5.3) near_gate / P1 READY pending / default
                agent.phase_floor = _compute_phase_floor(agent, self.protocol)

                # C2: this agent already signalled and the gate has not moved —
                # park without a model call. Must sit at the TOP of the loop so
                # a D5 `skipped` continue lands here instead of on step().
                if agent.protocol_done and self.server.inbox_size(agent.agent_id) == 0:
                    woke = await self._wait_for_gate_wakeup(
                        agent, steps_done=lambda: total_steps[0]
                    )
                    if not woke:
                        break
                    continue

                try:
                    result = await agent.step()
                except asyncio.CancelledError:
                    # request_interrupt() cancelled this task mid-step.
                    break
                except IndexError:
                    # Script exhausted — agent has no more completions.
                    # This is a normal finish, not an error.
                    break
                except Exception as exc:  # noqa: BLE001 — single agent failure
                    # Agent step failed — mark as finished so the session
                    # continues with remaining agents instead of aborting.
                    print(
                        f"  [{agent.agent_id}] step failed: {exc}",
                        flush=True,
                    )
                    _publish_session_error(
                        self,
                        f"[{agent.agent_id}] step failed: {exc}",
                        agent_id=agent.agent_id,
                    )
                    break

                # V1 relevance budget: T0 ignore — drain 됐지만 complete 생략됨.
                # step counter를 증가시키지 않고 다음 iteration으로 진행한다.
                if getattr(result, "skipped", False):
                    await asyncio.sleep(0)
                    continue

                # Increment step counter only after a successful step.
                total_steps[0] += 1
                self.schedule_checkpoint()

                # Step summary queued for display.
                await self._output_queue.put({
                    "type": "step",
                    "agent_id": agent.agent_id,
                    "result": result,
                    "timestamp": __import__("time").time(),
                })

                has_pending = self.server.inbox_size(agent.agent_id) > 0

                if result.tool_calls:
                    idle_streak = 0
                    await asyncio.sleep(0)
                    continue

                if has_pending:
                    idle_streak = 0
                    await asyncio.sleep(0)
                    continue

                if result.drained_count:
                    idle_streak = 0

                # Gate-wait park: stay silent until inbox / phase / gate opens.
                if self._is_gate_waiting():
                    # P1: remind once if this agent forgot READY: before parking.
                    if self._maybe_nudge_ready(agent):
                        await asyncio.sleep(0)
                        continue
                    # P2+: remind once with the concrete gate thread id.
                    if self._maybe_nudge_gate_thread(agent):
                        await asyncio.sleep(0)
                        continue
                    woke = await self._wait_for_gate_wakeup(
                        agent, steps_done=lambda: total_steps[0]
                    )
                    if woke:
                        continue
                    break

                # Legacy finish + D′ (text idle streak, non-gate-wait only).
                if result.text is None:
                    break
                idle_streak += 1
                if idle_streak >= 2:
                    break
                await asyncio.sleep(0)

        # Launch all agents as parallel asyncio tasks.
        tasks = [asyncio.create_task(run_agent(agent)) for agent in self.agents]
        self._agent_tasks = tasks
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._agent_tasks = []

        return total_steps[0]

    def _is_gate_waiting(self) -> bool:
        """True while protocol is blocked on READY (P1) or a closed phase gate."""
        protocol = self.protocol
        if protocol is None:
            return False
        phase = protocol.phase
        if phase in (COMPLETED, REJECTED):
            return False
        if phase == P1_EXPLORE:
            return True
        gate = protocol.gate_for(phase)
        if gate is None:
            return False
        return not gate.is_open

    def _maybe_nudge_ready(self, agent: AgentLoop) -> bool:
        """Inject a one-shot READY reminder for P1 agents that forgot to signal.

        Returns True if a nudge was injected (caller should step again).
        """
        protocol = self.protocol
        if protocol is None or protocol.phase != P1_EXPLORE:
            return False
        if protocol.has_ready(agent.agent_id):
            return False
        if agent.agent_id in self._ready_nudged:
            return False
        self._ready_nudged.add(agent.agent_id)
        human_tid = self.server.resolve_thread_id(HUMAN_CHAT_THREAD_NAME)
        where = (
            f" on thread '{human_tid}' (name={HUMAN_CHAT_THREAD_NAME!r})"
            if human_tid
            else ""
        )
        agent.conversation.append(
            {
                "role": "user",
                "content": (
                    "[protocol] You have not sent READY: yet. "
                    f"Call send_message{where} with content starting with READY: "
                    "(e.g. READY: or READY: done) to finish P1 exploration. "
                    "Do not create_thread — reuse the open session threads."
                ),
            }
        )
        return True

    def _maybe_nudge_gate_thread(self, agent: AgentLoop) -> bool:
        """Inject a one-shot reminder of the bound gate thread for P2+ phases.

        Returns True if a nudge was injected (caller should step again).
        """
        protocol = self.protocol
        if protocol is None:
            return False
        phase = protocol.phase
        if phase in (P1_EXPLORE, COMPLETED, REJECTED):
            return False
        gate = protocol.gate_for(phase)
        if gate is None or not gate.thread_id or gate.is_open:
            return False
        key = (agent.agent_id, phase)
        if key in self._gate_thread_nudged:
            return False
        self._gate_thread_nudged.add(key)
        agent.conversation.append(
            {
                "role": "user",
                "content": (
                    f"[protocol] Phase is {phase}. "
                    f"The gate thread id is '{gate.thread_id}' "
                    f"(name={gate.thread_name!r}). "
                    "Send PROPOSE:/APPROVE: on that thread only. "
                    f"Do not create another thread named {gate.thread_name!r}."
                ),
            }
        )
        return True

    async def _wait_for_gate_wakeup(
        self,
        agent: AgentLoop,
        *,
        steps_done: Callable[[], int] | None = None,
    ) -> bool:
        """Park until inbox, phase/gate change, interrupt, or step budget.

        Returns True to step again; False to exit the agent loop.
        Does **not** end the turn merely because every agent is idle.
        """
        protocol = self.protocol
        phase0 = protocol.phase if protocol is not None else None
        gate0_open = False
        if protocol is not None:
            gate = protocol.gate_for(protocol.phase)
            gate0_open = bool(gate and gate.is_open)

        while True:
            if self._interrupt.is_set() or self._closed:
                return False
            if (
                self.max_steps
                and steps_done is not None
                and steps_done() >= self.max_steps
            ):
                return False
            if self.server.inbox_size(agent.agent_id) > 0:
                return True
            if protocol is not None:
                if protocol.phase != phase0:
                    return True
                gate = protocol.gate_for(protocol.phase)
                now_open = bool(gate and gate.is_open)
                if now_open and not gate0_open:
                    return True
                if not self._is_gate_waiting():
                    return True
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                return False

    async def close(self) -> None:
        """Release resources: stop bots, close mirror, close backends.

        Call when the session is no longer needed. Safe to call multiple
        times — subsequent calls are no-ops.
        """
        if self._closed:
            return
        self._closed = True
        if self._exit_reason is None:
            self._exit_reason = "quit"
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
        if self._flush_interval_task and not self._flush_interval_task.done():
            self._flush_interval_task.cancel()
        try:
            await self.flush_checkpoint(exit_reason=self._exit_reason)
        except Exception:  # noqa: BLE001 — never block shutdown on checkpoint
            self.flush_checkpoint_sync(
                exit_reason=self._exit_reason, skip_compact=True
            )

        # Shutdown unified output consumer.
        await self._output_queue.put(None)
        if self._output_task:
            await self._output_task

        # v0.3: stop bots (close Discord connections, prevent leaks)
        if self.bot_manager:
            await self.bot_manager.stop_all()

        if self.mirror is not None:
            await self.mirror.aclose()
        if self.slack_mirror is not None:
            await self.slack_mirror.aclose()
        # v0.7: close local-tool providers (web_search HTTP clients)
        for provider in self._local_providers:
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()
        for agent in self.agents:
            aclose = getattr(agent.backend, "aclose", None)
            if aclose is not None:
                await aclose()
        close = getattr(self.server, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result

    def _on_server_event(self, event: dict[str, Any]) -> None:
        """Capture server events and queue them for unified output.

        M4: Discord bots/mirror observe via Gateway (publish Wire first).
        """
        # Fan-out to Gateway surfaces (Ink later; Discord observe now).
        try:
            self.bridge.publish_core_event(event)
        except Exception:  # noqa: BLE001, S110 — never break Core for Wire
            pass

        event_type = event["type"]
        if event_type == "tool":
            import json

            tool = event.get("tool", "")
            result_str = event.get("result", "")
            # v1.4 #3: run_command 성공 + 짧은 출력이면 로그 스킵 (노이즈 감소)
            if tool == "run_command":
                try:
                    parsed = json.loads(result_str) if isinstance(result_str, str) else result_str
                    if isinstance(parsed, dict) and parsed.get("exit_code") == 0 \
                            and len(str(parsed.get("stdout", ""))) < 200 \
                            and not str(parsed.get("stderr", "")).strip():
                        return
                except Exception:  # noqa: BLE001, S110 — best-effort parse for log skip
                    pass
            # v1.4 #8: gate_closed -> protocol violation 태깅
            is_violation = False
            violation_msg = ""
            phase = "?"
            try:
                parsed = json.loads(result_str) if isinstance(result_str, str) else result_str
                if isinstance(parsed, dict) and parsed.get("error") == "gate_closed":
                    is_violation = True
                    violation_msg = parsed.get("message", "")
                    phase = parsed.get("phase", "?")
            except Exception:  # noqa: BLE001, S110 — best-effort parse for violation tag
                pass
            try:
                self._output_queue.put_nowait({
                    "type": "tool",
                    "agent_id": event["agent_id"],
                    "tool": event["tool"],
                    "args": event.get("args", {}),
                    "result": result_str,
                    "protocol_violation": is_violation,
                    "violation_message": violation_msg,
                    "phase": phase,
                    "timestamp": event.get("timestamp", __import__("time").time()),
                })
            except asyncio.QueueFull:
                pass
        elif event_type == "read_resource":
            try:
                self._output_queue.put_nowait({
                    "type": "read_resource",
                    "agent_id": event["agent_id"],
                    "threads": event["threads"],
                    "messages": event["messages"],
                    "timestamp": event.get("timestamp", __import__("time").time()),
                })
            except asyncio.QueueFull:
                pass
        elif event_type == "create_thread":
            try:
                payload: dict[str, Any] = {
                    "type": "create_thread",
                    "thread_id": event["thread_id"],
                    "name": event["name"],
                    "participants": event["participants"],
                    "timestamp": event.get("timestamp", __import__("time").time()),
                }
                if event.get("bootstrap"):
                    payload["bootstrap"] = True
                if event.get("reused"):
                    payload["reused"] = True
                self._output_queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass
        elif event_type == "send_message":
            try:
                self._output_queue.put_nowait({
                    "type": "send_message",
                    "message_id": event["message_id"],
                    "thread_id": event["thread_id"],
                    "author": event["author"],
                    "content": event["content"],
                    "delivered_to": event.get("delivered_to", []),
                    "timestamp": event.get("timestamp", __import__("time").time()),
                })
            except asyncio.QueueFull:
                pass

    async def _output_consumer(self) -> None:
        """Consume output events from queue and emit to callbacks."""
        while True:
            event = await self._output_queue.get()
            if event is None:
                break
            event_type = event.get("type")
            if event_type == "step":
                if self.on_step is not None:
                    self.on_step(event["agent_id"], event["result"])
            elif event_type in ("tool", "read_resource", "create_thread", "send_message") and self.on_tool_event is not None:
                self.on_tool_event(event)


def _resolve_role_prompt(spec: dict[str, Any], roles: dict[str, Any] | None) -> str:
    """Resolve the role prompt for an agent spec.

    - ``role``: looks up the preset in ``roles`` and returns its ``prompt``.
    - ``role_custom``: returns the inline string directly.
    - neither: returns "" (no role injected).
    """
    role = spec.get("role")
    role_custom = spec.get("role_custom")
    if role_custom is not None:
        return role_custom
    if role is not None and roles is not None:
        preset = roles.get(role, {})
        return preset.get("prompt", "")
    return ""


def _phase_from_string(name: str) -> Phase:
    """Convert a phase string to a Phase constant."""
    mapping = {
        "P1_EXPLORE": P1_EXPLORE,
        "P2_SPLIT": P2_SPLIT,
        "P3_EXECUTE": P3_EXECUTE,
        "P4_REVIEW": P4_REVIEW,
        "P5_SUBMIT": P5_SUBMIT,
    }
    if name not in mapping:
        raise ValueError(f"unknown phase: {name!r}")
    return mapping[name]


# PHASE_ENTRY_SIGNAL_DESIGN §3.3 — the signal each phase's gate waits for.
# P3/P4 stay free-form: the first APPROVE: doubles as the proposal.
_PHASE_GATE_ENTRY: dict[Phase, tuple[bool, str]] = {
    P2_SPLIT: (True, "PROPOSE:"),
    P3_EXECUTE: (False, ""),
    P4_REVIEW: (False, ""),
    P5_SUBMIT: (True, "FINAL:"),
}


def _on_protocol_gate_open(session: Session, phase: Phase) -> None:
    """Handle gate open events from the collaboration protocol."""
    # Auto-advance to the next phase when a gate opens (mode-aware).
    if session.protocol is None:
        return
    # T0: open snapshot before advance (COMPLETED has no gate_for → UI stuck at N-1/N).
    _publish_session_gate_for_phase(session, phase)
    next_phase = session.protocol.next_phase_after_gate(phase)
    if next_phase:
        session.protocol.advance(next_phase)


def _gate_wire_payload(phase: Phase, gate: ConsensusGate) -> dict[str, Any]:
    return {
        "type": "session.gate",
        "phase": phase,
        "thread_id": gate.thread_id,
        "thread_name": gate.thread_name,
        "approvals": sorted(gate.approvals),
        "pending": sorted(set(gate.participants) - gate.approvals),
        "open": gate.is_open,
        "has_proposal": gate.has_proposal,
        "require_proposal": gate.require_proposal,
        "entry_prefix": gate.entry_prefix,
        "human_pending": gate.human_pending,
    }


def _publish_session_gate_for_phase(session: Session, phase: Phase) -> None:
    """Emit ``session.gate`` for a specific phase (e.g. open snapshot before advance)."""
    protocol = session.protocol
    if protocol is None:
        return
    gate = protocol.gate_for(phase)
    if gate is None:
        return
    try:
        session.bridge.publish_core_event(_gate_wire_payload(phase, gate))
    except Exception:  # noqa: BLE001, S110 — never break Core for Wire
        pass


def _publish_session_gate(session: Session) -> None:
    """C0a: emit ``session.gate`` so surfaces can show why a gate is closed."""
    protocol = session.protocol
    if protocol is None:
        return
    gate = protocol.gate_for(protocol.phase)
    if gate is None:
        return
    try:
        session.bridge.publish_core_event(
            _gate_wire_payload(protocol.phase, gate)
        )
    except Exception:  # noqa: BLE001, S110 — never break Core for Wire
        pass


def _publish_session_phase(session: Session, phase: Phase) -> None:
    """B1: emit Wire ``session.phase`` when the collaboration protocol advances."""
    try:
        session.bridge.publish_core_event({"type": "session.phase", "phase": phase})
    except Exception:  # noqa: BLE001, S110 — never break Core for Wire
        pass


def _publish_human_approval_pending(
    session: Session, phase: Phase, gate: ConsensusGate
) -> None:
    """D5: agents agreed — wait for human APPROVE:/REJECT:."""
    from ..gateway.types import make_event

    try:
        session.gateway.publish(
            make_event(
                "session.human_approval_pending",
                phase=str(phase),
                thread_id=gate.thread_id,
                text=(
                    f"Agents reached consensus on {phase} — "
                    "reply APPROVE: or REJECT: to continue"
                ),
            )
        )
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        session.bridge.publish_core_event(
            {
                "type": "log",
                "text": (
                    f"[human_approval] {phase}: agent consensus — "
                    "waiting for human APPROVE:/REJECT:"
                ),
            }
        )
    except Exception:  # noqa: BLE001, S110
        pass


def _publish_session_error(
    session: Session,
    message: str,
    *,
    agent_id: str | None = None,
) -> None:
    """B2: emit Wire ``error`` for Core step / session failures."""
    event: dict[str, Any] = {"type": "error", "message": message}
    if agent_id:
        event["agent_id"] = agent_id
    try:
        session.bridge.publish_core_event(event)
    except Exception:  # noqa: BLE001, S110 — never break Core for Wire
        pass


def _compute_phase_floor(agent, protocol: CollaborationProtocol | None) -> float:
    """Compute the V1 relevance-budget floor for the agent's current context.

    DESIGN.md §5.3 / §4.3: floor keys are conditions, not phase names:

    - ``near_gate`` — closed gate with ≥1 approval (consensus window warming up).
      Not simply ``not gate.is_open``: that would ban T0 for all of P2–P5 and
      contradict §9 scenario A (P3 broadcast T0 savings).
    - ``p1_ready_pending`` — P1 + this agent has not submitted READY/REJECT.
    - ``default`` — everything else (incl. open gates / empty approval sets).

    Returns ``0.0`` when protocol/attention is absent.
    """
    if protocol is None:
        return 0.0

    floors = {}
    if agent._attention_policy is not None and agent._attention_config:
        floors = agent._attention_config.get("floors", {}) or {}

    phase = protocol.phase
    gate = protocol.gate_for(phase)

    # near_gate first (mutually exclusive with P1 — P1 has no gate; G6).
    if (
        gate is not None
        and not gate.is_open
        and len(gate.approvals) >= 1
    ):
        return float(floors.get("near_gate", floors.get("default", 0.0)))

    # P1: proposal pending and I have not decided → attention required (T0 banned).
    if phase == P1_EXPLORE and not protocol.has_ready(agent.agent_id):
        return float(floors.get("p1_ready_pending", floors.get("default", 0.0)))

    return float(floors.get("default", 0.0))


def _inject_protocol_gate_state(agent, protocol: CollaborationProtocol) -> None:
    """Inject the current phase's gate state into an agent.

    When a phase has a gate, the agent's gate_open/gate_thread_id reflect
    that gate's state. When the gate is closed, work-share on non-gate
    threads is blocked.

    P1_EXPLORE: gate_open=False, gate_thread_id=None — only READY: messages
    are allowed (to finish exploration). All other send_message calls are
    blocked by the gate-closed logic in AgentLoop._execute_tool.
    """
    gate = protocol.gate_for(protocol.phase)
    if gate is not None:
        agent.gate_open = gate.is_open
        agent.gate_thread_id = gate.thread_id
        agent.gate_thread_name = gate.thread_name
        agent.gate_approvals = gate.approvals
        agent.ready_states = protocol.ready_states
        agent.gate_entry_prefix = gate.entry_prefix
        agent.gate_needs_signal = (
            gate.entry_prefix
            if (gate.require_proposal and not gate.has_proposal)
            else None
        )
    elif protocol.phase == P1_EXPLORE:
        # P1: only READY: messages allowed to finish exploration
        agent.gate_open = False
        agent.gate_thread_id = None
        agent.gate_thread_name = None
        agent.gate_approvals = frozenset()
        agent.ready_states = protocol.ready_states
        agent.gate_entry_prefix = None
        agent.gate_needs_signal = None
    else:
        # Other phases without a gate — no restriction
        agent.gate_open = True
        agent.gate_thread_id = None
        agent.gate_thread_name = None
        # Reset both: a stale approvals view from the previous phase would
        # soft-block a legitimate vote here.
        agent.gate_approvals = frozenset()
        agent.ready_states = frozenset()
        agent.gate_entry_prefix = None
        agent.gate_needs_signal = None
