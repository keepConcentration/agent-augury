"""TUI router unit tests - pure function, no TTY."""

from __future__ import annotations

from dataclasses import dataclass

from agent_augury.tui.router import RouterContext, route


@dataclass
class _PQ:
    thread_id: str
    agent_id: str
    options: list[str]


def test_route_blank_ignored():
    assert route("", RouterContext()).kind == "ignored"
    assert route("   ", RouterContext()).kind == "ignored"


def test_route_quit_slash():
    r = route("/quit", RouterContext())
    assert r.kind == "quit"
    assert r.command == "quit"
    r2 = route("/exit", RouterContext())
    assert r2.kind == "quit"


def test_route_plain_quit_is_message():
    r = route("quit", RouterContext(recent_thread="t1"))
    assert r.kind == "plain"
    assert r.content == "quit"
    assert r.thread_id == "t1"


def test_route_slash_command():
    r = route("/help", RouterContext())
    assert r.kind == "command"
    assert r.command == "help"


def test_route_choice_substitutes_option():
    pq = _PQ("th", "agent-1", ["postgres", "mysql"])
    r = route("1", RouterContext(active_question=pq))
    assert r.kind == "choice"
    assert r.content == "postgres"
    assert r.mentions == ["agent-1"]
    assert r.thread_id == "th"


def test_route_question_reply_free_text():
    pq = _PQ("th", "agent-1", ["postgres", "mysql"])
    r = route("custom answer", RouterContext(active_question=pq))
    assert r.kind == "question_reply"
    assert r.content == "custom answer"
    assert r.mentions == ["agent-1"]
    assert r.notice is not None


def test_route_plain():
    r = route("hello", RouterContext(recent_thread="t9"))
    assert r.kind == "plain"
    assert r.content == "hello"
    assert r.mentions is None
