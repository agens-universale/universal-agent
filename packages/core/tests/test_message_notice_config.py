import pytest
import yaml
from gc_core.config import load_settings
from gc_core.config_modules.message_notice import (
    ToolUsageNoticeRule,
    active_tool_usage_notice_rules,
    tool_usage_notice_rules_from_config_dict,
)
from gc_core.config_modules.project_memory import (
    ProjectMemoryConfig,
    normalize_project_memory_config,
    project_memory_from_config_dict,
    project_memory_notice_enabled,
)
from gc_core.config_modules.root import AppConfig
from gc_testkit.settings import make_settings
from pydantic import ValidationError


def test_project_memory_notice_defaults():
    cfg = ProjectMemoryConfig()
    assert cfg.check_interval_messages == 5
    assert cfg.max_chars == 10_000
    assert cfg.max_append_chars == 3_000


def test_normalize_long_memory_notice_rejects_non_positive():
    cfg = ProjectMemoryConfig(check_interval_messages=-1)
    cfg = normalize_project_memory_config(cfg)
    assert cfg.check_interval_messages == 5


def test_app_config_parses_message_notice_sections():
    raw = yaml.safe_load(
        """
tool_usage_notice:
  - tool_name: working_memory
    step_messages_count: 40
    notice_message: "Review thread memory."
project_memory:
  enable_project_memory_notice: false
  check_interval_messages: 7
"""
    )
    app = AppConfig.model_validate(raw)
    assert len(app.tool_usage_notice) == 1
    assert app.tool_usage_notice[0].step_messages_count == 40
    assert app.project_memory.enable_project_memory_notice is False
    assert app.project_memory.check_interval_messages == 7


def test_app_config_rejects_unknown_keys_in_notice_sections():
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "tool_usage_notice": [
                    {
                        "tool_name": "x",
                        "step_messages_count": 3,
                        "notice_message": "n",
                        "extra": 1,
                    }
                ]
            }
        )


def test_project_memory_notice_from_config_dict_from_params():
    cfg = project_memory_from_config_dict(
        make_settings({"project_memory": {"check_interval_messages": 3}})
    )
    assert cfg.check_interval_messages == 3


def test_tool_usage_notice_rules_from_yaml_list():
    rules = tool_usage_notice_rules_from_config_dict(
        make_settings(
            {
                "tool_usage_notice": [
                    {
                        "tool_name": "working_memory",
                        "step_messages_count": 3,
                        "notice_message": "nudge",
                    }
                ]
            }
        )
    )
    assert len(rules) == 1
    assert rules[0].tool_name == "working_memory"
    assert rules[0].step_messages_count == 3


def test_tool_usage_notice_rules_empty_without_config():
    assert tool_usage_notice_rules_from_config_dict(make_settings()) == []
    assert tool_usage_notice_rules_from_config_dict(None) == []


def test_active_tool_usage_notice_rules_respects_stop_tools():
    cfg = make_settings(
        {
            "tool_usage_notice": [
                ToolUsageNoticeRule(
                    tool_name="working_memory",
                    step_messages_count=3,
                    notice_message="nudge",
                )
            ],
            "agent.stop_tools": ["working_memory"],
        }
    )
    assert active_tool_usage_notice_rules(cfg) == []


def test_app_config_parses_tool_usage_notice_section():
    raw = yaml.safe_load(
        """
tool_usage_notice:
  - tool_name: working_memory
    step_messages_count: 5
    notice_message: "Review thread memory."
"""
    )
    app = AppConfig.model_validate(raw)
    assert len(app.tool_usage_notice) == 1
    assert app.tool_usage_notice[0].notice_message == "Review thread memory."


def test_project_memory_notice_enabled_from_nested_and_flat_config():
    assert not project_memory_notice_enabled(make_settings())
    assert project_memory_notice_enabled(
        make_settings(
            {"project_memory": {"enable_project_memory_notice": True}}
        )
    )
    assert project_memory_notice_enabled(
        make_settings({"project_memory.enable_project_memory_notice": True})
    )


def test_normalize_project_memory_limits_rejects_non_positive():
    cfg = ProjectMemoryConfig(max_chars=-1, max_append_chars=0)
    cfg = normalize_project_memory_config(cfg)
    assert cfg.max_chars == 10_000
    assert cfg.max_append_chars == 3_000


def test_load_settings_includes_message_notice_keys(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        """
backend:
  api_mode: chat_completions
  completions_url: "http://127.0.0.1:8014/v1/chat/completions"
tool_usage_notice:
  - tool_name: working_memory
    step_messages_count: 25
    notice_message: "Review thread memory."
project_memory:
  enable_project_memory_notice: true
  check_interval_messages: 4
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.project_local_config_path",
        lambda: None,
    )
    monkeypatch.setattr("gc_core.config._xdg_config_path", lambda: None)
    settings = load_settings(str(cfg_path)).settings
    memory = settings.project_memory
    assert memory.check_interval_messages == 4
    assert memory.max_chars == 10_000
    assert memory.max_append_chars == 3_000
    assert memory.enable_project_memory_notice is True
    assert len(settings.tool_usage_notice) == 1
    assert settings.tool_usage_notice[0].step_messages_count == 25
