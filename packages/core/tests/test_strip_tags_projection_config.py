"""TDD: strip_tags_projection normalize + load_config → configure_for_model_tags."""

import yaml

from gc_core.config import load_config
from gc_core.config_modules.strip_tags_projection import StripTagsProjectionConfig, normalize_strip_tags_projection_config
from gc_core.message_projection import configure_for_model_tags, project_text_for_model

_SAMPLE = "<think>t</think><redacted_thinking>r</redacted_thinking>ok"


def test_normalize_missing_section_defaults_empty():
    cfg = StripTagsProjectionConfig()
    normalize_strip_tags_projection_config(cfg)
    assert cfg.tags == []


def test_normalize_strips_angle_brackets():
    cfg = StripTagsProjectionConfig(tags=["<redacted_thinking>"])
    normalize_strip_tags_projection_config(cfg)
    assert cfg.tags == ["redacted_thinking"]


def test_normalize_warn_drop_invalid(caplog):
    cfg = StripTagsProjectionConfig(
        tags=["", "  ", "bad/name", "has space", "ok_tag", "think", "THINK"]
    )
    with caplog.at_level("WARNING"):
        normalize_strip_tags_projection_config(cfg)
    assert cfg.tags == ["ok_tag"]
    assert "think" not in [t.lower() for t in cfg.tags]


def test_normalize_dedupe_case_insensitive():
    cfg = StripTagsProjectionConfig(tags=["Foo", "foo", "FOO", "bar"])
    normalize_strip_tags_projection_config(cfg)
    assert cfg.tags == ["Foo", "bar"]


def test_configure_from_normalized_tags_affects_project():
    configure_for_model_tags(["redacted_thinking"])
    try:
        assert project_text_for_model(_SAMPLE) == "ok"
    finally:
        configure_for_model_tags([])


def test_load_config_wires_strip_tags_into_project(tmp_path):
    """Регрессия: забыли configure_for_model_tags в load_config → extras молча no-op."""
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "backend": {"api_mode": "chat_completions"},
                "strip_tags_projection": {"tags": ["redacted_thinking"]},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_config(str(cfg_path))
    assert loaded["strip_tags_projection"]["tags"] == ["redacted_thinking"]
    assert project_text_for_model(_SAMPLE) == "ok"


def test_load_config_empty_tags_strips_only_think(tmp_path):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "backend:\n  api_mode: chat_completions\n"
        "strip_tags_projection:\n  tags: []\n",
        encoding="utf-8",
    )

    load_config(str(cfg_path))
    assert project_text_for_model(_SAMPLE) == (
        "<redacted_thinking>r</redacted_thinking>ok"
    )
