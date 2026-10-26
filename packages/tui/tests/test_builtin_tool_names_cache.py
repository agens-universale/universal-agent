"""Список встроенных тулов для уведомлений кешируется по объекту настроек."""

import pytest
from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp


@pytest.fixture
def counted(monkeypatch):
    calls = []

    def fake(settings):
        calls.append(settings)
        return {"fs_read"}

    monkeypatch.setattr("gc_tui.app.all_builtin_tool_names", fake)
    return calls


@pytest.fixture
def app(tmp_path):
    return TuiApp(threads_dir=str(tmp_path), system="cli")


def test_same_settings_object_builds_once(app, counted):
    s = tui_settings({})
    assert app._builtin_tool_names(s) == frozenset({"fs_read"})
    app._builtin_tool_names(s)
    assert len(counted) == 1


def test_new_settings_object_rebuilds(app, counted):
    app._builtin_tool_names(tui_settings({}))
    app._builtin_tool_names(tui_settings({}))
    assert len(counted) == 2
