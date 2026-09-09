"""L3 Passive Awareness benchmark — pytest entry point (offline, deterministic).

실행:
    pytest examples/benchmark/ -q

README.md '단언 (숫자)' 섹션과 run_l3_passive_awareness.py의 assert_benchmark와
동일한 어서션을 pytest 컬렉션으로 노출한다. CI 통합용.
"""

from __future__ import annotations

import sys
from pathlib import Path

# src 레이아웃 bootstrap — 설치 없이 repo 루트에서 pytest로 실행 가능하게
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from run_l3_passive_awareness import (
    FINAL_ANSWER,
    assert_benchmark,
    run_l3_scenario,
)


async def test_l3_search_not_interrupted_and_answer_absorbed():
    """search N회 이상 AND 최종 답 = 43 (작업 비방해 + 정정 흡수)."""
    metrics = await run_l3_scenario(search_rounds=3)
    assert_benchmark(metrics, search_rounds=3)


async def test_l3_radio_block_injected():
    """push → [radio] 블록이 step 경계에서 삽입되어야 한다."""
    metrics = await run_l3_scenario(search_rounds=3)
    assert metrics["radio_seen"] is True
    assert "(URGENT) 정답은 42가 아니라 43." in metrics["radio_content"]


async def test_l3_answer_comes_after_radio():
    """정정 흡수 이후에 최종 답이 나와야 한다 (흡수 순서)."""
    metrics = await run_l3_scenario(search_rounds=3)
    assert metrics["final_answer_after_radio"] is True


async def test_l3_no_explicit_polling():
    """명시적 폴링(read_resource) 없이 push-only로 수신해야 한다."""
    metrics = await run_l3_scenario(search_rounds=3)
    assert metrics["polled"] is False


async def test_l3_scales_with_search_rounds():
    """search_rounds를 늘려도 작업 비방해가 유지된다."""
    metrics = await run_l3_scenario(search_rounds=5)
    assert metrics["search_steps"] >= 5
    assert metrics["final_answer"] == FINAL_ANSWER
    assert metrics["final_answer_after_radio"] is True
