"""Секция chat образца demo_config_server.yml обязана проходить схему."""

from gc_server.config import load_server_config
from gc_testkit.paths import REPO


def test_demo_config_chat_section_validates():
    cfg = load_server_config(str(REPO / "demo_config_server.yml"))
    section = cfg.surface("chat")
    assert section.port == 8090
    assert section.cors_origins == []
    assert section.launch_root is None
    assert section.text_answer_mode == "allow"
    assert section.finish_gate_mode == "off"
