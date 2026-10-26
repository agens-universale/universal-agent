"""stream → tui.stream: канонический ключ в tui, с legacy-фолбэком на backend.stream.

``_tui_stream_enabled(settings)``: явный ``tui.stream`` (True/False) побеждает;
иначе legacy ``backend.stream``; иначе дефолт True. ``tui.stream`` по умолчанию
None (не задан) — чтобы отличить «явно выключил» от «не трогал» и уважать
legacy ``backend.stream``."""

from gc_core.config_modules.tui import TuiConfig
from gc_core.processing_responses import _tui_stream_enabled
from gc_core.settings import default_settings


def _enabled(**updates) -> bool:
    return _tui_stream_enabled(default_settings(**updates))


def test_tui_stream_field_default_unset():
    assert TuiConfig().stream is None
    assert TuiConfig(stream=False).stream is False
    assert TuiConfig(stream=True).stream is True


def test_explicit_tui_stream_wins():
    assert _enabled(**{"tui.stream": True}) is True
    assert _enabled(**{"tui.stream": False}) is False
    # явный tui.stream побеждает legacy backend.stream
    assert _enabled(**{"tui.stream": False, "backend.stream": True}) is False


def test_legacy_stream_fallback_when_tui_unset():
    # tui.stream не задан (None или отсутствует) → падаем на legacy backend.stream
    assert _enabled(**{"tui.stream": None, "backend.stream": False}) is False
    assert _enabled(**{"backend.stream": False}) is False


def test_default_true_when_neither_set():
    assert _enabled() is True
    assert _enabled(**{"tui.stream": None}) is True
