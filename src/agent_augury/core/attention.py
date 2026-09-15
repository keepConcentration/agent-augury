"""Agent attention / relevance budget policy (AGENT_RELEVANCE_BUDGET_DESIGN.md).

V1: heuristic weighted-sum relevance scoring with tier-based budget decisions.

Features (weighted sum, clipped to [0, 1]):
- F1: explicit mention in ``message["mentions"]``
- F2: receiver is a participant of the message's thread (requires server ref)
- F4: recent interaction between receiver and message author (requires server ref)

F3 (phase_role) is **removed** in V1 — assembler semantics expressed via
per-agent ``attention.floor`` override (§4.3 불변식 4, 리뷰 §8 Q1).

Human messages (author ``"human"``) always score 1.0 (리뷰 P0-2).

Contract with ``AgentLoop.step()`` (core/agent/loop.py):
  scores = policy.score_batch(agent_id, drained, phase=phase)
  decision = policy.decide(agent_id, scores, phase=phase,
                           phase_floor=pf, agent_floor=af)
  decision.run_llm         # bool — T0 == False
  decision.tier            # "ignore" | "skim" | "engage" | "intervene"
  decision.context_max_chars  # int | None — for skim truncation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Message = dict[str, Any]

Tier = Literal["ignore", "skim", "engage", "intervene"]


# ── public data classes ────────────────────────────────────────────────────


@dataclass(frozen=True)
class AttentionScores:
    """Per-message and per-agent relevance scores for one drained batch.

    ``by_message`` keys are ``message_id`` strings; ``by_agent`` keys are
    agent ids.  Both values are floats in [0, 1].
    """

    by_agent: dict[str, float] = field(default_factory=dict)
    by_message: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class BudgetDecision:
    """Single budget decision for a batch of drained messages.

    V1 semantics:
    - ``run_llm`` is ``False`` **only** for ``tier == "ignore"`` (V1.1 may
      add edge cases).
    - ``max_tokens`` is always ``None`` (V1.1 — backend API gap).
    - ``tools_allowed`` is always ``True`` (V1.1 may cap by tier).
    - ``context_max_chars`` is ``None`` for engage/intervene (unlimited);
      set to ``skim_max_chars`` for skim; ``None`` for ignore.
    """

    tier: Tier
    r: float
    run_llm: bool = True
    max_tokens: int | None = None
    context_max_chars: int | None = None
    tools_allowed: bool = True


# ── default constants (mirrors config._ATTENTION_DEFAULTS §6) ──────────────

_DEFAULT_FEATURES: dict[str, float] = {
    "mention_boost": 0.5,
    "thread_participant": 0.25,
    "recent_interact": 0.2,
}

_DEFAULT_TIERS: dict[str, float] = {
    "ignore": 0.15,
    "skim": 0.45,
    "engage": 0.80,
}

_DEFAULT_CONTEXT: dict[str, Any] = {
    "skim_max_chars": 400,
    "t0_digest": False,
}

# F4: lookback window (number of *overall* messages to scan backwards).
_RECENT_INTERACT_WINDOW = 10


# ── policy ─────────────────────────────────────────────────────────────────


class RelevancePolicy:
    """Heuristic relevance scoring + budget tier decision (V1).

    Parameters:
        features: weight dict — ``mention_boost``, ``thread_participant``,
            ``recent_interact``.  Missing keys filled from defaults.
        tiers: threshold dict — ``ignore``, ``skim``, ``engage``.  Must
            satisfy ``ignore <= skim <= engage``.  Missing keys filled
            from defaults.
        context: ``skim_max_chars`` (int), ``t0_digest`` (bool).
        server: optional ``MessageServer`` reference for F2/F4 lookups.
            When ``None``, F2 and F4 always return 0.
    """

    def __init__(
        self,
        features: dict[str, float] | None = None,
        tiers: dict[str, float] | None = None,
        context: dict[str, Any] | None = None,
        *,
        server: Any = None,
    ) -> None:
        f = {**_DEFAULT_FEATURES, **(features or {})}
        self._mention_boost = float(f["mention_boost"])
        self._thread_participant = float(f["thread_participant"])
        self._recent_interact = float(f["recent_interact"])

        t = {**_DEFAULT_TIERS, **(tiers or {})}
        self._tier_ignore = float(t["ignore"])
        self._tier_skim = float(t["skim"])
        self._tier_engage = float(t["engage"])

        # Monotonicity invariant (design §6.2, enforced by config.py;
        # duplicated here for programmatic construction safety).
        if not (self._tier_ignore <= self._tier_skim <= self._tier_engage):
            raise ValueError(
                f"tier thresholds must be monotonic: "
                f"ignore={self._tier_ignore} skim={self._tier_skim} "
                f"engage={self._tier_engage}"
            )

        ctx = {**_DEFAULT_CONTEXT, **(context or {})}
        self._skim_max_chars: int = int(ctx["skim_max_chars"])
        self._t0_digest: bool = bool(ctx["t0_digest"])

        self._server = server

    # -- public API (called by AgentLoop.step) -------------------------------

    def score_batch(
        self,
        receiver_id: str,
        drained: list[Message],
        *,
        phase: str = "",
    ) -> AttentionScores:
        """Score every message in *drained*.

        Caller uses ``max(by_message.values())`` for ``decide()``
        (design §4.3 불변식 3 — batch r = max).
        """
        by_message: dict[str, float] = {}
        by_agent: dict[str, float] = {}
        for msg in drained:
            msg_id = msg.get("message_id", "")
            r = self.score_message(receiver_id, msg, phase=phase)
            if msg_id:
                by_message[msg_id] = r
            author = msg.get("author", "")
            if author:
                by_agent[author] = max(by_agent.get(author, 0.0), r)
        return AttentionScores(by_agent=by_agent, by_message=by_message)

    def score_message(
        self,
        receiver_id: str,
        message: Message,
        *,
        phase: str = "",
    ) -> float:
        """Compute r ∈ [0, 1] for one message from *receiver_id*'s perspective.

        Human messages always return 1.0 (리뷰 P0-2).
        """
        author = message.get("author", "")

        # P0-2: human messages always engage+
        if author and author.lower() == "human":
            return 1.0

        # G4: URGENT prefix forces intervene (r=1.0, 설계문서 §4.2 불변식 2)
        content = message.get("content", "")
        if isinstance(content, str) and content.strip().upper().startswith("URGENT"):
            return 1.0

        score = 0.0

        # F1 — explicit mention (보장: 멘션만으로도 최소 T2 engage)
        mentions: list[str] = message.get("mentions") or []
        if receiver_id in mentions:
            score += self._mention_boost
            score = max(score, self._tier_skim)

        # F2 — thread participant
        if self._server is not None:
            thread_id = message.get("thread_id", "")
            if thread_id:
                try:
                    thread = self._server.get_thread(thread_id)
                    participants: list[str] = thread.get("participants", [])
                    if receiver_id in participants:
                        score += self._thread_participant
                except (KeyError, AttributeError):
                    pass

        # F4 — recent interaction (receiver ↔ sender)
        if self._server is not None and author and author != receiver_id:
            try:
                if self._has_recent_interact(receiver_id, author):
                    score += self._recent_interact
            except Exception:  # noqa: BLE001, S110 — best-effort feature
                pass

        return max(0.0, min(1.0, score))

    def decide(
        self,
        receiver_id: str,
        scores: AttentionScores,
        *,
        phase: str = "",
        phase_floor: float = 0.0,
        agent_floor: float = 0.0,
    ) -> BudgetDecision:
        """Compute ``r_final = max(max(by_message), phase_floor, agent_floor)``
        and map to a budget tier (§4.3 불변식 3).
        """
        # Prefer by_message; fall back to by_agent if ids were missing.
        if scores.by_message:
            r_max = max(scores.by_message.values())
        elif scores.by_agent:
            r_max = max(scores.by_agent.values())
        else:
            r_max = 0.0
        r_final = max(r_max, phase_floor, agent_floor)
        r_final = max(0.0, min(1.0, r_final))

        if r_final < self._tier_ignore:
            return BudgetDecision(
                tier="ignore",
                r=r_final,
                run_llm=False,
                context_max_chars=None,
            )
        elif r_final < self._tier_skim:
            return BudgetDecision(
                tier="skim",
                r=r_final,
                run_llm=True,
                context_max_chars=self._skim_max_chars,
            )
        elif r_final < self._tier_engage:
            return BudgetDecision(
                tier="engage",
                r=r_final,
                run_llm=True,
                context_max_chars=None,  # V1: unlimited
            )
        else:
            return BudgetDecision(
                tier="intervene",
                r=r_final,
                run_llm=True,
                context_max_chars=None,
            )

    # -- internals -----------------------------------------------------------

    def _has_recent_interact(self, agent_a: str, agent_b: str) -> bool:
        """Check whether *agent_a* and *agent_b* exchanged messages recently.

        Scans the last ``_RECENT_INTERACT_WINDOW`` global messages for any
        message whose author is one and mentions include the other (or
        vice-versa), or a broadcast originated by one of them while the
        other is in the same thread.
        """
        if self._server is None:
            return False
        try:
            snapshot = self._server.snapshot()
            messages: list[Message] = snapshot.get("messages", [])
        except Exception:  # noqa: BLE001
            return False

        recent = messages[-_RECENT_INTERACT_WINDOW:]
        for msg in recent:
            author = msg.get("author", "")
            mentions: list[str] = msg.get("mentions") or []
            # Direct mention either way
            if author == agent_a and agent_b in mentions:
                return True
            if author == agent_b and agent_a in mentions:
                return True
            # Broadcast from one to the other (both in same thread)
            if author in (agent_a, agent_b) and not mentions:
                # Check if the other is a thread participant
                thread_id = msg.get("thread_id", "")
                if thread_id:
                    try:
                        thread = self._server.get_thread(thread_id)
                        participants: list[str] = thread.get(
                            "participants", []
                        )
                        other = agent_b if author == agent_a else agent_a
                        if other in participants:
                            return True
                    except (KeyError, AttributeError):
                        pass
        return False

    # -- factory -----------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        attention_config: dict[str, Any] | None,
        *,
        server: Any = None,
    ) -> RelevancePolicy | None:
        """Factory from ``config._normalize_attention`` output.

        Returns ``None`` when ``attention_config`` is ``None`` or
        ``enabled`` is ``False`` (design §6.1 — opt-out gate).

        Otherwise constructs with values from the normalized config dict.
        """
        if attention_config is None:
            return None
        if attention_config.get("enabled") is not True:
            return None
        return cls(
            features=dict(attention_config.get("features", {})),
            tiers=dict(attention_config.get("tiers", {})),
            context=dict(attention_config.get("context", {})),
            server=server,
        )


# ── deep merge helper (used by Session.from_config) ──────────────────────


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge *override* into *base*, returning a new dict.

    Used by ``Session.from_config`` to merge per-agent ``attention.floor``
    into the global normalized attention config.  The override dict is
    expected to be ``{\"floor\": float}`` — the ``floor`` key is mapped
    into ``base[\"floors\"][\"default\"]``.
    """
    import copy

    result = copy.deepcopy(base)
    for key, value in override.items():
        if key == "floor":
            result.setdefault("floors", {})
            result["floors"]["default"] = float(value)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result