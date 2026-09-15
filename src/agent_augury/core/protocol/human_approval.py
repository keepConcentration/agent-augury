"""YAML ``protocol.human_approval`` normalize / validate (HUMAN_APPROVAL_GATE_DESIGN)."""

from __future__ import annotations

from typing import Any

from .phases import P1_EXPLORE, P2_SPLIT, P3_EXECUTE, P4_REVIEW, P5_SUBMIT

# Gate phases that may opt into human approval (defaults all false).
HUMAN_APPROVAL_PHASES: tuple[str, ...] = (
    P2_SPLIT,
    P3_EXECUTE,
    P4_REVIEW,
    P5_SUBMIT,
)

DEFAULT_MODE = "after_agents"
ALLOWED_MODES = frozenset({DEFAULT_MODE})


def empty_human_approval_map() -> dict[str, bool]:
    return {p: False for p in HUMAN_APPROVAL_PHASES}


def any_human_approval(enabled: dict[str, bool] | None) -> bool:
    if not enabled:
        return False
    return any(bool(enabled.get(p)) for p in HUMAN_APPROVAL_PHASES)


def normalize_human_approval(
    protocol: dict[str, Any] | None,
    *,
    config_error: type[Exception],
) -> dict[str, bool]:
    """Return P2~P5 bool map (defaults false). Mutates nothing on *protocol*.

    Accepts short form ``{P5_SUBMIT: true}`` or long form
    ``{phases: {P5_SUBMIT: {required: true, mode: after_agents}}}``.
    """
    out = empty_human_approval_map()
    if not isinstance(protocol, dict):
        return out

    participants = protocol.get("participants")
    if isinstance(participants, list):
        for p in participants:
            if isinstance(p, str) and p.strip().lower() == "human":
                raise config_error(
                    "protocol.participants must not include 'human' "
                    "(P1 READY pollution); use protocol.human_approval instead"
                )

    raw = protocol.get("human_approval")
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise config_error("protocol.human_approval must be a mapping")

    gates = protocol.get("gates")
    if not isinstance(gates, dict) or not gates:
        raise config_error(
            "protocol.human_approval requires protocol.gates to be set"
        )

    # Long form: { phases: { P5_SUBMIT: { required, mode } } }
    if "phases" in raw:
        phases_raw = raw["phases"]
        if not isinstance(phases_raw, dict):
            raise config_error("protocol.human_approval.phases must be a mapping")
        for phase_name, spec in phases_raw.items():
            _apply_phase_entry(
                out,
                str(phase_name),
                spec,
                gates=gates,
                config_error=config_error,
                long_form=True,
            )
        return out

    # Short form: { P5_SUBMIT: true, ... }
    for phase_name, spec in raw.items():
        if phase_name == "phases":
            continue
        _apply_phase_entry(
            out,
            str(phase_name),
            spec,
            gates=gates,
            config_error=config_error,
            long_form=False,
        )
    return out


def _apply_phase_entry(
    out: dict[str, bool],
    phase_name: str,
    spec: Any,
    *,
    gates: dict[str, Any],
    config_error: type[Exception],
    long_form: bool,
) -> None:
    if phase_name == P1_EXPLORE:
        raise config_error(
            "protocol.human_approval cannot enable P1_EXPLORE "
            "(conflicts with READY policy)"
        )
    if phase_name not in HUMAN_APPROVAL_PHASES:
        raise config_error(
            f"protocol.human_approval unknown phase {phase_name!r}; "
            f"allowed: {', '.join(HUMAN_APPROVAL_PHASES)}"
        )
    if phase_name not in gates:
        # Omitted/false entries may appear after normalize writes a full map
        # onto a protocol that only declares a subset of gates.
        if not (
            (isinstance(spec, bool) and spec)
            or (
                isinstance(spec, dict)
                and bool(spec.get("required", spec.get("enabled", False)))
            )
        ):
            out[phase_name] = False
            return
        raise config_error(
            f"protocol.human_approval.{phase_name} requires a matching "
            f"entry in protocol.gates"
        )

    required = False
    mode = DEFAULT_MODE
    if long_form:
        if isinstance(spec, bool):
            required = bool(spec)
        elif isinstance(spec, dict):
            required = bool(spec.get("required", False))
            if "mode" in spec and spec["mode"] is not None:
                mode = str(spec["mode"]).strip()
        else:
            raise config_error(
                f"protocol.human_approval.phases.{phase_name} must be "
                f"a bool or mapping"
            )
    else:
        if isinstance(spec, bool):
            required = bool(spec)
        elif isinstance(spec, dict):
            required = bool(spec.get("required", spec.get("enabled", False)))
            if "mode" in spec and spec["mode"] is not None:
                mode = str(spec["mode"]).strip()
        else:
            raise config_error(
                f"protocol.human_approval.{phase_name} must be a boolean "
                f"or mapping, got {type(spec).__name__}"
            )

    if mode not in ALLOWED_MODES:
        raise config_error(
            f"protocol.human_approval.{phase_name} mode must be "
            f"'after_agents' in v1, got {mode!r}"
        )
    out[phase_name] = required


def has_discord_inbound(cfg: dict[str, Any]) -> bool:
    bots = cfg.get("bots")
    if not isinstance(bots, list):
        return False
    return any(
        isinstance(b, dict) and bool(b.get("inbound")) for b in bots
    )
