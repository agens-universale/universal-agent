"""Секция acp.*: дефолты IDE-чата и загрузка из config.yml."""

import pytest
from gc_core.config_modules.acp import AcpConfig
from pydantic import ValidationError


def test_acp_config_defaults():
    cfg = AcpConfig()
    assert cfg.text_answer_mode == "allow"
    assert cfg.finish_gate_mode == "off"
    assert cfg.todo_reminder_steps == 0
    assert cfg.confirm_timeout_sec == 600


def test_acp_config_accepts_overrides():
    cfg = AcpConfig(
        text_answer_mode="deny",
        finish_gate_mode="smart",
        todo_reminder_steps=5,
        confirm_timeout_sec=30,
    )
    assert cfg.text_answer_mode == "deny"
    assert cfg.finish_gate_mode == "smart"
    assert cfg.todo_reminder_steps == 5
    assert cfg.confirm_timeout_sec == 30


def test_acp_config_rejects_unknown_modes():
    with pytest.raises(ValidationError):
        AcpConfig(text_answer_mode="bogus")
    with pytest.raises(ValidationError):
        AcpConfig(finish_gate_mode="bogus")
    with pytest.raises(ValidationError):
        AcpConfig(confirm_timeout_sec=-1)


def test_load_settings_exposes_acp_keys(tmp_path):
    from gc_core import config as cfgmod

    conf = tmp_path / "config.yml"
    conf.write_text(
        "acp:\n"
        "  text_answer_mode: deny\n"
        '  finish_gate_mode: "off"\n'
        "  todo_reminder_steps: 3\n",
        encoding="utf-8",
    )
    acp = cfgmod.load_settings(str(conf)).settings.acp
    assert acp.text_answer_mode == "deny"
    assert acp.finish_gate_mode == "off"
    assert acp.todo_reminder_steps == 3


def test_load_settings_default_acp_keys(tmp_path):
    from gc_core import config as cfgmod

    conf = tmp_path / "config.yml"
    conf.write_text("{}\n", encoding="utf-8")
    acp = cfgmod.load_settings(str(conf)).settings.acp
    assert acp.text_answer_mode == "allow"
    assert acp.finish_gate_mode == "off"
    assert acp.todo_reminder_steps == 0
    assert acp.confirm_timeout_sec == 600
