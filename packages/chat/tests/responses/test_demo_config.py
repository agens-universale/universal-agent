"""Образец demo_config_chat.yml обязан проходить схему."""

from pathlib import Path

import yaml
from gc_core.config_modules.root import AppConfig

_DEMO = Path(__file__).resolve().parents[4] / "demo_config_chat.yml"


def test_demo_config_chat_section_validates():
    raw = yaml.safe_load(_DEMO.read_text(encoding="utf-8"))
    cfg = AppConfig.model_validate(raw)
    section = cfg.chat_api
    assert section.host == "127.0.0.1"
    assert section.port == 8090
    assert section.auth_token == ""
    assert section.cors_origins == []
    assert section.launch_root is None
    assert section.text_answer_mode == "allow"
    assert section.finish_gate_mode == "off"
