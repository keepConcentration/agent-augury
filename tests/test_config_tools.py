"""config tools: 섹션 검증 테스트 (AGENT_TOOLS_EXPANSION_DESIGN.md §4.7).

agent-2 담당 — config.py 의 tools: 검증 규칙을 검증한다.

검증 규칙:
1. tools: 최상위 키는 shell/web/file 만 허용.
2. tools.shell / tools.web / tools.file 의 하위 키 화이트리스트.
3. web.search_provider 는 duckduckgo|serper|tavily|searxng 중 하나.
4. 에이전트별 tools: 도 동일 검증 (딥 병합은 ToolPolicy.from_config/merge 에서).
5. 빈 shell allowed → 경고 (치명 오류 아님).
"""

from __future__ import annotations

import pytest

from agent_augury.config import ConfigError, load_config


def _write(tmp_path, yaml_text: str):
    p = tmp_path / "session.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    return str(p)


def _base_yaml(tools_yaml: str = "") -> str:
    """전역 tools: 블록이 있는 기본 config (agents 는 단일 fake 에이전트).

    ``tools_yaml`` 은 들여쓰기 2칸으로 ``tools:`` 아래에 들어간다.
    """
    tools_block = f"tools:\n{tools_yaml}\n" if tools_yaml else ""
    return f"""\
{tools_block}max_steps: 10
agents:
  - id: agent-1
    backend:
      type: fake
      script: ["ok"]
"""


# ---------------------------------------------------------------------------
# 최상위 키 화이트리스트
# ---------------------------------------------------------------------------


def test_tools_unknown_top_key_rejected(tmp_path):
    path = _write(
        tmp_path,
        _base_yaml("  shell:\n    enabled: true\n  unknown: 1\n"),
    )
    with pytest.raises(ConfigError, match="unknown key 'unknown'"):
        load_config(path, allow_fake=True)


def test_tools_shell_unknown_key_rejected(tmp_path):
    path = _write(
        tmp_path,
        _base_yaml("  shell:\n    enable: true\n"),  # 오타: enabled 가 아니라 enable
    )
    with pytest.raises(ConfigError, match="unknown key 'enable'"):
        load_config(path, allow_fake=True)


def test_tools_web_unknown_key_rejected(tmp_path):
    path = _write(
        tmp_path,
        _base_yaml("  web:\n    max_result: 5\n"),  # 오타
    )
    with pytest.raises(ConfigError, match="unknown key 'max_result'"):
        load_config(path, allow_fake=True)


def test_tools_file_unknown_key_rejected(tmp_path):
    path = _write(
        tmp_path,
        _base_yaml("  file:\n    edit: true\n"),  # edit_enabled 가 아니라 edit
    )
    with pytest.raises(ConfigError, match="unknown key 'edit'"):
        load_config(path, allow_fake=True)


# ---------------------------------------------------------------------------
# search_provider 검증
# ---------------------------------------------------------------------------


def test_invalid_search_provider_rejected(tmp_path):
    path = _write(
        tmp_path,
        _base_yaml("  web:\n    search_provider: bing\n"),
    )
    with pytest.raises(ConfigError, match="search_provider"):
        load_config(path, allow_fake=True)


@pytest.mark.parametrize("provider", ["duckduckgo", "serper", "tavily", "searxng"])
def test_valid_search_provider_accepted(tmp_path, provider):
    path = _write(
        tmp_path,
        _base_yaml(f"  web:\n    search_provider: {provider}\n"),
    )
    cfg = load_config(path, allow_fake=True)
    assert cfg["tools"]["web"]["search_provider"] == provider


# ---------------------------------------------------------------------------
# 타입 검증
# ---------------------------------------------------------------------------


def test_shell_allowed_must_be_list(tmp_path):
    path = _write(
        tmp_path,
        _base_yaml("  shell:\n    allowed: git\n"),  # 문자열 → 오류
    )
    with pytest.raises(ConfigError, match="shell.allowed"):
        load_config(path, allow_fake=True)


def test_web_allow_domains_must_be_list(tmp_path):
    path = _write(
        tmp_path,
        _base_yaml("  web:\n    allow_domains: example.com\n"),
    )
    with pytest.raises(ConfigError, match="allow_domains"):
        load_config(path, allow_fake=True)


def test_file_allowed_roots_must_be_list_of_strings(tmp_path):
    path = _write(
        tmp_path,
        _base_yaml("  file:\n    allowed_roots: [1, 2]\n"),
    )
    with pytest.raises(ConfigError, match="allowed_roots"):
        load_config(path, allow_fake=True)


# ---------------------------------------------------------------------------
# 에이전트별 tools: 검증
# ---------------------------------------------------------------------------


def test_agent_tools_unknown_key_rejected(tmp_path):
    path = _write(
        tmp_path,
        "max_steps: 10\n"
        "agents:\n"
        "  - id: agent-1\n"
        "    tools:\n"
        "      shell:\n"
        "        enabled: false\n"
        "        bogus: true\n"
        "    backend:\n"
        "      type: fake\n"
        "      script: [\"ok\"]\n",
    )
    with pytest.raises(ConfigError, match="agents\\[0\\].tools"):
        load_config(path, allow_fake=True)


def test_agent_tools_valid_override_accepted(tmp_path):
    path = _write(
        tmp_path,
        "max_steps: 10\n"
        "tools:\n"
        "  shell:\n"
        "    allowed: [git]\n"
        "agents:\n"
        "  - id: agent-1\n"
        "    tools:\n"
        "      shell:\n"
        "        enabled: false\n"
        "    backend:\n"
        "      type: fake\n"
        "      script: [\"ok\"]\n",
    )
    cfg = load_config(path, allow_fake=True)
    assert cfg["tools"]["shell"]["allowed"] == ["git"]
    assert cfg["agents"][0]["tools"]["shell"]["enabled"] is False


# ---------------------------------------------------------------------------
# 경고 (치명 오류 아님)
# ---------------------------------------------------------------------------


def test_empty_shell_allowed_warns_but_loads(tmp_path, capsys):
    """shell.enabled:true + allowed:[] → 경고 출력 + 로드 성공 (agent-1 피드백 5)."""
    path = _write(
        tmp_path,
        _base_yaml("  shell:\n    enabled: true\n    allowed: []\n"),
    )
    cfg = load_config(path, allow_fake=True)
    assert cfg["tools"]["shell"]["allowed"] == []
    captured = capsys.readouterr()
    assert "warning" in captured.out.lower()


def test_allow_domains_ip_literal_warns(tmp_path, capsys):
    path = _write(
        tmp_path,
        _base_yaml("  web:\n    allow_domains: [127.0.0.1]\n"),
    )
    cfg = load_config(path, allow_fake=True)
    assert cfg["tools"]["web"]["allow_domains"] == ["127.0.0.1"]
    captured = capsys.readouterr()
    assert "warning" in captured.out.lower()


# ---------------------------------------------------------------------------
# tools 미설정 → 기본 (로드 성공, 신규 도구 기본 활성)
# ---------------------------------------------------------------------------


def test_no_tools_section_loads_with_defaults(tmp_path):
    path = _write(tmp_path, _base_yaml())
    cfg = load_config(path, allow_fake=True)
    assert "tools" not in cfg  # 미설정 → 키 없음 (ToolPolicy.from_config 기본값 사용)
