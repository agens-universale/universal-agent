import yaml
import pytest
from pydantic import ValidationError

from config import load_config
from config_modules.memory_messages import (
    LongMemoryMessagesConfig,
    MemoryGateConfig,
    ToolUsageNoticeRule,
    active_tool_usage_notice_rules,
    long_memory_notice_enabled,
    memory_file_notice_enabled,
    long_memory_messages_from_config_dict,
    memory_gate_from_config_dict,
    normalize_long_memory_messages_config,
    normalize_memory_gate_config,
    tool_usage_notice_rules_from_config_dict,
)
from config_modules.root import AppConfig


def test_memory_gate_defaults():
    cfg = MemoryGateConfig()
    assert cfg.cooldown_messages == 30


def test_long_memory_messages_defaults():
    cfg = LongMemoryMessagesConfig()
    assert cfg.check_interval_messages == 5


def test_normalize_memory_gate_rejects_non_positive():
    cfg = MemoryGateConfig(cooldown_messages=0)
    normalize_memory_gate_config(cfg)
    assert cfg.cooldown_messages == 30


def test_normalize_long_memory_messages_rejects_non_positive():
    cfg = LongMemoryMessagesConfig(check_interval_messages=-1)
    normalize_long_memory_messages_config(cfg)
    assert cfg.check_interval_messages == 5


def test_app_config_parses_memory_messages_sections():
    raw = yaml.safe_load(
        """
memory_gate:
  enable_memory_file_notice: true
  cooldown_messages: 40
long_memory:
  enable_long_memory_notice: false
  check_interval_messages: 7
"""
    )
    app = AppConfig.model_validate(raw)
    assert app.memory_gate.enable_memory_file_notice is True
    assert app.memory_gate.cooldown_messages == 40
    assert app.long_memory.enable_long_memory_notice is False
    assert app.long_memory.check_interval_messages == 7


def test_app_config_rejects_unknown_keys_in_memory_sections():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"memory_gate": {"cooldown_messages": 30, "extra": 1}})


def test_memory_gate_from_config_dict_prefers_model():
    model = MemoryGateConfig(cooldown_messages=12)
    cfg = memory_gate_from_config_dict(
        {"memory_gate": model, "memory_gate_params": {"cooldown_messages": 99}}
    )
    assert cfg is model
    assert cfg.cooldown_messages == 12


def test_long_memory_messages_from_config_dict_from_params():
    cfg = long_memory_messages_from_config_dict(
        {"long_memory_params": {"check_interval_messages": 3}}
    )
    assert cfg.check_interval_messages == 3


def test_memory_file_notice_enabled_from_nested_and_flat_config():
    assert not memory_file_notice_enabled({})
    assert memory_file_notice_enabled(
        {"memory_gate_params": {"enable_memory_file_notice": True}}
    )
    assert memory_file_notice_enabled({"enable_memory_file_notice": True})


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


def test_tool_usage_notice_legacy_memory_gate_bridge():
    rules = tool_usage_notice_rules_from_config_dict(
        {
            "memory_gate_params": {
                "enable_memory_file_notice": True,
                "cooldown_messages": 12,
            }
        }
    )
    assert len(rules) == 1
    assert rules[0].tool_name == "working_memory"
    assert rules[0].step_messages_count == 12


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


def test_long_memory_notice_enabled_from_nested_and_flat_config():
    assert not long_memory_notice_enabled({})
    assert long_memory_notice_enabled(
        {"long_memory_params": {"enable_long_memory_notice": True}}
    )
    assert long_memory_notice_enabled({"enable_long_memory_notice": True})


def test_load_config_includes_memory_messages_keys(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        """
backend:
  api_mode: chat_completions
  completions_url: "http://127.0.0.1:8014/v1/chat/completions"
memory_gate:
  enable_memory_file_notice: true
  cooldown_messages: 25
long_memory:
  enable_long_memory_notice: true
  check_interval_messages: 4
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("config._project_local_config_path", lambda: None)
    monkeypatch.setattr("config._xdg_config_path", lambda: None)
    cfg = load_config(str(cfg_path))
    assert cfg["memory_gate_cooldown_messages"] == 25
    assert cfg["long_memory_check_interval_messages"] == 4
    assert cfg["enable_memory_file_notice"] is True
    assert cfg["enable_long_memory_notice"] is True
    assert cfg["memory_gate_params"]["cooldown_messages"] == 25
    assert cfg["long_memory_params"]["check_interval_messages"] == 4
    assert "tool_usage_notice_params" in cfg
