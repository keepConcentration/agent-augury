"""InputBar Enter=submit / newline / history regression tests (V1-V7, V6b)."""

from __future__ import annotations

from unittest.mock import MagicMock

from prompt_toolkit.document import Document
from prompt_toolkit.keys import Keys

from agent_augury.tui.input_bar import InputBar


def _handlers_for(bar: InputBar, *want: object) -> list:
    """Return handlers whose binding keys match *want* (Keys or alias strings)."""
    out = []
    for b in bar._kb.bindings:
        vals = {getattr(k, "value", str(k)) for k in b.keys}
        keys = set(b.keys) | vals
        if all(w in keys or getattr(w, "value", w) in vals for w in want) and len(b.keys) == len(want):
            out.append(b.handler)
    return out


def _event(buffer) -> MagicMock:
    event = MagicMock()
    event.current_buffer = buffer
    event.app = MagicMock()
    return event


def test_enter_and_cj_and_escape_cj_bindings_present(tmp_path):
    bar = InputBar(lambda _t: None, history=tmp_path / "h.txt")
    assert _handlers_for(bar, Keys.ControlM) or _handlers_for(bar, "c-m")
    assert _handlers_for(bar, Keys.ControlJ) or _handlers_for(bar, "c-j")
    assert _handlers_for(bar, Keys.Escape, Keys.ControlM) or _handlers_for(
        bar, "escape", "c-m"
    )
    assert _handlers_for(bar, Keys.Escape, Keys.ControlJ) or _handlers_for(
        bar, "escape", "c-j"
    )


def test_enter_submits_via_validate_and_handle(tmp_path):
    """V1: Enter binding calls validate_and_handle -> accept_handler."""
    submitted: list[str] = []
    bar = InputBar(lambda t: submitted.append(t), history=tmp_path / "h.txt")
    buff = bar.widget.buffer
    buff.text = "hello"
    handlers = _handlers_for(bar, Keys.ControlM) or _handlers_for(bar, "c-m")
    assert handlers
    handlers[0](_event(buff))
    assert submitted == ["hello"]
    assert buff.text == ""


def test_blank_enter_still_accepts_buffer(tmp_path):
    """Blank text still goes through accept; router ignores upstream (V2)."""
    submitted: list[str] = []
    bar = InputBar(lambda t: submitted.append(t), history=tmp_path / "h.txt")
    buff = bar.widget.buffer
    buff.text = ""
    handlers = _handlers_for(bar, Keys.ControlM) or _handlers_for(bar, "c-m")
    handlers[0](_event(buff))
    assert submitted == [""]


def test_escape_enter_inserts_newline(tmp_path):
    """V6 part: Esc+Enter inserts newline, does not submit."""
    submitted: list[str] = []
    bar = InputBar(lambda t: submitted.append(t), history=tmp_path / "h.txt")
    buff = bar.widget.buffer
    buff.text = "a"
    handlers = _handlers_for(bar, Keys.Escape, Keys.ControlM) or _handlers_for(
        bar, "escape", "c-m"
    )
    assert handlers
    buff.insert_text = MagicMock()  # type: ignore[method-assign]
    handlers[0](_event(buff))
    buff.insert_text.assert_called_once_with("\n")
    assert submitted == []


def test_escape_cj_inserts_newline_not_submit(tmp_path):
    """V6b: win32 Ctrl+Enter (Escape, ControlJ) inserts newline."""
    submitted: list[str] = []
    bar = InputBar(lambda t: submitted.append(t), history=tmp_path / "h.txt")
    buff = bar.widget.buffer
    buff.text = "a"
    handlers = _handlers_for(bar, Keys.Escape, Keys.ControlJ) or _handlers_for(
        bar, "escape", "c-j"
    )
    assert handlers
    buff.insert_text = MagicMock()  # type: ignore[method-assign]
    handlers[0](_event(buff))
    buff.insert_text.assert_called_once_with("\n")
    assert submitted == []


def test_multiline_then_enter_submits(tmp_path):
    """V6: multiline buffer then Enter submits."""
    submitted: list[str] = []
    bar = InputBar(lambda t: submitted.append(t), history=tmp_path / "h.txt")
    buff = bar.widget.buffer
    buff.set_document(Document("a\nb", cursor_position=3))
    enter = _handlers_for(bar, Keys.ControlM) or _handlers_for(bar, "c-m")
    enter[0](_event(buff))
    assert submitted == ["a\nb"]


def test_history_saved_after_enter(tmp_path):
    """V7: accept returns False so validate_and_handle can append_to_history."""
    hist_path = tmp_path / "hist.txt"
    submitted: list[str] = []
    bar = InputBar(lambda t: submitted.append(t), history=hist_path)
    buff = bar.widget.buffer
    buff.text = "hello"
    enter = _handlers_for(bar, Keys.ControlM) or _handlers_for(bar, "c-m")
    enter[0](_event(buff))
    assert submitted == ["hello"]
    assert "hello" in list(buff.history.get_strings())
    assert "hello" in hist_path.read_text(encoding="utf-8")


def test_accept_return_false_preserves_text_for_history(tmp_path):
    """Defect-2: accept_handler must not reset before append_to_history."""
    submitted: list[str] = []
    bar = InputBar(lambda t: submitted.append(t), history=tmp_path / "h.txt")
    buff = bar.widget.buffer
    buff.text = "keep-me"
    keep = bar.widget.accept_handler(buff)  # type: ignore[misc]
    assert keep is False
    assert buff.text == "keep-me"


def test_ctrl_c_clears_draft(tmp_path):
    """V5: Ctrl+C resets buffer."""
    bar = InputBar(lambda _t: None, history=tmp_path / "h.txt")
    bar.widget.buffer.text = "draft"
    handlers = _handlers_for(bar, Keys.ControlC) or _handlers_for(bar, "c-c")
    assert handlers
    handlers[0](_event(bar.widget.buffer))
    assert bar.widget.buffer.text == ""
