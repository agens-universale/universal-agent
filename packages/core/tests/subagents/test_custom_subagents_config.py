"""Конфиг-ключи agent.subagents_dir / agent.subagents_max_count."""

import pytest
from gc_core.config_modules.root import AppConfig
from pydantic import ValidationError


def _agent(agent_extra=None):
    raw = {"agent": {**(agent_extra or {})}}
    return AppConfig(**raw).agent


def test_defaults():
    agent = _agent()
    assert agent.subagents_dir is None
    assert agent.subagents_max_count == 20


def test_values_reach_settings():
    agent = _agent({"subagents_dir": "/tmp/agents", "subagents_max_count": 3})
    assert agent.subagents_dir == "/tmp/agents"
    assert agent.subagents_max_count == 3


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_max_count_is_rejected(bad):
    # Ноль означал бы «папка задана, но агентов нет» — тихая и необъяснимая
    # для оператора ситуация, поэтому ловим на валидации конфига.
    with pytest.raises(ValidationError):
        AppConfig(agent={"subagents_max_count": bad})


def test_unknown_neighbour_key_still_forbidden():
    with pytest.raises(ValidationError):
        AppConfig(agent={"subagents_dirr": "/tmp/agents"})
