"""Каналы ``chat`` и ``a2a``: самопроверка ``off`` · ``smart`` · ``always``;
прежнее ``on`` читается как ``always``."""

import pytest
from gc_server.config import A2aConfig, ChatConfig


@pytest.mark.parametrize("section", [ChatConfig, A2aConfig])
def test_channel_gate_always_and_legacy_on(section):
    assert section(finish_gate_mode="always").finish_gate_mode == "always"
    assert section(finish_gate_mode="on").finish_gate_mode == "always"
