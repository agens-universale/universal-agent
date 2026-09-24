"""Секция конфига `diff_tui:` — дефолты схемы и flatten в config_dict.

Пакет `diff_tui` умер, секция конфига — нет: её ключи держат лимиты записи
правок и их показа в TUI.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from gc_core.config_modules.diff_tui import DiffTuiConfig


def test_diff_tui_defaults():
    cfg = DiffTuiConfig()
    assert cfg.snapshots_max_files == 500
    assert cfg.max_diff_chars == 100_000


def test_diff_tui_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        DiffTuiConfig(unknown_flag=True)


def test_diff_tui_rejects_keys_of_dead_visual_diff():
    """Удалённые ключи не должны молча воскреснуть в схеме (см. compat.REMOVED)."""
    for dead in ("preview_context_lines", "full_file_max_lines"):
        with pytest.raises(ValidationError):
            DiffTuiConfig(**{dead: 1})


def test_app_config_includes_diff_tui():
    from gc_core.config_modules.root import AppConfig

    app = AppConfig()
    assert app.diff_tui.snapshots_max_files == 500
    assert app.diff_tui.max_diff_chars == 100_000


def test_flatten_exposes_diff_tui(tmp_path):
    """Значение из config.yml доходит до config_dict['diff_tui']."""
    from gc_core import config as cfgmod

    conf = tmp_path / "config.yml"
    conf.write_text(
        "diff_tui:\n"
        "  snapshots_max_files: 42\n"
        "  max_diff_chars: 100\n",
        encoding="utf-8",
    )
    cfg = cfgmod.load_config(str(conf))
    assert cfg["diff_tui"] == {
        "snapshots_max_files": 42,
        "max_diff_chars": 100,
        "hunk_context_lines": 3,
        "max_diff_source_bytes": 2_000_000,
        "shell_tracking": True,
        "trace_inline_max_lines": 30,
        "replay_expanded_turns": 20,
        "turn_summary_in_status": True,
    }
