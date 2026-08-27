import asyncio
import sys
sys.path.insert(0, 'src')
from examples.p1_to_p5_demo import CFG, AGENTS
from agent_augury.session import Session

session = Session.from_config(dict(CFG))
protocol = session.protocol

def on_step(agent_id, result):
    snap = session.server.snapshot()
    gates = {p: protocol.gate_for(p) for p in ['P2_SPLIT','P3_EXECUTE','P4_REVIEW','P5_SUBMIT']}
    gate_info = {p: (g.is_open if g else None) for p, g in gates.items()}
    print(f'{agent_id}: phase={protocol.phase} tools={[c.name for c in result.tool_calls]} gates={gate_info}')
    # P1→P2 transition
    if len(snap['threads']) >= 4 and protocol.phase == 'P1_EXPLORE':
        protocol.advance('P2_SPLIT')
        print('  → advanced to P2_SPLIT')

session.on_step = on_step
steps = asyncio.run(session.run())
print(f'Final phase: {protocol.phase}')
