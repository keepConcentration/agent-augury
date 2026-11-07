"""``agent-augury --web`` — Web BFF launcher (WBS 4.1, F-022, N-003)."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_augury import cli
from agent_augury.cli import _web_env, _web_front_dir, main

REPO = Path(__file__).resolve().parents[1]
LIFECYCLE_YAML = REPO / "tests" / "fixtures" / "web_lifecycle.yaml"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--web"], "--web needs --config"),
        (["--web", "--config", "x.yaml", "--headless"], "cannot be combined with --headless"),
        (["--web", "--ink-hello"], "cannot be combined with --ink-hello"),
        (["--web", "--plan", "do it"], "cannot be combined with --plan"),
        (["--web", "--reconfigure"], "cannot be combined with --reconfigure"),
        (["--web", "--output", "o.yaml"], "cannot be combined with --output"),
    ],
)
def test_web_rejects_unsupported_combinations(argv, message, capsys):
    with patch.object(cli, "_run_web_surface") as run:
        assert main(argv) == 1
    run.assert_not_called()
    assert message in capsys.readouterr().err


def test_web_passes_session_flags_and_allows_no_auto_start():
    with patch.object(cli, "_run_web_surface", return_value=0) as run:
        assert main(["--web", "--config", "s.yaml", "--quiet", "--no-auto-start", "--session", "s1"]) == 0
    run.assert_called_once_with(
        "s.yaml", quiet=True, no_auto_start=True, new_session=False, session_id="s1"
    )


def test_web_rejects_the_removed_demo_flag():
    with patch.object(cli, "_run_web_surface") as run, pytest.raises(SystemExit) as exc:
        main(["--web", "--config", "s.yaml", "--demo"])
    assert exc.value.code == 2
    run.assert_not_called()


def test_no_auto_start_still_needs_headless_or_web(capsys):
    assert main(["--config", "s.yaml", "--no-auto-start"]) == 1
    assert "--headless or --web" in capsys.readouterr().err


def test_web_env_scrubs_secrets_and_sets_ink_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setenv("AUGURY_PLAN_FIRST", "1")
    monkeypatch.setenv("AGENT_AUGURY_SESSION", "stale")
    cfg = tmp_path / "s.yaml"
    env = _web_env(cfg, quiet=False, no_auto_start=True, new_session=False, session_id=None)
    secrets_file = Path(env["AUGURY_GATEWAY_SECRETS_FILE"])
    try:
        assert "OPENAI_API_KEY" not in env
        assert json.loads(secrets_file.read_text())["OPENAI_API_KEY"] == "sk-test-123"
        assert env["AUGURY_GATEWAY_MODE"] == "session"
        assert env["AUGURY_CONFIG"] == str(cfg)
        assert env["AUGURY_NO_AUTO_START"] == "1"
        assert "AUGURY_DEMO" not in env
        assert "AUGURY_QUIET" not in env
        assert "AUGURY_PLAN_FIRST" not in env
        assert "AGENT_AUGURY_SESSION" not in env
        assert env["AUGURY_FILE_ROOT"] == str(Path.cwd())
        assert env["AUGURY_LAUNCHED_BY_CLI"] == "1"  # BFF: no token => CLI gone, not dev mode
        # No access token travels through env (SRS §3.5.1).
        assert not any("TOKEN" in k and "AUGURY" in k for k in env)
    finally:
        secrets_file.unlink(missing_ok=True)


def test_web_reports_missing_config(tmp_path, capsys):
    assert cli._run_web_surface(str(tmp_path / "nope.yaml")) == 1
    assert "config not found" in capsys.readouterr().err


def test_web_reports_old_or_missing_node(capsys):
    with patch.object(shutil, "which", return_value=None):
        assert cli._run_web_surface(str(LIFECYCLE_YAML)) == 1
    assert "Node.js >= 22" in capsys.readouterr().err
    with patch.object(shutil, "which", return_value="/usr/bin/node"), patch.object(cli, "_node_major", return_value=20):
        assert cli._run_web_surface(str(LIFECYCLE_YAML)) == 1
    assert "Node.js >= 22" in capsys.readouterr().err


def _ready_front(tmp_path, monkeypatch, *, with_modules: bool = True) -> Path:
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text(json.dumps({"dependencies": {"ajv": "^8"}}))
    if with_modules:
        for name in ("tsx", "ajv"):
            (web / "node_modules" / name).mkdir(parents=True)
            (web / "node_modules" / name / "package.json").write_text("{}")
    (tmp_path / "ink" / "src").mkdir(parents=True)
    (tmp_path / "ink" / "src" / "wire.ts").write_text("")
    monkeypatch.setattr(cli, "_web_front_dir", lambda: web)
    monkeypatch.setattr(shutil, "which", lambda name: "/fake/node")
    monkeypatch.setattr(cli, "_node_major", lambda node: 22)
    return web


def test_web_reports_missing_node_modules(tmp_path, monkeypatch, capsys):
    web = _ready_front(tmp_path, monkeypatch, with_modules=False)
    assert cli._run_web_surface(str(LIFECYCLE_YAML)) == 1
    assert f"run `npm install` in {web}" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("remove", "named"),
    [
        ("web/node_modules/ajv", "ajv"),
        ("web/node_modules/tsx", "tsx"),
        ("ink/src/wire.ts", "wire.ts"),
    ],
)
def test_web_reports_each_missing_bff_import(tmp_path, monkeypatch, capsys, remove, named):
    web = _ready_front(tmp_path, monkeypatch)
    target = tmp_path / remove
    shutil.rmtree(target) if target.is_dir() else target.unlink()
    with patch.object(subprocess, "Popen") as popen:
        assert cli._run_web_surface(str(LIFECYCLE_YAML)) == 1
    popen.assert_not_called()
    err = capsys.readouterr().err
    assert named in err and f"run `npm install` in {web}" in err


def test_web_refuses_windows(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    assert cli._run_web_surface(str(LIFECYCLE_YAML)) == 1
    assert "not supported on Windows" in capsys.readouterr().err


class _FakeBff:
    """Stands in for the BFF process: records what the CLI hands it."""

    def __init__(self, argv, **kwargs):
        import io

        self.argv = argv
        self.env = kwargs["env"]
        self.stdin = io.BytesIO()
        self.stdin.close = lambda: None  # type: ignore[method-assign]
        self.stdout = io.BytesIO(b"http://127.0.0.1:1/#token=x\n")
        self.signals: list[int] = []
        _FakeBff.last = self

    returncode = 0

    def wait(self):
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)


def test_web_token_goes_over_stdin_only_and_secrets_never_in_env(tmp_path, monkeypatch, capsys):
    _ready_front(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-should-not-pass")
    monkeypatch.setattr(subprocess, "Popen", _FakeBff)
    assert cli._run_web_surface(str(LIFECYCLE_YAML)) == 0
    bff = _FakeBff.last
    token = bff.stdin.getvalue().decode().strip()
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    values = " ".join(bff.env.values())
    assert token not in values and token not in " ".join(bff.argv)
    assert "sk-live-should-not-pass" not in values
    assert bff.argv[1:] == ["--import", "tsx", "src/bff/main.ts"]
    assert "http://127.0.0.1:1/#token=x" in capsys.readouterr().out
    # The Gateway never read it here, so the CLI removed the secrets file.
    assert not Path(bff.env["AUGURY_GATEWAY_SECRETS_FILE"]).exists()


@pytest.mark.parametrize(("rc", "expected"), [(-15, 143), (-9, 137), (3, 3), (0, 0)])
def test_web_normalizes_a_signal_death_to_128_plus_n(tmp_path, monkeypatch, rc, expected):
    _ready_front(tmp_path, monkeypatch)
    monkeypatch.setattr(_FakeBff, "returncode", rc)
    monkeypatch.setattr(subprocess, "Popen", _FakeBff)
    assert cli._run_web_surface(str(LIFECYCLE_YAML)) == expected


@pytest.mark.parametrize("tty", [False, True])
def test_web_warns_on_stderr_when_the_token_url_goes_to_a_non_terminal(
    tmp_path, monkeypatch, capsys, tty
):
    _ready_front(tmp_path, monkeypatch)
    monkeypatch.setattr(subprocess, "Popen", _FakeBff)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)
    assert cli._run_web_surface(str(LIFECYCLE_YAML)) == 0
    out = capsys.readouterr()
    assert "http://127.0.0.1:1/#token=x" in out.out
    assert ("one-time access token" in out.err) is not tty
    assert "one-time access token" not in out.out


def test_web_removes_the_secrets_file_when_the_bff_cannot_start(tmp_path, monkeypatch, capsys):
    _ready_front(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-should-not-linger")
    made: list[str] = []
    real_env = cli._web_env

    def spy(*a, **kw):
        env = real_env(*a, **kw)
        made.append(env["AUGURY_GATEWAY_SECRETS_FILE"])
        return env

    def broken(*a, **kw):
        raise OSError("no such file: node")

    monkeypatch.setattr(cli, "_web_env", spy)
    monkeypatch.setattr(subprocess, "Popen", broken)
    assert cli._run_web_surface(str(LIFECYCLE_YAML)) == 1
    assert "could not start the Web BFF" in capsys.readouterr().err
    assert made and not Path(made[0]).exists()


# ---- real launch ------------------------------------------------------------------

_web = _web_front_dir()
_ready = (
    sys.platform != "win32"
    and shutil.which("node") is not None
    and _web is not None
    and (_web / "node_modules" / "tsx").is_dir()
)
requires_web = pytest.mark.skipif(not _ready, reason="needs node and fronts/web/node_modules")


def _start(*extra: str) -> tuple[subprocess.Popen[str], str]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_augury.cli", "--web", "--config", str(LIFECYCLE_YAML), "--no-auto-start", *extra],
        cwd=REPO,
        env={**os.environ, "PYTHONPATH": str(REPO / "src"), "AUGURY_TEST_DUMMY_API_KEY": "dummy"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    url = proc.stdout.readline().strip()
    return proc, url


def _exchange(url: str) -> int:
    port = re.search(r":(\d+)/", url).group(1)
    token = url.split("#token=", 1)[1]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/auth/exchange",
        data=json.dumps({"token": token}).encode(),
        headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def _pids() -> set[str]:
    """BFF and Gateway child processes (compared against a snapshot taken before)."""
    found: set[str] = set()
    for pattern in ("src/bff/main.ts", "agent_augury.gateway.session_stdio"):
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False).stdout
        found.update(p for p in out.split() if p)
    return found


def _wait_gone(pattern_pids: list[str], timeout: float = 10.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        alive = [p for p in pattern_pids if _alive(int(p))]
        if not alive:
            return
        time.sleep(0.1)
    raise AssertionError(f"still running: {pattern_pids}")


def _secrets_files() -> set[str]:
    import glob
    import tempfile

    return set(glob.glob(os.path.join(tempfile.gettempdir(), "augury-secrets-*.json")))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@requires_web
def test_web_launch_prints_url_token_works_once_and_ctrl_c_cleans_up():
    before = _pids()
    proc, url = _start()
    try:
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+/#token=[A-Za-z0-9_-]{43}", url), url
        assert _exchange(url) == 204
        with pytest.raises(urllib.error.HTTPError) as again:
            _exchange(url)
        assert again.value.code == 401
        bffs = sorted(_pids() - before)
        assert len(bffs) >= 2, "expected the BFF and its Gateway child"
        proc.send_signal(signal.SIGINT)  # what a terminal Ctrl+C does to the CLI
        assert proc.wait(timeout=20) == 0
        _wait_gone(bffs)
    finally:
        if proc.poll() is None:
            proc.kill()


@requires_web
def test_web_second_ctrl_c_exits_now_and_leaves_nothing():
    before = _pids()
    proc, url = _start()
    gateways: list[str] = []
    try:
        assert url.startswith("http://127.0.0.1:")
        end = time.monotonic() + 10
        while len(_pids() - before) < 2 and time.monotonic() < end:
            time.sleep(0.1)
        spawned = sorted(_pids() - before)
        assert proc.stderr is not None
        gateway = None
        while gateway is None:
            line = proc.stderr.readline()
            assert line, "BFF exited before reporting its gateway pid"
            if m := re.search(r"gateway pid (\d+)", line):
                gateway = int(m.group(1))
        # Freeze the Gateway so the first shutdown is still in progress when the
        # second Ctrl+C lands (the Gateway otherwise quits in well under 0.2 s).
        gateways.append(str(gateway))
        os.kill(gateway, signal.SIGSTOP)
        proc.send_signal(signal.SIGINT)
        while (line := proc.stderr.readline()) and "shutting down" not in line:
            pass
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=20) == 130
        _wait_gone(spawned)  # the BFF SIGKILLs its process group on exit, stopped or not
    finally:
        if proc.poll() is None:
            proc.kill()
        for p in gateways:  # never leave a stopped child behind if an assert failed
            try:
                os.kill(int(p), signal.SIGKILL)
            except ProcessLookupError:
                pass


@requires_web
def test_web_bff_exits_when_the_cli_is_killed():
    before = _pids()
    secrets_before = _secrets_files()
    proc, url = _start()
    assert url.startswith("http://127.0.0.1:")
    end = time.monotonic() + 10
    while len(_pids() - before) < 2 and time.monotonic() < end:
        time.sleep(0.1)
    bffs = sorted(_pids() - before)
    assert len(bffs) >= 2, "expected the BFF and its Gateway child"
    proc.kill()  # SIGKILL: no chance to forward anything — BFF sees stdin EOF
    proc.wait(timeout=10)
    _wait_gone(bffs)
    # Even when the Gateway was killed before loading it, no key copy stays behind.
    assert _secrets_files() <= secrets_before
