"""Конфиг-ключи трейс-диффов (волна 2): trace_inline_max_lines,
replay_expanded_turns, turn_summary_in_status. Потребители подключат их
в задачах этой волны (см. .superpowers/sdd/2026-08-10-diff-rework-wave2-plan) —
здесь только схема.
"""
from __future__ import annotations

from gc_core.config_modules.diff_tui import DiffTuiConfig
from gc_core.config_modules.root import AppConfig


def test_diff_tui_wave2_keys_defaults():
    cfg = DiffTuiConfig()
    assert cfg.trace_inline_max_lines == 30
    assert cfg.replay_expanded_turns == 20
    assert cfg.turn_summary_in_status is True


def test_app_config_includes_diff_tui_wave2_keys():
    app = AppConfig()
    assert app.diff_tui.trace_inline_max_lines == 30
    assert app.diff_tui.replay_expanded_turns == 20
    assert app.diff_tui.turn_summary_in_status is True


def test_flatten_exposes_diff_tui_wave2_keys_from_yaml(tmp_path, monkeypatch):
    """Значения из config.yml доходят до config_dict['diff_tui']."""
    import gc_core.config as cfgmod
    import gc_core.config_modules.env_vars as env_vars

    # Адресуем МОДУЛЬ-владелец: `config.py` зовёт обе функции через него, и оба
    # имени переехали в `config_modules.env_vars` при разделении на пакеты.
    monkeypatch.setattr(env_vars, "project_local_config_path", lambda: None)
    monkeypatch.setattr(env_vars, "managed_config_path", lambda: tmp_path / "none.yml")

    conf = tmp_path / "config.yml"
    conf.write_text(
        "diff_tui:\n"
        "  trace_inline_max_lines: 9\n"
        "  replay_expanded_turns: 1\n"
        "  turn_summary_in_status: false\n",
        encoding="utf-8",
    )
    cfg = cfgmod.load_config(str(conf))
    assert cfg["diff_tui"]["trace_inline_max_lines"] == 9
    assert cfg["diff_tui"]["replay_expanded_turns"] == 1
    assert cfg["diff_tui"]["turn_summary_in_status"] is False
