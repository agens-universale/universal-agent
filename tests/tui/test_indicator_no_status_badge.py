import importlib

import pytest


def test_indicator_has_no_status_badge_api():
    from tui.widgets.thinking_indicator import ThinkingIndicator
    ind = ThinkingIndicator(state="done", frozen_elapsed=5, clock=lambda: 0.0)
    assert not hasattr(ind, "set_status")
    assert not hasattr(ind, "_compose_status")
    # Терминальная строка — без правого бейджа/хвостового паддинга.
    assert ind._render_text().plain == "✓ Выполнил за 5с"


def test_status_badge_service_removed():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("tui.services.status_badge")
