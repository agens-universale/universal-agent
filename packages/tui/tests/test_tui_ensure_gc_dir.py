"""gc-ua (TUI) при старте создаёт <cwd>/.gc/ — паритет с gc-cli."""
import pytest

pytest.importorskip("textual")

import gc_tui as tui
from gc_core.config_modules import env_vars


def test_tui_creates_project_gc_dir(tmp_path, monkeypatch):
    # изоляция: свой ~/.gc (config-шаблон → стоп-гайд), cwd → work
    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "home", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.setattr(env_vars, "project_slug", lambda: "p")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    rc = tui.main([])  # конфиг-шаблон → стоп-гайд вернёт 0, app не стартует
    assert rc == 0
    assert (work / ".gc").is_dir()  # <cwd>/.gc создан при старте TUI
