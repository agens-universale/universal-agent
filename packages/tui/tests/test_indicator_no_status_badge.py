import importlib

import pytest


def test_indicator_has_no_status_badge_api():
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    ind = ThinkingIndicator(clock=lambda: 0.0)
    assert not hasattr(ind, "set_status")
    assert not hasattr(ind, "_compose_status")


def test_status_badge_service_removed():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("gc_tui.services.status_badge")
