"""Tests for all_builtin_tool_names in registry."""

import sys

import pytest


@pytest.fixture(autouse=True)
def _ensure_real_tools_module():
    """Восстановить реальный модуль tools, если он был подменён tests/tools/conftest.py."""
    stale = [
        name
        for name in sys.modules
        if name == "gc_core.tools" or name.startswith("gc_core.tools.")
    ]
    for name in stale:
        sys.modules.pop(name, None)
    yield
    stale = [
        name
        for name in sys.modules
        if name == "gc_core.tools" or name.startswith("gc_core.tools.")
    ]
    for name in stale:
        sys.modules.pop(name, None)


def test_returns_prefilter_names_ignoring_stop_tools():
    """stop_tools НЕ влияет на состав имён (проверка против полного набора)."""
    from gc_core.tools.registry import all_builtin_tool_names
    from gc_testkit.settings import make_settings

    # stop_tools НЕ влияет на состав имён (проверка против полного набора)
    names_no_stop = all_builtin_tool_names(make_settings())
    names_with_stop = all_builtin_tool_names(
        make_settings({"agent.stop_tools": ["fs_write_file"]})
    )
    assert names_no_stop == names_with_stop
    assert "fs_write_file" in names_no_stop
    assert "turn_ask_human" in names_no_stop
