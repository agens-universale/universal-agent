"""DiffTuiConfig defaults + flatten в config_dict."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from gc_core.config_modules.diff_tui import DiffTuiConfig


def test_diff_tui_defaults():
    cfg = DiffTuiConfig()
    assert cfg.cache_max_orig == 500
    assert cfg.preview_context_lines == 50
    assert cfg.full_file_max_lines == 300
    assert cfg.max_diff_chars == 100_000


def test_diff_tui_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        DiffTuiConfig(unknown_flag=True)


def test_app_config_includes_diff_tui():
    from gc_core.config_modules.root import AppConfig

    app = AppConfig()
    assert app.diff_tui.cache_max_orig == 500
    assert app.diff_tui.preview_context_lines == 50
    assert app.diff_tui.full_file_max_lines == 300
    assert app.diff_tui.max_diff_chars == 100_000


def test_flatten_exposes_diff_tui(tmp_path, monkeypatch):
    """Значение из config.yml доходит до config_dict['diff_tui']."""
    from gc_core import config as cfgmod

    monkeypatch.setattr(cfgmod, "_project_local_config_path", lambda: None)
    monkeypatch.setattr(cfgmod, "managed_config_path", lambda: tmp_path / "none.yml")

    conf = tmp_path / "config.yml"
    conf.write_text(
        "diff_tui:\n"
        "  cache_max_orig: 42\n"
        "  preview_context_lines: 7\n"
        "  full_file_max_lines: 120\n"
        "  max_diff_chars: 100\n",
        encoding="utf-8",
    )
    cfg = cfgmod.load_config(str(conf))
    assert cfg["diff_tui"] == {
        "cache_max_orig": 42,
        "preview_context_lines": 7,
        "full_file_max_lines": 120,
        "max_diff_chars": 100,
    }
