"""Конфиг-ключи трейс-диффов (волна 2): trace_inline_max_lines,
replay_expanded_turns, turn_summary_in_status. Потребители подключат их
в задачах этой волны (см. .superpowers/sdd/2026-08-10-diff-rework-wave2-plan) —
здесь только схема.
"""

from __future__ import annotations

from gc_core.config_modules.agent_edits import AgentEditsConfig
from gc_core.config_modules.root import AppConfig


def test_agent_edits_wave2_keys_defaults():
    cfg = AgentEditsConfig()
    assert cfg.trace_inline_max_lines == 12
    assert cfg.replay_expanded_turns == 20
    assert cfg.turn_summary_in_status is True


def test_app_config_includes_agent_edits_wave2_keys():
    app = AppConfig()
    assert app.agent_edits.trace_inline_max_lines == 12
    assert app.agent_edits.replay_expanded_turns == 20
    assert app.agent_edits.turn_summary_in_status is True


def test_flatten_exposes_agent_edits_wave2_keys_from_yaml(
    tmp_path, monkeypatch
):
    """Значения из config.yml доходят до настроек (`settings.agent_edits`)."""
    import gc_core.config as cfgmod
    import gc_core.config_modules.env_vars as env_vars

    # Адресуем МОДУЛЬ-владелец: `config.py` зовёт обе функции через него, и оба
    # имени переехали в `config_modules.env_vars` при разделении на пакеты.
    monkeypatch.setattr(env_vars, "project_local_config_path", lambda: None)
    monkeypatch.setattr(
        env_vars, "managed_config_path", lambda: tmp_path / "none.yml"
    )

    conf = tmp_path / "config.yml"
    conf.write_text(
        "agent_edits:\n"
        "  trace_inline_max_lines: 9\n"
        "  replay_expanded_turns: 1\n"
        "  turn_summary_in_status: false\n",
        encoding="utf-8",
    )
    edits = cfgmod.load_settings(str(conf)).settings.agent_edits
    assert edits.trace_inline_max_lines == 9
    assert edits.replay_expanded_turns == 1
    assert edits.turn_summary_in_status is False
