"""Team planner — TEAM_PLANNER_DESIGN.md."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from agent_augury.backend.base import Completion
from agent_augury.backend.fake import FakeModelBackend
from agent_augury.config import load_config
from agent_augury.model_config import (
    is_planner_config,
    load_model_config,
    save_model_config,
)
from agent_augury.model_listing import ModelInfo
from agent_augury.planner import (
    MAX_AGENTS,
    build_messages,
    format_plan,
    parse_plan,
    plan_team,
    to_session_agents,
)
from agent_augury.wizard import run_wizard


@pytest.fixture(autouse=True)
def _no_real_sign_in(request):
    """plan_loop signs in to OAuth providers first; never hit the network or
    open a browser from tests (test_ensure_oauth_* exercise it directly)."""
    if request.node.name.startswith("test_ensure_oauth"):
        yield
        return

    async def _noop(*_a, **_k):
        return None

    with patch("agent_augury.planner.ensure_oauth", _noop):
        yield

PROVIDERS = {
    "openrouter": {
        "type": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "nous_oauth": {
        "type": "nous_oauth",
        "base_url": "https://inference-api.nousresearch.com/v1",
    },
}
PLANNER = {"provider": "nous_oauth", "model": "big"}
CATALOG = {
    "nous_oauth": {"big": ModelInfo("big", 1e-6, 3e-6), "small": None},
    "openrouter": {"coder": ModelInfo("coder", 2e-7, 8e-7)},
}


def _agent(i="a1", provider="nous_oauth", model="big", role="does things"):
    return {"id": i, "provider": provider, "model": model, "role": role, "reason": "r"}


def _reply(*agents):
    return json.dumps({"agents": list(agents)})


def _feed(responses):
    it = iter(responses)

    def fake(_prompt=""):
        return next(it, "")

    return fake


# ── parse_plan ─────────────────────────────────────────────────────────────


def test_parse_plan_accepts_fenced_json():
    text = "Here you go:\n```json\n" + _reply(_agent(), _agent("a2", "openrouter", "coder")) + "\n```"
    plan = parse_plan(text, CATALOG)
    assert [a["id"] for a in plan] == ["a1", "a2"]
    assert plan[1]["provider"] == "openrouter"


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("no json here", "no JSON"),
        ("{not json}", "invalid JSON"),
        (_reply(), "1-10"),
        (_reply(*[_agent(f"a{i}") for i in range(MAX_AGENTS + 1)]), "1-10"),
        (_reply(_agent(model="ghost")), "not in the list"),
        (_reply(_agent(), _agent("A1")), "duplicated"),
        (_reply(_agent("human")), "reserved"),
        (_reply(_agent(i="")), "id is empty"),
        (_reply(_agent(role="  ")), "role is empty"),
    ],
)
def test_parse_plan_rejects(text, reason):
    with pytest.raises(ValueError, match=reason):
        parse_plan(text, CATALOG)


# ── plan_team ──────────────────────────────────────────────────────────────


def test_plan_team_retries_once_with_reason():
    backend = FakeModelBackend(
        [Completion(text=_reply(_agent(model="ghost"))), Completion(text=_reply(_agent()))]
    )
    plan = asyncio.run(plan_team("task", backend, CATALOG))
    assert plan[0]["model"] == "big"
    retry_msgs = backend.calls[1]["messages"]
    assert "not in the list" in retry_msgs[-1]["content"]


def test_plan_team_gives_up_after_second_bad_reply():
    backend = FakeModelBackend([Completion(text="nope"), Completion(text="still nope")])
    with pytest.raises(ValueError, match="rejected"):
        asyncio.run(plan_team("task", backend, CATALOG))


def test_build_messages_groups_models_under_provider():
    system = build_messages("t", CATALOG)[0]["content"]
    assert "provider: nous_oauth\n  - big  [$1/$3 per 1M tok]\n  - small\n" in system
    assert "provider: openrouter\n  - coder" in system


# Real Nous ids contain "/" — the planner mis-split them (2026-09-28 run).
SLASHY = {
    "nous_oauth": {"openai/gpt-5.2-codex": None, "anthropic/claude-sonnet-4.5": None},
    "openai": {"gpt-4o": None, "gpt-5.2-codex": None},
}


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("nous_oauth", "openai/gpt-5.2-codex", ("nous_oauth", "openai/gpt-5.2-codex")),
        # provider swallowed the model's vendor segment
        ("nous_oauth/anthropic", "claude-sonnet-4.5", ("nous_oauth", "anthropic/claude-sonnet-4.5")),
        # provider path echoed inside model
        ("nous_oauth/openai", "nous_oauth/openai/gpt-5.2-codex", ("nous_oauth", "openai/gpt-5.2-codex")),
        ("", "nous_oauth/openai/gpt-5.2-codex", ("nous_oauth", "openai/gpt-5.2-codex")),
        # an "openai" provider must not steal the nous id's own "openai/" segment
        ("openai", "gpt-5.2-codex", ("openai", "gpt-5.2-codex")),
        ("openai", "gpt-4o", ("openai", "gpt-4o")),
        # unique owner rescues a wrong provider; unknown id stays rejected
        ("openrouter", "anthropic/claude-sonnet-4.5", ("nous_oauth", "anthropic/claude-sonnet-4.5")),
        ("nous_oauth", "anthropic/claude-sonnet-9", None),
    ],
)
def test_resolve_model_handles_slashy_ids(provider, model, expected):
    from agent_augury.planner import resolve_model

    assert resolve_model(provider, model, SLASHY) == expected


def test_rejection_suggests_close_ids():
    with pytest.raises(ValueError, match=r"did you mean: .*anthropic/claude-sonnet-4\.5"):
        parse_plan(_reply(_agent(provider="nous_oauth", model="anthropic/claude-sonnet-4.9")), SLASHY)


def test_format_plan_and_session_agents():
    plan = parse_plan(_reply(_agent(), _agent("coder", "openrouter", "coder")), CATALOG)
    table = format_plan(plan, CATALOG)
    assert "Proposed team (2 agents)" in table
    assert "openrouter/coder" in table
    agents = to_session_agents(plan, PROVIDERS)
    assert agents[1] == {
        "id": "coder",
        "role_custom": "does things",
        "backend": {**PROVIDERS["openrouter"], "model": "coder"},
    }


# ── model_config ───────────────────────────────────────────────────────────


def test_model_config_planner_round_trip(tmp_path):
    path = tmp_path / "mc.json"
    save_model_config(0, [], path, bots=[], providers=PROVIDERS, planner=PLANNER)
    loaded = load_model_config(path)
    assert loaded is not None and is_planner_config(loaded)
    assert loaded["planner"] == PLANNER


def test_is_planner_config_requires_planner_provider_registered():
    assert not is_planner_config({"providers": PROVIDERS, "planner": {"provider": "x", "model": "m"}})
    assert not is_planner_config({"agents": [{"id": "a"}]})


# ── wizard (planner mode) ──────────────────────────────────────────────────


def test_wizard_planner_mode_saves_providers_and_planner():
    inputs = [
        "2",          # provider = OpenRouter
        "n",          # no more providers
        "or-model",   # planner model (manual entry — listing patched off)
    ]
    with patch("agent_augury.wizard._select_setup_mode", return_value="planner"), \
         patch("builtins.input", side_effect=_feed(inputs)), \
         patch("agent_augury.wizard._try_list_models", return_value=None), \
         patch("agent_augury.wizard.save_model_config") as mock_save:
        cfg = run_wizard()

    assert cfg["agents"] == []
    assert cfg["planner"] == {"provider": "openrouter", "model": "or-model"}
    assert cfg["providers"]["openrouter"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert mock_save.call_args.kwargs["planner"] == cfg["planner"]


# ── cli end-to-end ─────────────────────────────────────────────────────────


def _planner_saved():
    return {"max_steps": 0, "agents": [], "bots": [], "providers": PROVIDERS, "planner": PLANNER}


def _run_cli(tmp_path, monkeypatch, confirm):
    from agent_augury.cli import main

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    out = tmp_path / "session.yaml"
    out.write_text("bots: [{agent_id: old, token_env: X, channel_id: 1}]\ncompact: {}\n", encoding="utf-8")
    fake = FakeModelBackend(
        [Completion(text=_reply(_agent("lead"), _agent("coder", "openrouter", "coder")))]
    )
    launches: list[dict] = []
    with patch("agent_augury.cli.check_tty", return_value=True), \
         patch("agent_augury.cli.model_config_exists", return_value=True), \
         patch("agent_augury.cli.load_model_config", return_value=_planner_saved()), \
         patch("agent_augury.planner.build_catalog", return_value=CATALOG), \
         patch("agent_augury.backends_factory.build_backend", return_value=fake), \
         patch("builtins.input", side_effect=_feed([confirm])), \
         patch("agent_augury.cli._launch_session", side_effect=lambda p, **kw: launches.append({"path": p, **kw}) or 0):
        rc = main(["--plan", "build it", "--output", str(out)])
    return rc, out, launches


def test_cli_plan_writes_team_yaml_and_starts_fresh(tmp_path, monkeypatch):
    rc, out, launches = _run_cli(tmp_path, monkeypatch, "y")
    assert rc == 0
    assert launches and launches[0]["new_session"] is True
    loaded = load_config(out)
    assert loaded["task"] == "build it"
    assert [a["id"] for a in loaded["agents"]] == ["lead", "coder"]
    assert loaded["agents"][1]["role_custom"] == "does things"
    assert loaded["bots"] == []           # old team's binding dropped
    assert "compact" in loaded             # other hand-edited keys kept
    assert "providers" not in loaded and "planner" not in loaded


def test_cli_plan_declined_starts_nothing(tmp_path, monkeypatch):
    rc, _out, launches = _run_cli(tmp_path, monkeypatch, "n")
    assert rc == 0
    assert launches == []


def test_cli_plan_rejects_manual_setup(tmp_path, capsys):
    from agent_augury.cli import main

    manual = {"max_steps": 0, "agents": [{"id": "a", "backend": {"type": "fake"}}], "bots": []}
    with patch("agent_augury.cli.check_tty", return_value=True), \
         patch("agent_augury.cli.model_config_exists", return_value=True), \
         patch("agent_augury.cli.load_model_config", return_value=manual):
        rc = main(["--plan", "x", "--output", str(tmp_path / "s.yaml")])
    assert rc == 1
    assert "team-planner setup" in capsys.readouterr().err


# ── re-plan loop ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [("", "yes"), ("Y", "yes"), ("네", "yes"), ("no", "no"), ("취소", "no"), ("리뷰어 추가해줘", "revise")],
)
def test_classify_answer(answer, verdict):
    from agent_augury.planner import classify_answer

    assert classify_answer(answer) == verdict


def test_plan_loop_replans_with_feedback():
    from agent_augury.planner import plan_loop

    fake = FakeModelBackend(
        [
            Completion(text=_reply(_agent("lead"))),
            Completion(text=_reply(_agent("lead"), _agent("reviewer", model="small"))),
        ]
    )
    answers = iter(["add a reviewer", "yes"])
    with patch("agent_augury.planner.build_catalog", return_value=CATALOG), \
         patch("agent_augury.backends_factory.build_backend", return_value=fake):
        agents = asyncio.run(
            plan_loop("t", PROVIDERS, PLANNER, ask=lambda _t: next(answers), notify=lambda _m: None)
        )
    assert [a["id"] for a in agents] == ["lead", "reviewer"]
    second = fake.calls[1]["messages"]
    assert '"lead"' in second[-2]["content"]          # previous proposal replayed
    assert "add a reviewer" in second[-1]["content"]  # feedback attached


def test_cli_planner_without_plan_defers_to_ink(tmp_path, monkeypatch):
    from agent_augury.cli import main

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    out = tmp_path / "session.yaml"
    launches: list[dict] = []
    with patch("agent_augury.cli.check_tty", return_value=True), \
         patch("agent_augury.cli.model_config_exists", return_value=True), \
         patch("agent_augury.cli.load_model_config", return_value=_planner_saved()), \
         patch("agent_augury.planner.plan_loop") as never_called, \
         patch("agent_augury.cli._run_ink_surface", side_effect=lambda **kw: launches.append(kw) or 0):
        rc = main(["--output", str(out)])
    assert rc == 0
    never_called.assert_not_called()
    assert launches[0]["plan_first"] is True
    import yaml

    saved = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert saved["planner"] == PLANNER and saved["agents"] == []


# ── Ink pre-phase (session_stdio.plan_first) ───────────────────────────────


def _cmd(typ, cid, **fields):
    return json.dumps({"dir": "cmd", "type": typ, "id": cid, **fields}) + "\n"


def _plan_first_run(tmp_path, lines, script):
    import yaml

    from agent_augury.gateway.session_stdio import plan_first

    cfg = tmp_path / "session.yaml"
    cfg.write_text(
        yaml.safe_dump({"max_steps": 0, "human": {"id": "human"}, "agents": [],
                        "providers": PROVIDERS, "planner": PLANNER}),
        encoding="utf-8",
    )
    written: list[dict] = []
    fake = FakeModelBackend(script)
    with patch("agent_augury.planner.build_catalog", return_value=CATALOG), \
         patch("agent_augury.backends_factory.build_backend", return_value=fake):
        ok = plan_first(str(cfg), iter(lines), lambda s: written.append(json.loads(s)))
    return ok, cfg, written


def test_plan_first_replans_then_writes_session_yaml(tmp_path):
    ok, cfg, events = _plan_first_run(
        tmp_path,
        [
            _cmd("human.send", "c1", content="모듈 분석해줘"),
            _cmd("human.answer", "c2", content="add a coder", question_id="plan-1"),
            _cmd("human.answer", "c3", content="1", question_id="plan-2"),  # [1] yes
        ],
        [
            Completion(text=_reply(_agent("lead"))),
            Completion(text=_reply(_agent("lead"), _agent("coder", "openrouter", "coder"))),
        ],
    )
    assert ok is True
    assert events[0]["type"] == "session.started" and events[0]["agents"] == []
    questions = [e for e in events if e.get("type") == "human.question"]
    assert [q["question_id"] for q in questions] == ["plan-1", "plan-2"]
    # Table lives in the (printed-once) log; the redrawn ask_user panel stays one line.
    assert all("Proposed team" not in q["question"] for q in questions)
    q1 = events.index(questions[0])
    assert "Proposed team" in events[q1 - 1]["text"]
    assert all(e["ok"] for e in events if e["dir"] == "result")
    loaded = load_config(cfg)
    assert loaded["task"] == "모듈 분석해줘"
    assert [a["id"] for a in loaded["agents"]] == ["lead", "coder"]
    assert "providers" not in loaded
    assert "모듈 분석해줘" in cfg.read_text(encoding="utf-8")  # plain text, not \u escapes


def test_prompt_treats_deliverable_owner_as_core():
    system = build_messages("t", CATALOG)[0]["content"]
    assert "final deliverable is a CORE role" in system


def test_plan_first_declined_then_quit(tmp_path):
    ok, _cfg, events = _plan_first_run(
        tmp_path,
        [
            _cmd("human.send", "c1", content="build it"),
            _cmd("human.skip", "c2", question_id="plan-1"),   # /skip == no
            _cmd("session.quit", "c3"),
        ],
        [Completion(text=_reply(_agent("lead")))],
    )
    assert ok is False
    types = [e.get("type") for e in events if e["dir"] == "event"]
    assert "session.turn_done" in types      # prompt handed back after "no"
    assert types[-1] == "session.ended"


def test_plan_first_reports_planner_failure_and_waits(tmp_path):
    ok, _cfg, events = _plan_first_run(
        tmp_path,
        [_cmd("human.send", "c1", content="build it")],  # then EOF
        [Completion(text="garbage"), Completion(text="still garbage")],
    )
    assert ok is False
    errors = [e for e in events if e.get("type") == "error"]
    assert errors and "planning failed" in errors[0]["message"]


# ── sign-in before planning (same flow as a session backend) ───────────────


def test_ensure_oauth_reports_cause_and_reconfigure_hint():
    from agent_augury.planner import ensure_oauth

    notes: list[str] = []

    async def boom(self):
        raise RuntimeError("refresh rejected")

    with patch("agent_augury.backend.nous_portal_oauth.NousPortalOAuthBackend.get_access_token", boom):
        asyncio.run(ensure_oauth(PROVIDERS, notes.append, None))
    assert len(notes) == 1  # only the OAuth provider is signed in here
    assert "sign-in failed: RuntimeError: refresh rejected" in notes[0]
    assert "--reconfigure" in notes[0]


def test_ensure_oauth_uses_backend_sign_in_with_code_callback():
    from agent_augury.planner import ensure_oauth

    seen: list = []

    async def ok(self):
        seen.append(self._on_user_code)
        return "tok"

    def cb(code, uri):
        return None

    notes: list[str] = []
    with patch("agent_augury.backend.nous_portal_oauth.NousPortalOAuthBackend.get_access_token", ok):
        asyncio.run(ensure_oauth(PROVIDERS, notes.append, cb))
    assert seen == [cb] and notes == []


def test_unavailable_reason_names_missing_key_env(monkeypatch):
    from agent_augury.planner import unavailable_reason

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert unavailable_reason("openrouter", PROVIDERS["openrouter"]) == (
        "API key env OPENROUTER_API_KEY is not set"
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert unavailable_reason("openrouter", PROVIDERS["openrouter"]) is None
