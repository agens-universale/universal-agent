"""TDD: strip_tags_projection normalize + load_config → snapshot for callers."""

import yaml
from gc_core.config import load_config
from gc_core.config_modules.strip_tags_projection import (
    StripTagsProjectionConfig,
    normalize_strip_tags_projection_config,
    persist_reasoning_content_from_config,
    projection_tags_from_config,
)
from gc_core.message_projection import project_text_for_model

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
    assert cfg.tags == ["ok_tag", "think"]


def test_normalize_strip_think_default_false():
    cfg = StripTagsProjectionConfig()
    assert cfg.strip_think is False


def test_persist_reasoning_content_default_true():
    assert persist_reasoning_content_from_config({}) is True
    assert persist_reasoning_content_from_config(None) is True
    assert (
        persist_reasoning_content_from_config(
            {"strip_tags_projection": {"strip_think": True}}
        )
        is False
    )
    assert (
        persist_reasoning_content_from_config(
            {"strip_tags_projection": {"strip_think": False}}
        )
        is True
    )


def test_normalize_keeps_think_in_tags():
    cfg = StripTagsProjectionConfig(tags=["think", "<THINK>"])
    normalize_strip_tags_projection_config(cfg)
    assert cfg.tags == ["think"]


def test_normalize_dedupe_case_insensitive():
    cfg = StripTagsProjectionConfig(tags=["Foo", "foo", "FOO", "bar"])
    normalize_strip_tags_projection_config(cfg)
    assert cfg.tags == ["Foo", "bar"]


def test_project_from_normalized_tags():
    assert project_text_for_model(_SAMPLE, tags=["redacted_thinking"]) == "ok"


def test_load_config_exposes_strip_tags_for_callers(tmp_path):
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
    tags = projection_tags_from_config(loaded)
    assert persist_reasoning_content_from_config(loaded) is True
    assert project_text_for_model(_SAMPLE, tags=tags, strip_think=True) == "ok"


def test_load_config_empty_tags_strips_only_think(tmp_path):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "backend:\n  api_mode: chat_completions\nstrip_tags_projection:\n  tags: []\n",
        encoding="utf-8",
    )

    loaded = load_config(str(cfg_path))
    assert loaded["strip_tags_projection"]["strip_think"] is False
    tags = projection_tags_from_config(loaded)
    assert persist_reasoning_content_from_config(loaded) is True
    assert project_text_for_model(_SAMPLE, tags=tags, strip_think=True) == (
        "<redacted_thinking>r</redacted_thinking>ok"
    )


def test_load_config_strip_think_false_is_persist_only_not_prose(tmp_path):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "backend": {"api_mode": "chat_completions"},
                "strip_tags_projection": {"strip_think": False, "tags": []},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_config(str(cfg_path))
    assert loaded["strip_tags_projection"]["strip_think"] is False
    assert persist_reasoning_content_from_config(loaded) is True
    tags = projection_tags_from_config(loaded)
    # Проза derived-sink'ов всегда без ``<think>``; флаг — только persist канона.
    assert project_text_for_model(_SAMPLE, tags=tags, strip_think=True) == (
        "<redacted_thinking>r</redacted_thinking>ok"
    )


def test_load_config_strip_think_false_think_in_tags_strips_prose(tmp_path):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "backend": {"api_mode": "chat_completions"},
                "strip_tags_projection": {
                    "strip_think": False,
                    "tags": ["think"],
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_config(str(cfg_path))
    tags = projection_tags_from_config(loaded)
    assert persist_reasoning_content_from_config(loaded) is True
    assert project_text_for_model(_SAMPLE, tags=tags, strip_think=True) == (
        "<redacted_thinking>r</redacted_thinking>ok"
    )
