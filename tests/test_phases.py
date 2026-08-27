"""Phase transition hooks — v0.1b extension point for v0.2 P1~P5."""

from agent_augury.protocol.phases import (
    APPROVED,
    OPEN,
    PROPOSED,
    PhaseManager,
)


def test_initial_phase_is_proposed():
    pm = PhaseManager()
    assert pm.phase == PROPOSED


def test_advance_fires_callback_with_from_and_to():
    seen = []
    pm = PhaseManager()
    pm.on_transition(lambda frm, to: seen.append((frm, to)))

    pm.advance(APPROVED)
    assert pm.phase == APPROVED
    assert seen == [(PROPOSED, APPROVED)]


def test_multiple_callbacks_fire_in_registration_order():
    seen = []
    pm = PhaseManager()
    pm.on_transition(lambda f, t: seen.append("cb1"))
    pm.on_transition(lambda f, t: seen.append("cb2"))

    pm.advance(OPEN)
    assert seen == ["cb1", "cb2"]


def test_advance_is_idempotent_no_callback_on_same_phase():
    seen = []
    pm = PhaseManager()
    pm.on_transition(lambda f, t: seen.append((f, t)))

    pm.advance(PROPOSED)  # same as current
    assert seen == []
    assert pm.phase == PROPOSED


def test_gate_open_triggers_phase_advance():
    """Integration: ConsensusGate.on_open drives PhaseManager.advance."""
    from agent_augury.protocol.approval import ConsensusGate
    from agent_augury.server import MessageServer

    async def scenario():
        server = MessageServer()
        for a in ("a", "b"):
            server.register_agent(a)

        gate = ConsensusGate(server, thread_name="plan")
        server.subscribe(gate.on_message)

        pm = PhaseManager()
        gate.on_open(lambda: pm.advance(OPEN))

        plan = await server.create_thread("plan", participants=["a", "b"])
        await server.send_message(plan, author="a", content="PROPOSE: v1", mentions=[])
        assert pm.phase == PROPOSED  # proposal alone does not advance

        await server.send_message(plan, author="a", content="APPROVE: ok", mentions=[])
        assert pm.phase == PROPOSED  # not unanimous yet

        await server.send_message(plan, author="b", content="APPROVE: ok", mentions=[])
        assert gate.is_open
        assert pm.phase == OPEN  # on_open fired → phase advanced

    import asyncio
    asyncio.run(scenario())
