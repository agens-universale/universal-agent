import yaml
import pytest
from pydantic import ValidationError

from config import load_config
from config_modules.message_notice import (
    ProjectMemoryNoticeConfig,
    ToolUsageNoticeRule,
    active_tool_usage_notice_rules,
    project_memory_notice_enabled,
    project_memory_notice_from_config_dict,
    normalize_project_memory_notice_config,
    tool_usage_notice_rules_from_config_dict,
)
from config_modules.root import AppConfig


def test_project_memory_notice_defaults():
    cfg = ProjectMemoryNoticeConfig()
    assert cfg.check_interval_messages == 5


def test_normalize_long_memory_notice_rejects_non_positive():
    cfg = ProjectMemoryNoticeConfig(check_interval_messages=-1)
    normalize_project_memory_notice_config(cfg)
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
                    {"tool_name": "x", "step_messages_count": 3, "notice_message": "n", "extra": 1}
                ]
            }
        )


def test_project_memory_notice_from_config_dict_from_params():
    cfg = project_memory_notice_from_config_dict(
        {"project_memory_params": {"check_interval_messages": 3}}
    )
    assert cfg.check_interval_messages == 3


def test_tool_usage_notice_rules_from_yaml_list():
    rules = tool_usage_notice_rules_from_config_dict(
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
    assert len(rules) == 1
    assert rules[0].tool_name == "working_memory"
    assert rules[0].step_messages_count == 3


def test_tool_usage_notice_rules_empty_without_config():
    assert tool_usage_notice_rules_from_config_dict({}) == []
    assert tool_usage_notice_rules_from_config_dict(None) == []


def test_active_tool_usage_notice_rules_respects_stop_tools():
    cfg = {
        "tool_usage_notice": [
            ToolUsageNoticeRule(
                tool_name="working_memory",
                step_messages_count=3,
                notice_message="nudge",
            )
        ],
        "stop_tools": ["working_memory"],
    }
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
    assert not project_memory_notice_enabled({})
    assert project_memory_notice_enabled(
        {"project_memory_params": {"enable_project_memory_notice": True}}
    )
    assert project_memory_notice_enabled({"enable_project_memory_notice": True})


def test_load_config_includes_message_notice_keys(tmp_path, monkeypatch):
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
    monkeypatch.setattr("config._project_local_config_path", lambda: None)
    monkeypatch.setattr("config._xdg_config_path", lambda: None)
    cfg = load_config(str(cfg_path))
    assert cfg["project_memory_check_interval_messages"] == 4
    assert cfg["enable_project_memory_notice"] is True
    assert cfg["project_memory_params"]["check_interval_messages"] == 4
    assert len(cfg["tool_usage_notice_params"]) == 1
    assert cfg["tool_usage_notice_params"][0]["step_messages_count"] == 25
