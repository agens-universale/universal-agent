"""Секция конфига `agent_edits:` — дефолты схемы и flatten в config_dict.

Пакет `agent_edits` умер, секция конфига — нет: её ключи держат лимиты записи
правок и их показа в TUI.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gc_core.config_modules.agent_edits import AgentEditsConfig


def test_agent_edits_defaults():
    cfg = AgentEditsConfig()
    assert cfg.snapshots_max_files == 500
    assert cfg.max_diff_chars == 100_000


def test_agent_edits_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        AgentEditsConfig(unknown_flag=True)


def test_agent_edits_rejects_keys_of_dead_visual_diff():
    """Удалённые ключи не должны молча воскреснуть в схеме (см. compat.REMOVED)."""
    for dead in ("preview_context_lines", "full_file_max_lines"):
        with pytest.raises(ValidationError):
            AgentEditsConfig(**{dead: 1})


def test_app_config_includes_agent_edits():
    from gc_core.config_modules.root import AppConfig

    app = AppConfig()
    assert app.agent_edits.snapshots_max_files == 500
    assert app.agent_edits.max_diff_chars == 100_000


def test_load_settings_exposes_agent_edits(tmp_path):
    """Значение из config.yml доходит до настроек `agent_edits`."""
    from gc_core import config as cfgmod

    conf = tmp_path / "config.yml"
    conf.write_text(
        "agent_edits:\n  snapshots_max_files: 42\n  max_diff_chars: 100\n",
        encoding="utf-8",
    )
    loaded = cfgmod.load_settings(str(conf))
    assert loaded.settings.agent_edits.model_dump() == {
        "snapshots_max_files": 42,
        "max_diff_chars": 100,
        "hunk_context_lines": 3,
        "max_diff_source_bytes": 2_000_000,
        "shell_tracking": True,
        "trace_inline_max_lines": 3,
        "replay_expanded_turns": 20,
        "turn_summary_in_status": True,
        "record": {"tui": True, "cli": False, "server": False},
    }
