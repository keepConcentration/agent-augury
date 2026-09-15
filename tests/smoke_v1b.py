"""Smoke-test V1b: attention config validation."""
from agent_augury.config import (
    ConfigError,
    _merge_agent_attention,
    _normalize_attention,
    _validate_attention_section,
)


def test_defaults():
    norm = _normalize_attention({})
    assert norm["enabled"] is False
    assert norm["floors"]["default"] == 0.0
    assert norm["tiers"]["engage"] == 0.80
    assert norm["features"]["mention_boost"] == 0.5
    assert norm["context"]["skim_max_chars"] == 400


def test_mention_boost_too_low():
    try:
        _normalize_attention({"features": {"mention_boost": 0.3}})
    except ConfigError:
        return
    raise AssertionError("should have raised ConfigError")


def test_mention_boost_below_engage_ok():
    """mention_boost may be < tiers.engage (T3); only >= tiers.skim is required."""
    norm = _normalize_attention({
        "features": {"mention_boost": 0.5},
        "tiers": {"engage": 0.80},
    })
    assert norm["features"]["mention_boost"] == 0.5
    assert norm["tiers"]["engage"] == 0.80


def test_partial_override():
    part = {"enabled": True, "floors": {"default": 0.2}}
    norm = _normalize_attention(part)
    assert norm["enabled"] is True
    assert norm["floors"]["default"] == 0.2


def test_unknown_key():
    try:
        _validate_attention_section({"bogus": 1})
    except ConfigError:
        return
    raise AssertionError("unknown key should raise")


def test_tier_monotonicity():
    try:
        _validate_attention_section({"tiers": {"skim": 0.9, "engage": 0.5}})
    except ConfigError:
        return
    raise AssertionError("monotonicity violation should raise")


def test_per_agent_merge():
    norm = _normalize_attention({"floors": {"default": 0.3}})
    merged = _merge_agent_attention(norm, {"floor": 0.7}, 0)
    assert merged["floors"]["default"] == 0.7


def test_load_config_with_attention(tmp_path):
    """Integration: load_config handles attention: section."""
    import yaml

    from agent_augury.config import load_config

    cfg = {
        "agents": [{"id": "test", "backend": {"type": "fake"}}],
        "attention": {
            "enabled": True,
            "floors": {"default": 0.1},
        },
    }
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.dump(cfg), encoding="utf-8")
    data = load_config(str(p), allow_fake=True)
    assert data["_attention_normalized"]["enabled"] is True
    assert data["_attention_normalized"]["floors"]["default"] == 0.1


print("ALL SMOKE TESTS PASSED")