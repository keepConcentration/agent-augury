"""ChoicePanel vertical-options + line_count tests (v1.0 — 결정 ③ 3-A).

Verifies:
* render() lists options vertically, one per line (not a single joined row)
* line_count() = question 1 + option count + (queued 1 if any), capped at 8
* queue FIFO answer/skip still works (regression from existing behavior)
"""

from __future__ import annotations

from prompt_toolkit.formatted_text import FormattedText

from agent_augury.tui.choice_panel import ChoicePanel, PendingQuestion


def _panel_with(question: str = "DB?", options: list[str] | None = None) -> ChoicePanel:
    panel = ChoicePanel()
    panel.on_ask_user(
        "agent-1",
        "ask_user",
        {
            "thread": "thread-1",
            "question": question,
            "options": options or [],
        },
        None,
    )
    return panel


def _text(panel: ChoicePanel) -> str:
    """Extract plain text from the rendered FormattedText."""
    ft = panel.render()
    assert isinstance(ft, FormattedText)
    return "".join(style_text for _style, style_text, *_ in ft) if ft else ""


# ---------------------------------------------------------------------------
# Vertical list rendering (3-A)
# ---------------------------------------------------------------------------


def test_render_options_vertical_one_per_line():
    """Options render one per line: '[1] pg' / '[2] mysql' (not one joined row)."""
    panel = _panel_with(options=["pg", "mysql"])
    text = _text(panel)
    lines = text.split("\n")
    assert lines[0].startswith("❓ agent-1: DB?")
    assert "   [1] pg" in lines[1]
    assert "   [2] mysql" in lines[2]
    # No joined single row containing both options on one line
    assert "pg   [2]" not in text


def test_render_long_option_not_cut_in_panel_text():
    """Long option stays intact in the rendered text (wrap handled by Window)."""
    long_opt = "x" * 120
    panel = _panel_with(options=[long_opt])
    text = _text(panel)
    assert long_opt in text


def test_render_no_options_question_only_single_line():
    """Question without options renders a single line (no option row)."""
    panel = _panel_with(options=[])
    text = _text(panel)
    assert text == "❓ agent-1: DB?"
    assert "\n" not in text


def test_render_empty_when_no_pending():
    panel = ChoicePanel()
    assert _text(panel) == ""


# ---------------------------------------------------------------------------
# line_count() (3-A dynamic height)
# ---------------------------------------------------------------------------


def test_line_count_question_only():
    assert _panel_with(options=[]).line_count() == 1


def test_line_count_question_plus_options():
    assert _panel_with(options=["a", "b", "c"]).line_count() == 4


def test_line_count_caps_at_max_eight():
    panel = _panel_with(options=[str(i) for i in range(1, 20)])
    assert panel.line_count() == 8  # 1 question + 19 options would be 20 → cap 8


def test_line_count_custom_max():
    panel = _panel_with(options=[str(i) for i in range(1, 10)])
    assert panel.line_count(max_lines=5) == 5


def test_line_count_no_pending_is_zero():
    assert ChoicePanel().line_count() == 0


def test_line_count_with_queued():
    panel = _panel_with(options=["a"])
    panel.on_ask_user(
        "agent-2",
        "ask_user",
        {"thread": "thread-2", "question": "Q2?", "options": ["x", "y", "z"]},
        None,
    )
    # active: 1 + 1 + queued 1 = 3
    assert panel.line_count() == 3


# ---------------------------------------------------------------------------
# FIFO answer / skip regression
# ---------------------------------------------------------------------------


def test_answer_pops_next_question():
    panel = _panel_with(options=["a", "b"])
    panel.on_ask_user(
        "agent-2",
        "ask_user",
        {"thread": "thread-2", "question": "Q2?", "options": ["x", "y", "z"]},
        None,
    )
    assert panel.active is not None and panel.active.thread_id == "thread-1"
    panel.answer()
    assert panel.active is not None and panel.active.thread_id == "thread-2"
    assert panel.active.question == "Q2?"
    panel.answer()
    assert panel.active is None


def test_skip_dismisses_current():
    panel = _panel_with(options=["a"])
    pq = panel.skip()
    assert pq is not None and pq.thread_id == "thread-1"
    assert pq.status == "dismissed"
    assert panel.active is None


def test_push_manual_pending_question():
    panel = ChoicePanel()
    panel.push(PendingQuestion("thread-9", "agent-9", "Manual?", ["y", "n"]))
    assert panel.active is not None and panel.active.question == "Manual?"
    assert panel.line_count() == 3
