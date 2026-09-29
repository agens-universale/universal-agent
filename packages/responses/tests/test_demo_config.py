"""Образец demo_config_responses.yml обязан проходить схему."""

from pathlib import Path

import yaml
from gc_core.config_modules.root import AppConfig

_DEMO = Path(__file__).resolve().parents[3] / "demo_config_responses.yml"


def test_demo_config_responses_section_validates():
    raw = yaml.safe_load(_DEMO.read_text(encoding="utf-8"))
    cfg = AppConfig.model_validate(raw)
    section = cfg.responses
    assert section.host == "127.0.0.1"
    assert section.port == 8090
    assert section.auth_token == ""
    assert section.cors_origins == []
    assert section.launch_root is None
    assert section.text_answer_mode == "allow"
    assert section.finish_gate_mode == "off"
