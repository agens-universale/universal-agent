"""Конфиг-ключи слоя данных диффов (волна 1): hunk_context_lines,
max_diff_source_bytes, shell_tracking. Потребители подключат их в волне 2-3
(см. .superpowers/sdd/2026-08-09-diff-rework-plan) — здесь только схема.
"""
from __future__ import annotations

from gc_core.config_modules.agent_edits import AgentEditsConfig
from gc_core.config_modules.root import AppConfig


def test_agent_edits_new_keys_defaults():
    cfg = AgentEditsConfig()
    assert cfg.hunk_context_lines == 3
    assert cfg.max_diff_source_bytes == 2_000_000
    assert cfg.shell_tracking is True


def test_app_config_includes_agent_edits_new_keys():
    app = AppConfig()
    assert app.agent_edits.hunk_context_lines == 3
    assert app.agent_edits.max_diff_source_bytes == 2_000_000
    assert app.agent_edits.shell_tracking is True


def test_snapshots_max_files_default_and_compat():
    from gc_core.config_modules.compat import RENAMES, apply_compat

    assert AgentEditsConfig().snapshots_max_files == 500
    assert RENAMES["agent_edits.cache_max_orig"] == "agent_edits.snapshots_max_files"
    raw = {"agent_edits": {"cache_max_orig": 42}}
    facts = apply_compat(raw)
    assert raw["agent_edits"] == {"snapshots_max_files": 42}
    assert any("cache_max_orig" in f for f in facts)


def test_flatten_exposes_agent_edits_new_keys_from_yaml(tmp_path, monkeypatch):
    """Значения из config.yml доходят до config_dict['agent_edits']."""
    import gc_core.config as cfgmod
    import gc_core.config_modules.env_vars as env_vars

    # Адресуем МОДУЛЬ-владелец: `config.py` зовёт обе функции через него,
    # поэтому подмена доезжает. Оба имени переехали в `config_modules.env_vars`
    # при разделении на пакеты — тот же адрес, что у соседних наборов ядра.
    monkeypatch.setattr(env_vars, "project_local_config_path", lambda: None)
    monkeypatch.setattr(env_vars, "managed_config_path", lambda: tmp_path / "none.yml")

    conf = tmp_path / "config.yml"
    conf.write_text(
        "agent_edits:\n"
        "  hunk_context_lines: 7\n"
        "  max_diff_source_bytes: 12345\n"
        "  shell_tracking: false\n",
        encoding="utf-8",
    )
    cfg = cfgmod.load_config(str(conf))
    assert cfg["agent_edits"]["hunk_context_lines"] == 7
    assert cfg["agent_edits"]["max_diff_source_bytes"] == 12345
    assert cfg["agent_edits"]["shell_tracking"] is False
